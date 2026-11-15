#!/usr/bin/env python3
"""
inventor_pp.py - pos-processamento do OrcaSlicer para a FlashForge Inventor (original, 2 extrusores).

Converte o G-code do Orca para o "dialeto" que o FlashPrint gera para a Inventor:
  * extrusao absoluta com contador separado por extrusor (sem M82/M83/G92 E);
  * troca de extrusor so com "M108 Tn" (remove o "Tn" solto que o Orca adiciona);
  * M109 -> M104 + M6 (espera de temperatura do bico), M190 -> M140 + M7 (mesa);
  * aquece os dois bicos juntos no inicio e mantem os dois quentes o tempo todo
    (sem esfriar o bico parado), para a troca de extrusor nao ficar esperando;
  * ventoinha: M106 S0 -> M107, M106 Sx -> M106;
  * limita a velocidade do eixo Z a 7 mm/s (F420) e a de X/Y a 80 mm/s, como o FlashPrint;
  * remove comandos que o FlashPrint nunca usa (M204, M220, M73, G21, G4 S0 ...);
  * numera as linhas (N1, N2, ...);
  * confere se tudo cabe na mesa (230 x 150 x 160, origem no centro) e se nenhum
    bico extrude antes de ser aquecido. Se algo estiver errado, aborta a exportacao.

Uso no Orca (Configuracoes de processo > Outros > Scripts de pos-processamento):
  "C:\\caminho\\para\\python.exe" "C:\\caminho\\para\\inventor_pp.py";
"""
import math
import os
import re
import sys

# ---- limites da Inventor -------------------------------------------------
X_MIN, X_MAX = -115.0, 115.0
Y_MIN, Y_MAX = -75.0, 75.0
Z_MAX = 160.0
Z_MAX_FEED = 420.0          # mm/min (7 mm/s), igual ao FlashPrint
XY_MAX_FEED = 4800.0        # mm/min (80 mm/s), deslocamento maximo usado pelo FlashPrint
BOUNDS_TOL = 0.5            # mm de folga na checagem de limites
# Mantem os dois bicos sempre na temperatura de impressao (ignora o resfriamento
# do bico parado que o Orca faz nas trocas). Assim a troca nao fica esperando aquecer.
HOLD_TEMPS = True

# Comandos que o FlashPrint usa para a Inventor (e que vamos deixar passar).
ALLOWED = {
    "G1", "G28", "G90", "G91", "G161", "G162",
    "M6", "M7", "M18", "M104", "M106", "M107", "M108", "M118",
    "M132", "M140", "M651", "M652", "M907",
}
# Comandos que sao "traduzidos" ou tratados aqui antes da whitelist.
HANDLED = {"G0", "G92", "M82", "M83", "M109", "M190", "G4", "G2", "G3"}

TOKEN_RE = re.compile(r"([A-Za-z])([-+]?(?:\d+\.?\d*|\.\d+))?")


class ConvertError(Exception):
    pass


def fmt(v, nd):
    s = f"{v:.{nd}f}".rstrip("0").rstrip(".")
    return s if s not in ("", "-0") else "0"


def strip_line(raw):
    """Remove comentario, numero de linha e checksum. Retorna o comando limpo."""
    line = raw.split(";", 1)[0].strip()
    if not line:
        return ""
    if "*" in line:
        line = line.split("*", 1)[0].strip()
    if line[:1] in ("N", "n"):
        parts = line.split(None, 1)
        line = parts[1] if len(parts) > 1 else ""
    return line.strip()


def parse(cmdline):
    """'G1 X1 Y2 E.5' -> ('G1', [('X','1'), ('Y','2'), ('E','.5')])"""
    parts = cmdline.split()
    code = parts[0].upper()
    # normaliza G01 -> G1, M0104 -> M104
    m = re.match(r"^([GMT])0*(\d+)$", code)
    if m:
        code = m.group(1) + m.group(2)
    params = []
    for p in parts[1:]:
        for letter, value in TOKEN_RE.findall(p):
            params.append((letter.upper(), value))
    return code, params


