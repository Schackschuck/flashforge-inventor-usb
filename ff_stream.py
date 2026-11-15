"""Imprime um G-code mandando linha a linha pelo USB, sem gravar na memória interna da impressora.

Uso:  py -3.14 ff_stream.py arquivo.g          -> imprime
      py -3.14 ff_stream.py arquivo.g --ar     -> "imprime no ar": Z +30 mm, sem aquecer nem extrudar
Rode numa janela própria e com o FlashPrint fechado. Ctrl+C cancela.
O normal é imprimir pelo Orca, que manda para a ff_ponte.py (com a página em http://localhost:8001).

Regra do firmware (15/11/2026): um comando por vez. Mandar outra coisa enquanto um comando espera
(ex.: G1 parado atrás de um M6 aquecendo) enche a fila do USB e trava o USB da placa até replugar.
"""
import collections
import ctypes
import os
import pathlib
import re
import socket
import sys
import time

from ff_usb import Impressora

# ~M601 S0 primeiro: depois de uma espera longa o controle pode ter expirado, e sem ele tudo é ignorado
PARAR = ["~M601 S0", "~M104 S0 T0", "~M104 S0 T1", "~M140 S0 T0", "~M107", "~G162 Z F1800"]
WIFI_IP = os.environ.get("FF_WIFI_IP")  # opcional: plano B pela rede (porta 8899) se o USB travar
Z_AR = 30.0
Z_PAUSA = 3  # mm que a mesa desce ao pausar, para o bico não derreter a peça
LOG = pathlib.Path(__file__).with_name("impressao.log")
ULTIMAS = collections.deque(maxlen=40)  # últimas linhas do log, para a página


def diz(msg):
    """Mostra na janela, grava no impressao.log e guarda para a página."""
    print(msg, flush=True)
    ULTIMAS.append(msg)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(msg + "\n")


def linhas(caminho, ar=False):
    for l in open(caminho, encoding="ascii", errors="replace"):
        l = re.sub(r"^N\d+\s+", "", l.split(";", 1)[0].strip())
        if not l:
            continue
        if ar:
            codigo = l.split()[0]
            if codigo in ("M104", "M140", "M6", "M7"):
                continue
            if codigo == "G1":
                l = re.sub(r"\s*E[-\d.]+", "", l)
                l = re.sub(r"Z([-\d.]+)", lambda m: f"Z{float(m[1]) + Z_AR:g}", l)
                z = re.search(r"Z([-\d.]+)", l)
                if z and float(z[1]) > 160:
                    raise SystemExit(f"--ar: Z {z[1]} passa da altura máxima (160). Use uma peça mais baixa.")
                if l == "G1":
                    continue
        yield l


def parar_pela_rede():
    if not WIFI_IP:
        raise OSError("sem plano B: defina FF_WIFI_IP com o IP da impressora")
    with socket.create_connection((WIFI_IP, 8899), 5) as s:
        s.settimeout(3)
        for c in ["~M601 S1", *PARAR[1:], "~M602"]:
            s.sendall((c + "\r\n").encode())
            s.recv(4096)


