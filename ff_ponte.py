"""Ponte Orca -> USB, com painel em http://localhost:8001.

Finge ser a impressora em 127.0.0.1:8899 para o envio "Flashforge" do Orca: recebe o arquivo
(~M28 tamanho 0:/user/nome + bytes crus + ~M29), salva em recebidos/ e, no ~M23, imprime pelo USB
(ff_stream.py) aqui dentro. A página mostra o progresso e tem pausar, continuar e cancelar.
Uso: py -3.14 ff_ponte.py   (deixe a janela aberta enquanto usa o Orca; Ctrl+C cancela e fecha)
"""
import http.server
import json
import pathlib
import re
import socketserver
import threading
import time

import ff_stream

PASTA = pathlib.Path(__file__).parent
RECEBIDOS = PASTA / "recebidos"
ESTADO = {"situacao": "esperando um arquivo do Orca"}
PARAR, PAUSA = threading.Event(), threading.Event()
trabalho = None  # thread da impressão em andamento
RESPOSTAS = {  # o Orca só confere a linha "ok"; o resto é para parecer a impressora
    "M601": "Control Success.",
    "M115": "Machine Type: Flashforge Inventor\r\nMachine Name: Ponte USB\r\nFirmware: V1.9 20191123\r\nX: 230  Y: 150  Z: 160\r\nTool Count: 2",
    "M119": "Endstop: X-max: 0 Y-max: 0 Z-min: 0\r\nMachineStatus: READY\r\nMoveMode: READY",
    "M27": "SD printing byte 0/0",
    "M105": "T0:0 /0 T1:0 /0 B:0 /0",
}


def nome_seguro(caminho):
    return re.sub(r"[^A-Za-z0-9._-]", "_", caminho.split("/")[-1]) or "sem_nome.g"


def imprimindo():
    return trabalho is not None and trabalho.is_alive()


def comecar(arq):
    global trabalho
    if imprimindo():
        ff_stream.diz(f"Já tem uma impressão rodando: {arq.name} ficou só salvo.")
        return
    PARAR.clear()
    PAUSA.clear()
    trabalho = threading.Thread(target=ff_stream.imprimir, args=(arq,),
                                kwargs=dict(estado=ESTADO, parar=PARAR, pausa=PAUSA), daemon=True)
    trabalho.start()


class Orca(socketserver.StreamRequestHandler):
    def handle(self):
        while linha := self.rfile.readline():
            cmd = linha.decode(errors="replace").strip().lstrip("~")
            if not cmd:
                continue
            codigo = cmd.split()[0]
            extra = RESPOSTAS.get(codigo, "")
            if codigo == "M28":
                _, tamanho, destino = cmd.split(maxsplit=2)
                extra = f"Writing to file: {destino}"
            self.wfile.write(f"CMD {codigo} Received.\r\n{extra + chr(13) + chr(10) if extra else ''}ok\r\n".encode())
            if codigo == "M28":  # o Orca só manda os bytes depois do ok
                dados = self.rfile.read(int(tamanho))
                arq = RECEBIDOS / nome_seguro(destino)
                RECEBIDOS.mkdir(exist_ok=True)
                arq.write_bytes(dados)
                ff_stream.diz(f"{time.strftime('%H:%M:%S')}  recebido {arq.name} ({len(dados)} de {tamanho} bytes)")
            elif codigo == "M23":
                comecar(RECEBIDOS / nome_seguro(cmd.split(maxsplit=1)[1]))