def prescan_temps(lines):
    """Descobre quais extrusores imprimem e a primeira temperatura de cada um."""
    tool = None
    first = {}
    extrudes = set()
    for raw in lines:
        cmd = strip_line(raw)
        if not cmd:
            continue
        code, params = parse(cmd)
        p = {k: v for k, v in params}
        if re.fullmatch(r"T\d+", code):
            tool = int(code[1:])
        elif code == "M108" and p.get("T") not in (None, ""):
            tool = int(float(p["T"]))
        elif code in ("M104", "M109"):
            t = int(float(p["T"])) if p.get("T") not in (None, "") else tool
            s = float(p.get("S") or 0)
            if t is not None and s > 0 and t not in first:
                first[t] = int(round(s))
        elif code in ("G0", "G1") and p.get("E") not in (None, "") and tool is not None:
            if float(p["E"]) > 0:
                extrudes.add(tool)
    return {t: first[t] for t in extrudes if t in first}


def standby_lines(lines):
    """Linhas M104 que vem logo antes de uma troca de extrusor (sem extrusao no meio).
    Sao o 'resfriamento do bico que vai ficar parado' que o Orca gera."""
    result = set()
    next_is_toolchange = False
    for idx in range(len(lines) - 1, -1, -1):
        cmd = strip_line(lines[idx])
        if not cmd:
            continue
        code, params = parse(cmd)
        p = {k: v for k, v in params}
        if re.fullmatch(r"T\d+", code) or (code == "M108" and p.get("T") not in (None, "")):
            next_is_toolchange = True
        elif code in ("G0", "G1") and p.get("E") not in (None, "") and float(p["E"]) > 0:
            next_is_toolchange = False
        elif code == "M104" and next_is_toolchange:
            result.add(idx + 1)   # numero de linha (1-based)
    return result


