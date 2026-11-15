"""Fala com a Flashforge Inventor pelo USB (driver WinUSB do FlashPrint), só com a stdlib.

Uso:  py ff_usb.py                 -> M115, M105, M119 e M27 (só leitura)
      py ff_usb.py "~M105" ...     -> manda os comandos dados
      py ff_usb.py --monitor       -> a cada 10 s: byte atual/total do arquivo, estado e temperaturas
                                      (tela e monitor.log). Ctrl+C para parar.
O FlashPrint precisa estar desconectado da impressora: o WinUSB só deixa um programa por vez.
"""
import ctypes as C
import re
import sys
import time
import winreg
from ctypes import wintypes as W

VIDPID = r"VID_2B71&PID_0005"
LEITURA = ["~M115", "~M105", "~M119", "~M27"]

k32 = C.WinDLL("kernel32", use_last_error=True)
wu = C.WinDLL("winusb", use_last_error=True)
k32.CreateFileW.restype = W.HANDLE
k32.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
for f in ("WinUsb_Initialize", "WinUsb_SetPipePolicy", "WinUsb_WritePipe", "WinUsb_ReadPipe", "WinUsb_Free"):
    getattr(wu, f).restype = W.BOOL


def caminho():
    """Monta o caminho do aparelho a partir do registro (serial + GUID da interface)."""
    base = rf"SYSTEM\CurrentControlSet\Enum\USB\{VIDPID}"
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as k:
        serial = winreg.EnumKey(k, 0)
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"{base}\{serial}\Device Parameters") as k:
        try:
            guid = winreg.QueryValueEx(k, "DeviceInterfaceGUIDs")[0][0]
        except FileNotFoundError:
            guid = winreg.QueryValueEx(k, "DeviceInterfaceGUID")[0]
    return rf"\\?\USB#{VIDPID}#{serial}#{guid}"


class Impressora:
    def __init__(self):
        h = k32.CreateFileW(caminho(), 0xC0000000, 3, None, 3, 0x40000080, None)  # RW, OVERLAPPED
        if h in (None, W.HANDLE(-1).value):
            raise OSError(f"não abriu o USB (erro {C.get_last_error()}). O FlashPrint está conectado?")
        self.h, self.wh = h, W.HANDLE()
        if not wu.WinUsb_Initialize(h, C.byref(self.wh)):
            raise OSError(f"WinUsb_Initialize falhou (erro {C.get_last_error()})")
        # 4 endpoints bulk: comandos em 0x01 -> 0x81. O 0x83 nunca respondeu.
        self.ep_out, self.ep_in = 0x01, 0x81
        t = W.ULONG(2000)  # PIPE_TRANSFER_TIMEOUT em ms
        wu.WinUsb_SetPipePolicy(self.wh, self.ep_in, 0x03, 4, C.byref(t))
        # na escrita também: se a placa parar de ler o USB, vira erro em vez de travar o programa
        wu.WinUsb_SetPipePolicy(self.wh, self.ep_out, 0x03, 4, C.byref(t))

    def comando(self, cmd, espera=30):
        """espera: segundos para aguardar o ok depois do "Received" (o G1 depois de um M6/M7 só responde
        quando aquece; um M104 atrás da fila de movimentos, quando a fila anda). Não encurte: mandar outro
        comando antes desse ok trava o USB da placa (15/11/2026, duas vezes)."""
        dados = (cmd.strip() + "\r\n").encode()
        n = W.ULONG()
        if not wu.WinUsb_WritePipe(self.wh, self.ep_out, dados, len(dados), C.byref(n), None):
            raise OSError(f"escrita falhou (erro {C.get_last_error()}): a placa parou de ler o USB?")
        resp, buf, fim = b"", C.create_string_buffer(4096), time.time() + espera
        while not resp.rstrip().endswith(b"ok"):
            if not wu.WinUsb_ReadPipe(self.wh, self.ep_in, buf, len(buf), C.byref(n), None):
                if C.get_last_error() == 121 and resp and time.time() < fim:
                    continue  # já veio o "Received": o ok sai quando o comando entrar na fila
                break  # devolve o que chegou. Vazio = sem controle: ela ignorou o comando
            resp += buf.raw[:n.value]
        return resp.decode(errors="replace").replace("\r\n", "\n").strip()

    def fechar(self):
        wu.WinUsb_Free(self.wh)
        k32.CloseHandle(self.h)


def monitorar(p):
    """Registra o progresso em bytes: se a impressão parar, a última linha diz em que byte foi."""
    with open("monitor.log", "a", encoding="utf-8") as log:
        while True:
            # a impressora solta o controle sozinha depois de uns segundos sem comando: pega de novo a cada ciclo
            r = " ".join(p.comando(c) for c in ("~M601 S0", "~M27", "~M119", "~M105"))
            byte = re.search(r"byte (\d+)/(\d+)", r)
            est = re.search(r"MachineStatus: (\S+)", r)
            temp = re.search(r"T0:\S+ /\S+ T1:\S+ /\S+ B:\S+ /\S+", r)
            linha = (f"{time.strftime('%d/%m %H:%M:%S')}  {est and est[1]}  "
                     f"byte {byte and byte[1]}/{byte and byte[2]}  {temp and temp[0]}")
            print(linha, flush=True)
            log.write(linha + "\n")
            log.flush()
            time.sleep(10)


if __name__ == "__main__":
    p = Impressora()
    try:
        p.comando("~M601 S0")  # pega o controle (S0 = USB; S1 é pela rede)
        if sys.argv[1:] == ["--monitor"]:
            monitorar(p)
        for cmd in sys.argv[1:] or LEITURA:
            print(f"\n>>> {cmd}\n{p.comando(cmd)}")
    except KeyboardInterrupt:
        pass
    finally:
        p.comando("~M602")  # solta o controle
        p.fechar()