def imprimir(caminho, ar=False, estado=None, parar=None, pausa=None):
    """estado (dict) é atualizado para a página; parar e pausa são threading.Event (None = só Ctrl+C)."""
    e = estado if estado is not None else {}
    cmds = list(linhas(caminho, ar))
    e.update(arquivo=pathlib.Path(caminho).name, total=len(cmds), i=0, inicio=time.time(), z=None,
             temp="", situacao="imprimindo", esperando=None, ritmo=collections.deque(maxlen=60))
    diz(f"\n{time.strftime('%d/%m %H:%M:%S')}  {len(cmds)} comandos de {caminho}" + (" (no ar)" if ar else ""))
    ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | 0x00000001)  # não deixa o PC dormir
    p = Impressora()

    def enviar(c, i=0):
        e["esperando"] = time.time()
        r = p.comando(c, espera=1800)  # aquecer a mesa pode levar minutos
        if not r:  # o controle expira se ela passa uns segundos sem comando novo (homing, aquecer)
            if i:  # i=0: M105 da página, não vale uma linha no log
                diz(f"  (comando {i}: retomando o controle)")
            p.comando("~M601 S0")  # sem controle ela ignora calada, então reenviar é seguro
            r = p.comando(c, espera=1800)
        e["esperando"] = None
        if not r.endswith("ok"):
            raise RuntimeError(f"comando {i} ({c}) sem ok: {r!r}")
        return r

    def ler_temp():  # só entre comandos, nunca durante uma espera
        t = re.search(r"T0:.*", enviar("~M105"))
        e["temp"] = t[0] if t else ""

    try:
        enviar("~M601 S0")
        ultima_temp = 0
        for i, c in enumerate(cmds, 1):
            if parar and parar.is_set():
                raise KeyboardInterrupt("cancelado na página")
            if pausa and pausa.is_set():
                e["situacao"] = "pausado"
                diz(f"{time.strftime('%H:%M:%S')}  pausado no comando {i}; a mesa desce {Z_PAUSA} mm")
                for x in ("~G91", f"~G1 Z{Z_PAUSA} F420", "~G90"):
                    enviar(x)
                while pausa.is_set() and not parar.is_set():
                    ler_temp()  # mantém o controle e a página atualizada
                    time.sleep(2)
                if parar.is_set():
                    raise KeyboardInterrupt("cancelado na página")
                for x in ("~G91", f"~G1 Z-{Z_PAUSA} F420", "~G90"):
                    enviar(x)
                diz(f"{time.strftime('%H:%M:%S')}  continuando")
                e["situacao"] = "imprimindo"
            enviar("~" + c, i)
            e["i"] = i
            e["ritmo"].append((time.time(), i))
            if z := re.search(r"^G1 .*Z([-\d.]+)", c):
                e["z"] = float(z[1])
            if time.time() - ultima_temp > 3:
                ler_temp()
                ultima_temp = time.time()
            if i % 2000 == 0 or i == len(cmds):
                dt = time.time() - e["inicio"]
                diz(f"{time.strftime('%H:%M:%S')}  {i}/{len(cmds)} ({100 * i / len(cmds):.1f}%)  {dt / 60:.0f} min")
        e["situacao"] = "terminado"
        diz(f"{time.strftime('%H:%M:%S')}  Enviado até o fim. Os últimos movimentos ainda estão na fila da impressora.")
    except BaseException as erro:  # inclui Ctrl+C
        e["situacao"] = "cancelado" if isinstance(erro, KeyboardInterrupt) else f"erro: {erro}"
        diz(f"\nPARANDO: {erro!r}")
        try:
            for c in PARAR:
                p.comando(c)
            diz("Aquecedores desligados pelo USB.")
        except OSError:
            diz("USB não responde. Tentando desligar pela rede...")
            try:
                parar_pela_rede()
                diz("Aquecedores desligados pela rede.")
            except OSError as e2:
                diz(f"A REDE TAMBÉM FALHOU ({e2}). DESLIGUE A IMPRESSORA NA CHAVE.")
        if estado is None:
            raise
    finally:
        e["esperando"] = None
        try:
            p.comando("~M602")
        except OSError:
            pass
        p.fechar()


def falta(e):
    """Segundos que faltam, pelo ritmo do último minuto (o começo tem aquecimento e engana)."""
    r = e.get("ritmo")
    if not r or len(r) < 2 or r[-1][1] == r[0][1]:
        return None
    (t0, i0), (t1, i1) = r[0], r[-1]
    return (e["total"] - i1) * (t1 - t0) / (i1 - i0)


def teste():
    import os, tempfile
    g = "N1 M140 S65 T0\nN2 M6 T0 ; espera\n\n;só comentário\nN3 G1 Z.2 F420\nN4 G1 X1 Y2 E0.5\nN5 G1 E-1 F1800\nN6 G162 Z F1800\n"
    with tempfile.NamedTemporaryFile("w", suffix=".g", delete=False) as f:
        f.write(g)
    assert list(linhas(f.name)) == ["M140 S65 T0", "M6 T0", "G1 Z.2 F420", "G1 X1 Y2 E0.5", "G1 E-1 F1800", "G162 Z F1800"]
    assert list(linhas(f.name, ar=True)) == ["G1 Z30.2 F420", "G1 X1 Y2", "G1 F1800", "G162 Z F1800"]
    os.remove(f.name)
    assert falta({"total": 100, "ritmo": [(0, 10), (10, 20)]}) == 80  # 1 comando/s, faltam 80
    assert falta({"total": 100, "ritmo": [(0, 10)]}) is None
    print("teste ok")


if __name__ == "__main__":
    if sys.argv[1:] == ["--teste"]:
        teste()
    elif len(sys.argv) >= 2:
        imprimir(sys.argv[1], "--ar" in sys.argv)
    else:
        print(__doc__)