def convert(lines):
    out = []                 # comandos de saida (sem numero de linha)
    dropped = {}             # comando -> contagem
    warnings = []

    rel_e = False            # modo de extrusao do arquivo de origem
    rel_xyz = False          # G91 ativo
    src_e = 0.0              # posicao E "vista" pelo Orca (modo absoluto)
    e_abs = {0: 0.0, 1: 0.0} # contador absoluto por extrusor (firmware)
    tool = None              # extrusor ativo
    heated = set()           # extrusores que ja receberam M104 S>0
    waited = set()           # extrusores que ja tiveram M6 (espera)
    nozzle_target = {0: 0, 1: 0}

    pos = {"X": None, "Y": None, "Z": None}
    src_f = None             # F pedido pelo Orca (modal)
    out_f = None             # F efetivamente enviado (modal no firmware)
    extruded_any = False
    used_temps = prescan_temps(lines) if HOLD_TEMPS else {}
    preheated_all = not HOLD_TEMPS
    standby = standby_lines(lines) if HOLD_TEMPS else set()

    def drop(code):
        dropped[code] = dropped.get(code, 0) + 1

    def set_tool(n, explicit_m108):
        nonlocal tool
        if n not in (0, 1):
            raise ConvertError(f"Extrusor T{n} nao existe na Inventor (so T0 e T1).")
        if n != tool:
            out.append(f"M108 T{n}")
            tool = n
        # se ja era o extrusor ativo, nao repete

    for lineno, raw in enumerate(lines, 1):
        cmd = strip_line(raw)
        if not cmd:
            continue
        code, params = parse(cmd)
        pdict = {k: v for k, v in params}

        # ---- troca de extrusor -------------------------------------------
        if re.fullmatch(r"T\d+", code):
            set_tool(int(code[1:]), False)
            continue
        if code == "M108":
            if "T" in pdict and pdict["T"] != "":
                set_tool(int(float(pdict["T"])), True)
            else:
                drop(code)
            continue

        # ---- modos ---------------------------------------------------------
        if code == "M83":
            rel_e = True
            continue
        if code == "M82":
            rel_e = False
            continue
        if code == "G90":
            rel_xyz = False
            out.append("G90")
            continue
        if code == "G91":
            rel_xyz = True
            out.append("G91")
            continue
        if code == "G92":
            # Nunca manda G92 para a impressora: so atualiza a referencia interna.
            if "E" in pdict:
                src_e = float(pdict["E"] or 0)
            if any(k in pdict for k in "XYZ"):
                raise ConvertError(f"Linha {lineno}: G92 em X/Y/Z nao e suportado ({cmd}).")
            continue

        # ---- arcos ---------------------------------------------------------
        if code in ("G2", "G3"):
            raise ConvertError(
                "O G-code tem arcos (G2/G3). Desligue 'Arc fitting' (Ajuste de arco) "
                "nas configuracoes de processo e fatie de novo.")

        # ---- esperas -------------------------------------------------------
        if code == "G4":
            secs = float(pdict.get("S") or 0) + float(pdict.get("P") or 0) / 1000.0
            if secs > 0:
                warnings.append(f"Pausa G4 de {secs:.1f}s removida (linha {lineno}).")
            drop("G4")
            continue

        # ---- temperaturas --------------------------------------------------
        if code in ("M104", "M109"):
            t = int(float(pdict["T"])) if pdict.get("T") not in (None, "") else tool
            if t is None:
                t = 0
            s = int(round(float(pdict.get("S") or 0)))

            if s > 0 and not preheated_all:
                # Primeiro aquecimento do arquivo: aquece TODOS os bicos usados
                # ao mesmo tempo, cada um na temperatura definida no Orca.
                preheated_all = True
                for ot in sorted(used_temps):
                    if ot != t:
                        out.append(f"M104 S{used_temps[ot]} T{ot}")
                        nozzle_target[ot] = used_temps[ot]
                        heated.add(ot)

            if s > 0:
                if HOLD_TEMPS and extruded_any and (t != tool or lineno in standby):
                    # Bico parado: o Orca quer baixar/preaquecer, mas mantemos
                    # ele na temperatura de impressao para nao esperar na troca.
                    drop("M104(standby)")
                elif s != nozzle_target[t]:
                    out.append(f"M104 S{s} T{t}")
                    if s > nozzle_target[t]:
                        waited.discard(t)     # subiu: precisa esperar de novo
                    nozzle_target[t] = s
                heated.add(t)
            else:
                if nozzle_target[t] != 0 or not extruded_any:
                    out.append(f"M104 S0 T{t}")
                nozzle_target[t] = 0
                heated.discard(t)
                waited.discard(t)

            if code == "M109" and s > 0 and t in heated and t not in waited:
                out.append(f"M6 T{t}")
                waited.add(t)
            continue
        if code == "M6":
            t = int(float(pdict["T"])) if pdict.get("T") not in (None, "") else (tool or 0)
            if t in waited:
                continue                  # ja esta na temperatura
            out.append(f"M6 T{t}")
            if t in heated:
                waited.add(t)
            continue
        if code in ("M140", "M190"):
            s = int(round(float(pdict.get("S") or 0)))
            out.append(f"M140 S{s} T0")
            if code == "M190" and s > 0:
                out.append("M7 T0")
            continue

        # ---- ventoinha -----------------------------------------------------
        if code == "M106":
            s = pdict.get("S")
            if s not in (None, "") and float(s) <= 0:
                out.append("M107")
            else:
                out.append("M106")
            continue

        # ---- homing invalida posicao conhecida ----------------------------
        if code in ("G28", "G161", "G162", "M132"):
            axes = [k for k in "XYZ" if k in pdict] or ["X", "Y", "Z"]
            for a in axes:
                pos[a] = None
            if pdict.get("F") not in (None, ""):
                out_f = float(pdict["F"])   # firmware passa a usar esse F
            out.append(cmd.split(";")[0].strip())
            continue

        # ---- movimentos ----------------------------------------------------
        if code in ("G0", "G1"):
            if "F" in pdict and pdict["F"] not in (None, ""):
                src_f = float(pdict["F"])
            new = {}
            for a in "XYZ":
                if a in pdict and pdict[a] not in (None, ""):
                    new[a] = float(pdict[a])

            e_out = None
            if "E" in pdict and pdict["E"] not in (None, ""):
                ev = float(pdict["E"])
                delta = ev if rel_e else ev - src_e
                if not rel_e:
                    src_e = ev
                if tool is None:
                    raise ConvertError(f"Linha {lineno}: extrusao antes de escolher um extrusor (M108 Tn).")
                if delta > 1e-9:
                    if tool not in heated:
                        raise ConvertError(
                            f"Linha {lineno}: o extrusor T{tool} ia extrudir sem ter sido aquecido "
                            f"(nenhum M104 para ele). Confira o G-code inicial.")
                    if tool not in waited:
                        # temperatura subiu (ou nunca esperou): espera antes de extrudir
                        out.append(f"M6 T{tool}")
                        waited.add(tool)
                    extruded_any = True
                e_abs[tool] += delta
                e_out = e_abs[tool]

            if not new and e_out is None:
                # so F: vira estado modal, sera aplicado no proximo movimento
                continue

            # posicao alvo e checagem de limites
            target = dict(pos)
            for a, v in new.items():
                if rel_xyz:
                    target[a] = None if pos[a] is None else pos[a] + v
                else:
                    target[a] = v
            if not rel_xyz:
                if "X" in new and not (X_MIN - BOUNDS_TOL <= new["X"] <= X_MAX + BOUNDS_TOL):
                    raise ConvertError(f"Linha {lineno}: X={new['X']} fora da mesa ({X_MIN}..{X_MAX}). Confira a origem (115, 75).")
                if "Y" in new and not (Y_MIN - BOUNDS_TOL <= new["Y"] <= Y_MAX + BOUNDS_TOL):
                    raise ConvertError(f"Linha {lineno}: Y={new['Y']} fora da mesa ({Y_MIN}..{Y_MAX}). Confira a origem (115, 75).")
                if "Z" in new and new["Z"] > Z_MAX + BOUNDS_TOL:
                    raise ConvertError(f"Linha {lineno}: Z={new['Z']} acima da altura maxima ({Z_MAX}).")

            # velocidade efetiva, limitando o componente Z a 7 mm/s
            want_f = src_f
            if want_f is not None and want_f > XY_MAX_FEED:
                want_f = XY_MAX_FEED
            if "Z" in new and want_f is not None:
                dz = (target["Z"] - pos["Z"]) if (target["Z"] is not None and pos["Z"] is not None) else None
                if dz is None or abs(dz) > 1e-9:
                    if dz is None:
                        max_f = Z_MAX_FEED
                    else:
                        dxy2 = 0.0
                        for a in "XY":
                            if a in new:
                                if target[a] is None or pos[a] is None:
                                    dxy2 = 0.0   # desconhecido: assume pior caso
                                    break
                                dxy2 += (target[a] - pos[a]) ** 2
                        dist = math.sqrt(dxy2 + dz * dz)
                        max_f = Z_MAX_FEED * dist / abs(dz)
                    if want_f > max_f:
                        want_f = max_f
            elif "Z" in new and want_f is None:
                want_f = Z_MAX_FEED

            parts = ["G1"]
            for a in "XYZ":
                if a in new:
                    parts.append(a + pdict[a])
            if e_out is not None:
                parts.append("E" + fmt(e_out, 5))
            if want_f is not None and (out_f is None or abs(want_f - out_f) > 0.5):
                parts.append("F" + str(int(round(want_f))))
                out_f = float(int(round(want_f)))
            out.append(" ".join(parts))

            for a in "XYZ":
                if a in new:
                    pos[a] = target[a]
            continue

        # ---- whitelist -----------------------------------------------------
        if code in ALLOWED:
            out.append(cmd)
        else:
            drop(code)

    if not extruded_any:
        warnings.append("Nenhuma extrusao encontrada no arquivo.")
    return out, dropped, warnings, e_abs