PAGINA = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Impressora</title>
<style>
body{font:16px system-ui;max-width:640px;margin:20px auto;padding:0 16px;background:#16181c;color:#e8e8e8}
.barra{height:22px;background:#2a2d33;border-radius:6px;overflow:hidden}.barra div{height:100%;background:#3fb27f}
.grade{display:grid;grid-template-columns:1fr 1fr;gap:6px 16px;margin:14px 0}.grade b{color:#9aa}
button{font-size:16px;padding:8px 16px;margin-right:8px;border:0;border-radius:6px;cursor:pointer}
#cancelar{background:#c0392b;color:#fff}pre{background:#0e0f12;padding:10px;font-size:13px;white-space:pre-wrap}
</style>
<h2 id="arquivo">Impressora</h2>
<div class="barra"><div id="progresso" style="width:0"></div></div>
<div class="grade">
<b>Situação</b><span id="situacao"></span><b>Progresso</b><span id="pct"></span>
<b>Altura</b><span id="z"></span><b>Tempo</b><span id="tempo"></span>
<b>Faltam</b><span id="falta"></span><b>Temperaturas</b><span id="temp"></span>
</div>
<button onclick="post('pausar')">Pausar</button><button onclick="post('continuar')">Continuar</button>
<button id="cancelar" onclick="confirm('Cancelar a impressão? Desliga o aquecimento.')&&post('cancelar')">Cancelar</button>
<pre id="log"></pre>
<script>
const m=s=>s==null?"—":Math.floor(s/60)+" min";
function post(a){fetch("/"+a,{method:"POST"}).then(atualiza)}
async function atualiza(){
 const e=await (await fetch("/estado")).json();
 arquivo.textContent=e.arquivo||"Impressora";
 const pct=e.total?100*e.i/e.total:0; progresso.style.width=pct+"%";
 pct_=e.total?`${pct.toFixed(1)}% (${e.i}/${e.total})`:"—"; document.getElementById("pct").textContent=pct_;
 situacao.textContent=e.esperando_s>5?`${e.situacao}: esperando a impressora (homing ou aquecimento) há ${Math.round(e.esperando_s)} s. Cancelar só vale depois da espera; se for urgente, desligue na chave`:e.situacao;
 z.textContent=e.z==null?"—":e.z+" mm"; tempo.textContent=m(e.decorrido); falta.textContent=m(e.falta);
 temp.textContent=(e.temp||"—").replace(/ \\//g,"/"); log.textContent=e.log.join("\\n");
}
atualiza(); setInterval(atualiza,2000);
</script>"""


class Painel(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def responder(self, corpo, tipo):
        self.send_response(200)
        self.send_header("Content-Type", tipo)
        self.end_headers()
        self.wfile.write(corpo.encode())

    def do_GET(self):
        if self.path == "/estado":
            e, agora = ESTADO, time.time()
            self.responder(json.dumps({
                "arquivo": e.get("arquivo"), "i": e.get("i", 0), "total": e.get("total", 0),
                "situacao": e["situacao"], "z": e.get("z"), "temp": e.get("temp"),
                "decorrido": agora - e["inicio"] if "inicio" in e and imprimindo() else None,
                "falta": ff_stream.falta(e) if imprimindo() else None,
                "esperando_s": agora - e["esperando"] if e.get("esperando") else 0,
                "log": list(ff_stream.ULTIMAS)}), "application/json")
        else:
            self.responder(PAGINA, "text/html; charset=utf-8")

    def do_POST(self):
        if imprimindo():
            {"/pausar": PAUSA.set, "/continuar": PAUSA.clear, "/cancelar": PARAR.set}.get(self.path, lambda: None)()
        self.responder("ok", "text/plain")


if __name__ == "__main__":
    orca = socketserver.ThreadingTCPServer(("127.0.0.1", 8899), Orca)
    painel = http.server.ThreadingHTTPServer(("127.0.0.1", 8001), Painel)
    threading.Thread(target=orca.serve_forever, daemon=True).start()
    print("Ponte em 127.0.0.1:8899 (Orca: host Flashforge, 127.0.0.1). Painel: http://localhost:8001", flush=True)
    try:
        painel.serve_forever()
    except KeyboardInterrupt:
        if imprimindo():
            PARAR.set()
            trabalho.join(30)  # espera desligar o aquecimento antes de fechar