def main():
    if len(sys.argv) < 2:
        print("uso: inventor_pp.py arquivo.gcode", file=sys.stderr)
        sys.exit(2)
    path = sys.argv[-1]
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    lines = text.splitlines()

    # Guarda uma copia do G-code original do Orca (antes da conversao) ao lado do
    # script, para diagnostico. E sobrescrita a cada exportacao.
    try:
        raw_copy = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ultimo_original_orca.gcode")
        with open(raw_copy, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
    except OSError:
        pass

    try:
        out, dropped, warnings, e_abs = convert(lines)
    except ConvertError as e:
        print("inventor_pp: " + str(e), file=sys.stderr)
        sys.exit(1)

    header = [
        ";generated by OrcaSlicer + inventor_pp.py",
        ";gcode_flavor: flashforge",
        ";machine_type: Inventor Series",
        f";filament_used_T0_mm: {e_abs[0]:.1f}",
        f";filament_used_T1_mm: {e_abs[1]:.1f}",
    ]
    if dropped:
        header.append(";removed: " + ", ".join(f"{k}x{v}" for k, v in sorted(dropped.items())))
    for w in warnings[:20]:
        header.append(";warning: " + w)

    body = [f"N{i} {c}" for i, c in enumerate(out, 1)]
    tmp = path + ".inventor_tmp"
    with open(tmp, "w", encoding="ascii", errors="replace", newline="\n") as f:
        f.write("\n".join(header + body) + "\n")
    os.replace(tmp, path)


if __name__ == "__main__":
    main()
