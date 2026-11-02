"""
Afinidade de CPU, prioridade, som e Special K (segundo plano + controles) por programa (Windows)

Aba "Jogos": detecta jogos instalados (Steam, Epic, Xbox) e inicia cada um já com um perfil
salvo (com nome): núcleos/prioridade da CPU, saída de som, tela onde o jogo abre e Special K
(segundo plano e qual controle é o Jogador 1, escolhido pelo nome do controle).

Ao abrir, o programa pede permissão de administrador e verifica os
componentes necessários, oferecendo baixar/instalar o que faltar:
  - psutil          (biblioteca Python, instalada com pip)
  - SoundVolumeView (NirSoft)  -> salvo na MESMA pasta deste script
  - Special K                  -> via winget ou página oficial
"""
import ctypes
import csv
import json
import os
import platform
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import webbrowser
import winreg
import xml.etree.ElementTree as ET
import zipfile
import tkinter as tk
from tkinter import ttk, messagebox, filedialog

PASTA = os.path.dirname(os.path.abspath(__file__))
SEM_JANELA = 0x08000000   # CREATE_NO_WINDOW
NOVO_CONSOLE = 0x00000010  # CREATE_NEW_CONSOLE


# ------------------------------------------------ administrador (primeiro de tudo)
def e_administrador():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


if __name__ == "__main__" and not e_administrador():
    # Reabre o próprio script pedindo permissão de administrador (janela do UAC)
    _args = " ".join(f'"{a}"' for a in [os.path.abspath(__file__)] + sys.argv[1:])
    ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, _args, PASTA, 1)
    sys.exit()


# ------------------------------------------------ psutil (biblioteca Python)
def _garantir_psutil():
    try:
        import psutil  # noqa: F401
        return
    except ImportError:
        pass
    raiz = tk.Tk()
    raiz.withdraw()
    if not messagebox.askyesno(
            "Componente necessário",
            "A biblioteca 'psutil' não está instalada.\n\n"
            "Instalar agora? (usa o pip e precisa de internet)"):
        sys.exit()
    python = sys.executable
    if python.lower().endswith("pythonw.exe"):
        python = python[:-len("pythonw.exe")] + "python.exe"
    r = subprocess.run([python, "-m", "pip", "install", "psutil"],
                       capture_output=True, text=True, creationflags=SEM_JANELA)
    if r.returncode != 0:
        messagebox.showerror("Erro", "Não consegui instalar o psutil.\n\n"
                                     "Abra um terminal e rode: pip install psutil\n\n"
                                     + (r.stderr or "")[-500:])
        sys.exit()
    raiz.destroy()
    import importlib
    importlib.invalidate_caches()


_garantir_psutil()
import psutil  # noqa: E402

user32 = ctypes.windll.user32
EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

SVV = os.path.join(PASTA, "SoundVolumeView.exe")
ARQ_CONFIG = os.path.join(PASTA, "config_afinidade.json")
PADRAO_WINDOWS = "DefaultRenderDevice"
UA = {"User-Agent": "Mozilla/5.0 (afinidade_jogos)"}

PRIORIDADES = [
    ("Baixa", psutil.IDLE_PRIORITY_CLASS),
    ("Abaixo do normal", psutil.BELOW_NORMAL_PRIORITY_CLASS),
    ("Normal", psutil.NORMAL_PRIORITY_CLASS),
    ("Acima do normal", psutil.ABOVE_NORMAL_PRIORITY_CLASS),
    ("Alta", psutil.HIGH_PRIORITY_CLASS),
]
NOME_PRIORIDADE = {valor: nome for nome, valor in PRIORIDADES}

# Processos comuns que não são jogos (só escondidos quando a lista é "só com janela")
IGNORAR = {
    "explorer.exe", "applicationframehost.exe", "textinputhost.exe",
    "systemsettings.exe", "searchhost.exe", "startmenuexperiencehost.exe",
    "shellexperiencehost.exe", "taskmgr.exe", "cmd.exe", "powershell.exe",
    "windowsterminal.exe", "python.exe", "pythonw.exe", "chrome.exe",
    "msedge.exe", "firefox.exe", "discord.exe", "code.exe", "steamwebhelper.exe",
    "epicwebhelper.exe", "nvidia overlay.exe", "widgets.exe",
}

# Pedaços de caminho típicos de launchers/jogos
PASTAS_JOGOS = ("steamapps", "epic games", "gog galaxy", "riot games",
                "battle.net", "ubisoft", "origin games", "ea games",
                "xboxgames", "games")


# ================================================================ localização
def pasta_documentos():
    buf = ctypes.create_unicode_buffer(260)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 and buf.value:
        return buf.value
    return os.path.join(os.path.expanduser("~"), "Documents")


def pasta_specialk_padrao():
    return os.path.join(pasta_documentos(), "My Mods", "SpecialK")


SK_UNINSTALL = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
                r"\{F4A43527-9457-424A-90A6-17CF02ACF677}_is1")


def pastas_specialk():
    """Pastas onde o Special K pode estar instalado (registro + padrão)."""
    pastas = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for flag in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, SK_UNINSTALL, 0, winreg.KEY_READ | flag) as k:
                    try:
                        pastas.append(winreg.QueryValueEx(k, "InstallLocation")[0])
                    except OSError:
                        pass
            except OSError:
                continue
    pastas.append(pasta_specialk_padrao())
    return [p for p in pastas if p]


def achar_specialk():
    """Retorna (instalado, caminho_do_SKIF.exe ou None)."""
    instalado = False
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for flag in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, SK_UNINSTALL, 0, winreg.KEY_READ | flag):
                    instalado = True
            except OSError:
                continue
    for p in pastas_specialk():
        skif = os.path.join(p, "SKIF.exe")
        if os.path.exists(skif):
            return True, skif
    return instalado, None


def pasta_instalacao_specialk():
    for p in pastas_specialk():
        if os.path.exists(os.path.join(p, "SKIF.exe")):
            return p
    return pasta_specialk_padrao()


def pasta_perfis_specialk():
    for p in pastas_specialk():
        perfis = os.path.join(p, "Profiles")
        if os.path.isdir(perfis):
            return perfis
    return os.path.join(pasta_specialk_padrao(), "Profiles")


INIS_LOCAIS = ("SpecialK.ini", "dxgi.ini", "d3d11.ini", "d3d9.ini", "d3d8.ini",
               "ddraw.ini", "dinput8.ini", "OpenGL32.ini")


def achar_inis_specialk(exe):
    """Possíveis .ini do jogo: perfil global, perfil convertido e instalação local."""
    achados = []
    pasta_perfil = os.path.join(pasta_perfis_specialk(), os.path.basename(exe))
    perfil = os.path.join(pasta_perfil, "SpecialK.ini")
    for nome in INIS_LOCAIS:
        p = os.path.join(pasta_perfil, nome)
        if os.path.exists(p):
            achados.append(p)
    for nome in INIS_LOCAIS[1:]:
        p = os.path.join(os.path.dirname(exe), nome)
        if os.path.exists(p):
            achados.append(p)
    return achados, perfil


def escolher_ini(exe):
    """(caminho, existe). Se nenhum existir, devolve o caminho padrão do perfil global."""
    achados, perfil = achar_inis_specialk(exe)
    return (achados[0], True) if achados else (perfil, False)


# ================================================================ Special K: serviço de injeção e whitelist
def achar_skifsvc():
    """Caminhos de SKIFsvc32/64.exe dentro da pasta do Special K."""
    base = pasta_instalacao_specialk()
    encontrados = {}
    if not os.path.isdir(base):
        return []
    nivel_base = base.rstrip("\\/").count(os.sep)
    for raiz, dirs, arqs in os.walk(base):
        dirs[:] = [d for d in dirs if d.lower() not in ("profiles", "global", "plugins")]
        if raiz.count(os.sep) - nivel_base >= 3:
            dirs[:] = []
        for a in arqs:
            if a.lower() in ("skifsvc64.exe", "skifsvc32.exe"):
                encontrados[a.lower()] = os.path.join(raiz, a)
    return list(encontrados.values())


def iniciar_servicos(servicos):
    for svc in servicos:
        subprocess.Popen([svc, "Start"], cwd=os.path.dirname(svc), creationflags=SEM_JANELA)


def parar_servicos(servicos):
    for svc in servicos:
        try:
            subprocess.run([svc, "Stop"], cwd=os.path.dirname(svc),
                           creationflags=SEM_JANELA, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            pass


def adicionar_whitelist(nome_exe):
    """Inclui o .exe em Global\\whitelist.ini (injeção global). True se adicionou."""
    pasta = os.path.join(pasta_instalacao_specialk(), "Global")
    os.makedirs(pasta, exist_ok=True)
    arq = os.path.join(pasta, "whitelist.ini")
    linhas = []
    if os.path.exists(arq):
        with open(arq, encoding="utf-8", errors="replace") as f:
            linhas = f.read().splitlines()
        if any(l.strip().lower() == nome_exe.lower() for l in linhas):
            return False
        bak = arq + ".bak_afinidade"
        if not os.path.exists(bak):
            shutil.copy2(arq, bak)
    linhas.append(nome_exe)
    with open(arq, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(linhas) + "\r\n")
    return True


def specialk_injetado(pid):
    """True/False se o Special K está carregado no processo; None se não deu para ler."""
    try:
        for m in psutil.Process(pid).memory_maps(grouped=True):
            if "specialk" in (m.path or "").lower():
                return True
        return False
    except (psutil.AccessDenied, psutil.NoSuchProcess, OSError):
        return None


# ================================================================ editor de .ini (preserva o resto do arquivo)
def _ler_ini(caminho):
    with open(caminho, "rb") as f:
        bruto = f.read()
    enc = "utf-8"
    if bruto.startswith(b"\xef\xbb\xbf"):
        enc = "utf-8-sig"
    elif bruto.startswith((b"\xff\xfe", b"\xfe\xff")):
        enc = "utf-16"
    texto = bruto.decode(enc, errors="replace")
    nl = "\r\n" if "\r\n" in texto else "\n"
    return texto, enc, nl


def ler_valor_ini(caminho, secao, chave):
    """Valor de [secao] chave=..., ou None."""
    try:
        texto, _, _ = _ler_ini(caminho)
    except OSError:
        return None
    atual = None
    for linha in texto.splitlines():
        s = linha.strip()
        if s.startswith("[") and s.endswith("]"):
            atual = s[1:-1].strip().lower()
        elif atual == secao.lower() and "=" in s:
            k, v = s.split("=", 1)
            if k.strip().lower() == chave.lower():
                return v.strip()
    return None


def escrever_edicoes(caminho, edicoes):
    """Aplica [(secao, chave, valor), ...] mantendo o resto do arquivo. Faz backup do original."""
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    if os.path.exists(caminho):
        texto, enc, nl = _ler_ini(caminho)
        bak = caminho + ".bak_afinidade"
        if not os.path.exists(bak):
            shutil.copy2(caminho, bak)
    else:
        texto, enc, nl = "", "utf-8", "\r\n"
    linhas = texto.splitlines()
    for secao, chave, valor in edicoes:
        i_sec = None
        for i, l in enumerate(linhas):
            if l.strip().lower() == f"[{secao.lower()}]":
                i_sec = i
                break
        if i_sec is None:
            if linhas and linhas[-1].strip():
                linhas.append("")
            linhas += [f"[{secao}]", f"{chave}={valor}"]
            continue
        fim = len(linhas)
        for j in range(i_sec + 1, len(linhas)):
            if linhas[j].strip().startswith("["):
                fim = j
                break
        achou = False
        for j in range(i_sec + 1, fim):
            partes = linhas[j].split("=", 1)
            if len(partes) == 2 and partes[0].strip().lower() == chave.lower():
                linhas[j] = f"{partes[0]}={valor}"
                achou = True
                break
        if not achou:
            pos = fim
            while pos > i_sec + 1 and not linhas[pos - 1].strip():
                pos -= 1
            linhas.insert(pos, f"{chave}={valor}")
    with open(caminho, "wb") as f:
        f.write((nl.join(linhas) + nl).encode(enc))


# --- troca de controles: [Input.XInput] SlotReassignment=a,b,c,d
def reatribuicao_para(n):
    """Troca o controle 1 com o controle n (0..3). A troca é simétrica, então vale
    qualquer que seja o sentido em que o Special K lê a lista."""
    v = [0, 1, 2, 3]
    v[0], v[n] = v[n], v[0]
    return v


def jogador1_de(lista_texto):
    """Inverso de reatribuicao_para: devolve n, ou None se for uma lista personalizada."""
    try:
        vals = [int(x) for x in lista_texto.split(",")]
    except (ValueError, AttributeError):
        return None
    for n in range(4):
        if vals == reatribuicao_para(n):
            return n
    return None


def edicoes_specialk(segundo_plano, idx_j1):
    edicoes = [["Window.System", "RenderInBackground", "true" if segundo_plano else "false"]]
    if idx_j1 is not None and idx_j1 < 4:
        edicoes.append(["Input.XInput", "SlotReassignment",
                        ",".join(map(str, reatribuicao_para(idx_j1)))])
    return edicoes


def jogo_em_execucao(exe):
    nome = os.path.basename(exe).lower()
    for p in psutil.process_iter(["name", "exe"]):
        if (p.info["name"] or "").lower() == nome:
            e = p.info["exe"]
            if not e or e.lower() == exe.lower():
                return True
    return False


# ================================================================ telas (monitores) e janelas
class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT), ("rcWork", RECT),
                ("dwFlags", ctypes.c_ulong), ("szDevice", ctypes.c_wchar * 32)]


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("DeviceName", ctypes.c_wchar * 32),
                ("DeviceString", ctypes.c_wchar * 128), ("StateFlags", ctypes.c_ulong),
                ("DeviceID", ctypes.c_wchar * 128), ("DeviceKey", ctypes.c_wchar * 128)]


MonitorEnumProc = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.POINTER(RECT), ctypes.c_void_p)

user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(MONITORINFOEXW)]
user32.EnumDisplayDevicesW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong,
                                       ctypes.POINTER(DISPLAY_DEVICEW), ctypes.c_ulong]
user32.EnumDisplayMonitors.argtypes = [ctypes.c_void_p, ctypes.c_void_p, MonitorEnumProc,
                                       ctypes.c_void_p]
user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(RECT)]
user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.IsZoomed.argtypes = [ctypes.c_void_p]
user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]


def _com_dpi_fisico(funcao):
    """Roda `funcao` com coordenadas físicas (pixels reais) nesta thread."""
    set_ctx, antigo = None, None
    try:
        set_ctx = user32.SetThreadDpiAwarenessContext
        set_ctx.restype = ctypes.c_void_p
        set_ctx.argtypes = [ctypes.c_void_p]
        antigo = set_ctx(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except (AttributeError, OSError):
        set_ctx = None
    try:
        return funcao()
    finally:
        if set_ctx and antigo:
            set_ctx(antigo)


def _listar_telas_fisicas():
    brutas = []

    def cb(hmon, hdc, lprect, lparam):
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
            r = info.rcMonitor
            dd = DISPLAY_DEVICEW()
            dd.cb = ctypes.sizeof(DISPLAY_DEVICEW)
            nome = dd.DeviceString if user32.EnumDisplayDevicesW(
                info.szDevice, 0, ctypes.byref(dd), 0) else ""
            brutas.append({"dispositivo": info.szDevice, "monitor": nome,
                           "x": r.left, "y": r.top, "w": r.right - r.left,
                           "h": r.bottom - r.top, "principal": bool(info.dwFlags & 1)})
        return 1

    user32.EnumDisplayMonitors(None, None, MonitorEnumProc(cb), 0)
    brutas.sort(key=lambda t: t["dispositivo"])
    for i, t in enumerate(brutas, 1):
        m = re.search(r"DISPLAY(\d+)", t["dispositivo"])
        n = int(m.group(1)) if m else i
        t["rotulo"] = (f"Tela {n}: {t['monitor'] or 'Monitor'} — {t['w']}x{t['h']}"
                       + (" (principal)" if t["principal"] else ""))
    return brutas


def listar_telas():
    return _com_dpi_fisico(_listar_telas_fisicas)


SWP_NOSIZE, SWP_NOZORDER, SWP_NOACTIVATE = 0x0001, 0x0004, 0x0010


def melhor_janela(pids):
    """Maior janela visível (não ferramenta) dos processos indicados."""
    achadas = []

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids and not (user32.GetWindowLongW(hwnd, -20) & 0x80):
                r = RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(r))
                w, h = r.right - r.left, r.bottom - r.top
                if w >= 300 and h >= 200:
                    achadas.append((w * h, hwnd))
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return max(achadas)[1] if achadas else None


def mover_janela(hwnd, tela, telas):
    """Leva a janela para `tela`. True se precisou mover, False se já estava lá."""
    r = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    cx, cy = r.left + w // 2, r.top + h // 2
    origem = next((t for t in telas if t["x"] <= cx < t["x"] + t["w"]
                   and t["y"] <= cy < t["y"] + t["h"]), None)
    if origem and origem["dispositivo"] == tela["dispositivo"]:
        return False
    flags = SWP_NOZORDER | SWP_NOACTIVATE
    if user32.IsZoomed(hwnd):
        user32.ShowWindow(hwnd, 9)  # restaurar
        user32.SetWindowPos(hwnd, None, tela["x"] + 40, tela["y"] + 40, 0, 0, flags | SWP_NOSIZE)
        user32.ShowWindow(hwnd, 3)  # maximizar (na nova tela)
    elif not origem or (w >= origem["w"] * 0.95 and h >= origem["h"] * 0.95):
        user32.SetWindowPos(hwnd, None, tela["x"], tela["y"], tela["w"], tela["h"], flags)
    else:
        user32.SetWindowPos(hwnd, None, tela["x"] + (tela["w"] - w) // 2,
                            tela["y"] + (tela["h"] - h) // 2, w, h, flags)
    return True


def mover_para_tela(proc, tela, enviar, limite=90):
    """Roda em thread: leva a janela do jogo para a tela escolhida assim que ela existir."""
    def passo():
        pids = {proc.pid}
        try:
            pids |= {c.pid for c in proc.children(recursive=True)}
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
        hwnd = melhor_janela(pids)
        if not hwnd:
            return None
        return mover_janela(hwnd, tela, _listar_telas_fisicas())

    inicio, estavel, avisou = time.time(), 0, False
    while time.time() - inicio < limite:
        try:
            if not proc.is_running():
                return
        except psutil.NoSuchProcess:
            return
        r = _com_dpi_fisico(passo)
        if r:
            estavel = 0
            if not avisou:
                enviar("status", f"Janela do jogo movida para a {tela['rotulo']}.")
                avisou = True
        elif r is False:
            estavel += 1
            if estavel >= 8:
                return
        time.sleep(1.5)


# ================================================================ controles XInput (nomes)
class XINPUT_GAMEPAD(ctypes.Structure):
    _fields_ = [("wButtons", ctypes.c_ushort), ("bLeftTrigger", ctypes.c_ubyte),
                ("bRightTrigger", ctypes.c_ubyte), ("sThumbLX", ctypes.c_short),
                ("sThumbLY", ctypes.c_short), ("sThumbRX", ctypes.c_short),
                ("sThumbRY", ctypes.c_short)]


class XINPUT_STATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_ulong), ("Gamepad", XINPUT_GAMEPAD)]


class XINPUT_VIBRATION(ctypes.Structure):
    _fields_ = [("wLeftMotorSpeed", ctypes.c_ushort), ("wRightMotorSpeed", ctypes.c_ushort)]


class XINPUT_CAPABILITIES(ctypes.Structure):
    _fields_ = [("Type", ctypes.c_ubyte), ("SubType", ctypes.c_ubyte), ("Flags", ctypes.c_ushort),
                ("Gamepad", XINPUT_GAMEPAD), ("Vibration", XINPUT_VIBRATION)]


class XINPUT_BATTERY_INFORMATION(ctypes.Structure):
    _fields_ = [("BatteryType", ctypes.c_ubyte), ("BatteryLevel", ctypes.c_ubyte)]


class JOYCAPSW(ctypes.Structure):
    _fields_ = [("wMid", ctypes.c_ushort), ("wPid", ctypes.c_ushort),
                ("szPname", ctypes.c_wchar * 32)] + \
               [(n, ctypes.c_uint) for n in (
                   "wXmin", "wXmax", "wYmin", "wYmax", "wZmin", "wZmax", "wNumButtons",
                   "wPeriodMin", "wPeriodMax", "wRmin", "wRmax", "wUmin", "wUmax", "wVmin",
                   "wVmax", "wCaps", "wMaxAxes", "wNumAxes", "wMaxButtons")] + \
               [("szRegKey", ctypes.c_wchar * 32), ("szOEMVxD", ctypes.c_wchar * 260)]


class JOYINFOEX(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulong) for n in (
        "dwSize", "dwFlags", "dwXpos", "dwYpos", "dwZpos", "dwRpos", "dwUpos", "dwVpos",
        "dwButtons", "dwButtonNumber", "dwPOV", "dwReserved1", "dwReserved2")]


SUBTIPOS_XINPUT = {2: "volante", 3: "arcade stick", 4: "joystick de voo", 5: "tapete de dança",
                   6: "guitarra", 7: "guitarra", 8: "bateria", 11: "guitarra baixo",
                   19: "arcade pad"}


def _xinput():
    for dll in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            return getattr(ctypes.windll, dll)
        except (OSError, AttributeError):
            continue
    return None


def _nomes_winmm_xbox():
    """Nomes (do driver) dos controles XInput conectados, na ordem do Windows."""
    nomes = []
    try:
        winmm = ctypes.windll.winmm
        total = winmm.joyGetNumDevs()
    except (OSError, AttributeError):
        return nomes
    for i in range(total):
        info = JOYINFOEX()
        info.dwSize = ctypes.sizeof(JOYINFOEX)
        info.dwFlags = 0xFF
        if winmm.joyGetPosEx(i, ctypes.byref(info)) != 0:
            continue
        caps = JOYCAPSW()
        if winmm.joyGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) != 0:
            continue
        nome = caps.szPname.strip()
        if "xbox" in nome.lower() or "xinput" in nome.lower():
            m = re.match(r"Controller \((.*)\)$", nome, re.IGNORECASE)
            nomes.append(m.group(1) if m else nome)
    return nomes


def listar_controles_directinput():
    """Nomes dos joysticks DirectInput/HID visiveis ao Windows.

    Eles sao apenas diagnostico: o Special K reordena slots XInput e nao um
    dispositivo DirectInput puro.
    """
    controles = []
    try:
        winmm = ctypes.windll.winmm
        total = winmm.joyGetNumDevs()
    except (OSError, AttributeError):
        return controles
    for i in range(total):
        info = JOYINFOEX()
        info.dwSize = ctypes.sizeof(JOYINFOEX)
        info.dwFlags = 0xFF
        if winmm.joyGetPosEx(i, ctypes.byref(info)) != 0:
            continue
        caps = JOYCAPSW()
        if winmm.joyGetDevCapsW(i, ctypes.byref(caps), ctypes.sizeof(caps)) != 0:
            continue
        nome = caps.szPname.strip()
        baixo = nome.lower()
        # XInput ja aparece no seletor; o restante inclui controles PlayStation
        # conectados sem emulacao XInput.
        if nome and "xbox" not in baixo and "xinput" not in baixo:
            controles.append(nome)
    return list(dict.fromkeys(controles))


def rotulos_controle_por_posicao(controles_xinput, controles_directinput):
    """Rotulos dos quatro slots do Special K, com nomes quando conhecidos.

    O slot, e nao o nome, e o que o Special K usa para reordenar. Manter todos
    os quatro disponiveis preserva o uso por ordem de conexao.
    """
    nomes = [None] * 4
    for controle in controles_xinput:
        slot = controle.get("slot")
        if isinstance(slot, int) and 0 <= slot < 4:
            nomes[slot] = controle.get("nome")
    prox = 0
    for nome in controles_directinput:
        while prox < 4 and nomes[prox]:
            prox += 1
        if prox >= 4:
            break
        nomes[prox] = nome
        prox += 1
    rotulos = []
    for slot, nome in enumerate(nomes):
        base = f"Controle {slot + 1}" + (" (padrao)" if slot == 0 else "")
        rotulos.append(f"{base} - {nome}" if nome else base)
    return rotulos


def listar_controles_xinput():
    """Controles XInput conectados: [{"slot", "nome", "rotulo"}]. Só os conectados aparecem."""
    xi = _xinput()
    if not xi:
        return []
    conectados = []
    for slot in range(4):
        st = XINPUT_STATE()
        if xi.XInputGetState(slot, ctypes.byref(st)) != 0:
            continue
        extra = []
        caps = XINPUT_CAPABILITIES()
        if xi.XInputGetCapabilities(slot, 0, ctypes.byref(caps)) == 0:
            if SUBTIPOS_XINPUT.get(caps.SubType):
                extra.append(SUBTIPOS_XINPUT[caps.SubType])
        try:
            bat = XINPUT_BATTERY_INFORMATION()
            if xi.XInputGetBatteryInformation(slot, 0, ctypes.byref(bat)) == 0:
                if bat.BatteryType == 1:
                    extra.append("com fio")
                elif bat.BatteryType in (2, 3):
                    extra.append("sem fio")
        except (OSError, AttributeError):
            pass
        conectados.append({"slot": slot, "extra": extra})
    nomes = _nomes_winmm_xbox()
    usar_nomes = len(nomes) == len(conectados)
    for i, c in enumerate(conectados):
        c["nome"] = nomes[i] if usar_nomes else "Controle XInput"
        c["rotulo"] = " · ".join([c["nome"]] + c["extra"])
    repetidos = {r for r in (c["rotulo"] for c in conectados)
                 if sum(1 for c in conectados if c["rotulo"] == r) > 1}
    for c in conectados:
        if c["rotulo"] in repetidos:
            c["rotulo"] += f" (#{c['slot'] + 1})"
    return conectados


def identificar_controle(timeout=10):
    """Espera um botão ser apertado e devolve o slot (0..3) do controle, ou None."""
    xi = _xinput()
    if not xi:
        return None
    fim = time.time() + timeout
    while time.time() < fim:
        for slot in range(4):
            st = XINPUT_STATE()
            if xi.XInputGetState(slot, ctypes.byref(st)) != 0:
                continue
            g = st.Gamepad
            if g.wButtons != 0 or g.bLeftTrigger > 100 or g.bRightTrigger > 100:
                return slot
        time.sleep(0.03)
    return None


def resolver_slot_controle(perfil, controles):
    """Slot atual do controle escolhido no perfil (pelo nome; se repetido, pelo slot salvo)."""
    slot, nome = perfil.get("j1_slot"), perfil.get("j1_nome")
    if slot is None:
        return None
    mesmos = [c for c in controles if c["nome"] == nome]
    if len(mesmos) == 1:
        return mesmos[0]["slot"]
    return slot if any(c["slot"] == slot for c in controles) else None


# ================================================================ detecção de jogos instalados
def pasta_steam():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as k:
            p = winreg.QueryValueEx(k, "SteamPath")[0].replace("/", "\\")
            if os.path.isdir(p):
                return p
    except OSError:
        pass
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base and os.path.isdir(os.path.join(base, "Steam")):
            return os.path.join(base, "Steam")
    return None


def jogos_steam_de(raiz):
    """Lê libraryfolders.vdf e os appmanifest_*.acf a partir da pasta do Steam."""
    bibliotecas = [raiz]
    bibliotecas_vistas = {os.path.normcase(os.path.realpath(raiz))}
    try:
        with open(os.path.join(raiz, "steamapps", "libraryfolders.vdf"),
                  encoding="utf-8", errors="replace") as f:
            texto = f.read()
    except OSError:
        texto = ""
    for m in re.finditer(r'"path"\s+"([^"]+)"', texto):
        p = m.group(1).replace("\\\\", "\\")
        chave = os.path.normcase(os.path.realpath(p))
        if chave not in bibliotecas_vistas:
            bibliotecas.append(p)
            bibliotecas_vistas.add(chave)
    jogos = []
    appids_vistos = set()
    for lib in bibliotecas:
        pasta_apps = os.path.join(lib, "steamapps")
        try:
            arquivos = os.listdir(pasta_apps)
        except OSError:
            continue
        for nome in arquivos:
            if not (nome.startswith("appmanifest_") and nome.endswith(".acf")):
                continue
            try:
                with open(os.path.join(pasta_apps, nome), encoding="utf-8", errors="replace") as f:
                    t = f.read()
            except OSError:
                continue

            def campo(c, t=t):
                m = re.search(rf'"{c}"\s+"([^"]*)"', t)
                return m.group(1) if m else ""

            appid, titulo, dirn = campo("appid"), campo("name"), campo("installdir")
            if not appid or not titulo:
                continue
            if appid in appids_vistos:
                continue
            if any(x in titulo.lower() for x in ("steamworks common", "redistributable",
                                                 "steam linux runtime")):
                continue
            jogos.append({"id": f"steam:{appid}", "launcher": "Steam", "nome": titulo,
                          "pasta": os.path.join(pasta_apps, "common", dirn), "exe": None,
                          "lancar": ["url", f"steam://rungameid/{appid}"]})
            appids_vistos.add(appid)
    return jogos


def jogos_epic_de(pasta_manifestos):
    jogos = []
    try:
        arquivos = [f for f in os.listdir(pasta_manifestos) if f.lower().endswith(".item")]
    except OSError:
        return jogos
    for arq in arquivos:
        try:
            with open(os.path.join(pasta_manifestos, arq), encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            continue
        nome, app = d.get("DisplayName"), d.get("AppName")
        ns, item = d.get("CatalogNamespace"), d.get("CatalogItemId")
        loc, exe_rel = d.get("InstallLocation"), d.get("LaunchExecutable")
        if not (nome and app and ns and item and loc and exe_rel):
            continue
        if d.get("MainGameAppName") and d.get("MainGameAppName") != app:
            continue  # DLC / complemento
        uri = f"com.epicgames.launcher://apps/{ns}%3A{item}%3A{app}?action=launch&silent=true"
        jogos.append({"id": f"epic:{app}", "launcher": "Epic", "nome": nome, "pasta": loc,
                      "exe": os.path.normpath(os.path.join(loc, exe_rel)),
                      "lancar": ["url", uri]})
    return jogos


SCRIPT_XBOX = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$r = @()
foreach ($p in (Get-AppxPackage | Where-Object { $_.InstallLocation -match 'XboxGames' -and -not $_.IsFramework })) {
  $a = $null
  try { $m = Get-AppxPackageManifest $p; $a = @($m.Package.Applications.Application)[0] } catch {}
  $r += [pscustomobject]@{ Pfn = $p.PackageFamilyName; Loc = $p.InstallLocation; AppId = $a.Id; Exe = $a.Executable }
}
if ($r.Count -eq 0) { '[]' } else { ConvertTo-Json -InputObject @($r) -Compress }
"""


def exe_do_xbox(loc, exe_manifesto):
    try:
        raiz = ET.parse(os.path.join(loc, "MicrosoftGame.config")).getroot()
        nomes = [e.get("Name") for e in raiz.iter()
                 if e.tag.split("}")[-1] == "Executable" and e.get("Name")]
        preferidos = [n for n in nomes if "launch" not in n.lower()]
        escolhido = (preferidos or nomes)[0]
        return os.path.join(loc, escolhido)
    except (OSError, ET.ParseError, IndexError):
        pass
    if exe_manifesto and "gamelaunchhelper" not in exe_manifesto.lower():
        return os.path.join(loc, exe_manifesto)
    return None


def jogos_xbox_de_json(texto):
    try:
        dados = json.loads(texto)
    except ValueError:
        return []
    if isinstance(dados, dict):
        dados = [dados]
    jogos = []
    for d in dados:
        loc, pfn, appid = d.get("Loc"), d.get("Pfn"), d.get("AppId")
        if not (loc and pfn and appid):
            continue
        pasta_jogo = os.path.dirname(loc) if os.path.basename(loc).lower() == "content" else loc
        jogos.append({"id": f"xbox:{pfn}", "launcher": "Xbox", "nome": os.path.basename(pasta_jogo),
                      "pasta": loc, "exe": exe_do_xbox(loc, d.get("Exe")),
                      "lancar": ["explorer", f"shell:AppsFolder\\{pfn}!{appid}"]})
    return jogos


def jogos_xbox():
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                            "-Command", SCRIPT_XBOX], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", creationflags=SEM_JANELA,
                           timeout=90)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return jogos_xbox_de_json(r.stdout.strip())


def varrer_jogos():
    jogos = []
    raiz = pasta_steam()
    if raiz:
        jogos += jogos_steam_de(raiz)
    jogos += jogos_epic_de(os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"),
                                        "Epic", "EpicGamesLauncher", "Data", "Manifests"))
    jogos += jogos_xbox()
    jogos.sort(key=lambda j: (j["launcher"], j["nome"].lower()))
    vistos = {}
    unicos = []
    for j in jogos:  # garante ids únicos (a lista de jogos usa o id como chave)
        base = j["id"]
        if base in vistos:
            continue
        vistos[base] = True
        unicos.append(j)
    return unicos


EXE_IGNORADOS = ("unins", "crash", "redist", "dxsetup", "setup", "install", "easyanticheat",
                 "beservice", "battleye", "prereq", "dotnet", "notification_helper", "webhelper",
                 "report", "updater", "helper", "eac")


def adivinhar_exe(pasta, profundidade=3):
    """Maior .exe da pasta do jogo, ignorando instaladores/ajudantes conhecidos."""
    melhor, tam = None, -1
    if not pasta or not os.path.isdir(pasta):
        return None
    nivel_base = pasta.rstrip("\\/").count(os.sep)
    for raiz, dirs, arqs in os.walk(pasta):
        if raiz.count(os.sep) - nivel_base >= profundidade:
            dirs[:] = []
        for a in arqs:
            baixo = a.lower()
            if not baixo.endswith(".exe") or any(x in baixo for x in EXE_IGNORADOS):
                continue
            try:
                t = os.path.getsize(os.path.join(raiz, a))
            except OSError:
                continue
            if t > tam:
                melhor, tam = os.path.join(raiz, a), t
    return melhor


def abrir_jogo(lancar):
    tipo, alvo = lancar
    if tipo == "explorer":
        subprocess.Popen(["explorer.exe", alvo])
    else:
        os.startfile(alvo)


def achar_processo_do_jogo(exe_nome, pasta):
    """Processo do jogo: primeiro por nome do .exe, depois por estar dentro da pasta dele."""
    por_pasta = None
    for p in psutil.process_iter(["pid", "name", "exe"]):
        nome = (p.info["name"] or "").lower()
        if exe_nome and nome == exe_nome:
            return p
        e = (p.info["exe"] or "").lower()
        if pasta and e.startswith(pasta) and not any(x in nome for x in EXE_IGNORADOS) \
                and por_pasta is None:
            por_pasta = p
    return por_pasta


def vigiar_lancamento(nome_jogo, exe, pasta, audio_id, usar_sk, parar_servico, servicos,
                      tela, enviar):
    """Roda em thread: espera o jogo abrir, troca o som e confirma a injeção do Special K."""
    exe_nome = os.path.basename(exe).lower() if exe else None
    pasta_l = (pasta or "").lower().rstrip("\\/")
    proc = None
    inicio = time.time()
    while time.time() - inicio < 180:
        proc = achar_processo_do_jogo(exe_nome, pasta_l)
        if proc:
            break
        time.sleep(1)
    if not proc:
        enviar("status", f"{nome_jogo}: não encontrei o processo do jogo em 3 minutos.")
        if usar_sk:
            parar_servicos(servicos)
        return
    try:
        exe_real = proc.exe()
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        exe_real = None
    if exe_real and (not exe or exe_real.lower() != exe.lower()):
        enviar("exe", exe_real)
    if audio_id and os.path.exists(SVV):
        try:
            definir_saida_do_app(proc.pid, audio_id)
            enviar("status", f"{nome_jogo}: som enviado ao dispositivo escolhido.")
        except (subprocess.CalledProcessError, OSError) as e:
            enviar("status", f"{nome_jogo}: falha ao trocar o som ({e}).")
    if tela:
        threading.Thread(target=mover_para_tela, args=(proc, tela, enviar), daemon=True).start()
    if usar_sk:
        confirmado = False
        t0 = time.time()
        while time.time() - t0 < 90:
            r = specialk_injetado(proc.pid)
            if r:
                confirmado = True
                break
            if r is None and time.time() - t0 > 30:
                break
            if not proc.is_running():
                break
            time.sleep(1)
        if parar_servico:
            parar_servicos(servicos)
        enviar("status", f"{nome_jogo}: Special K injetado." if confirmado else
               f"{nome_jogo}: não consegui confirmar a injeção do Special K.")
    else:
        enviar("status", f"{nome_jogo}: iniciado.")


# ================================================================ downloads
def baixar(url, destino, referer=None):
    cab = dict(UA)
    if referer:
        cab["Referer"] = referer
    req = urllib.request.Request(url, headers=cab)
    with urllib.request.urlopen(req, timeout=90) as r, open(destino, "wb") as f:
        shutil.copyfileobj(r, f)


def instalar_svv():
    nome = "soundvolumeview-x64.zip" if platform.machine().endswith("64") else "soundvolumeview.zip"
    tmp = os.path.join(tempfile.gettempdir(), nome)
    baixar("https://www.nirsoft.net/utils/" + nome, tmp,
           referer="https://www.nirsoft.net/utils/sound_volume_view.html")
    with zipfile.ZipFile(tmp) as z:
        for item in z.namelist():
            if os.path.basename(item).lower() == "soundvolumeview.exe":
                with z.open(item) as src, open(SVV, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                return f"SoundVolumeView.exe salvo em:\n{PASTA}"
    raise RuntimeError("SoundVolumeView.exe não foi encontrado dentro do zip baixado.")


def instalar_specialk():
    if shutil.which("winget"):
        subprocess.Popen(["winget", "install", "--id", "SpecialK.SpecialK", "-e",
                          "--accept-package-agreements", "--accept-source-agreements"],
                         creationflags=NOVO_CONSOLE)
        return ("A instalação do Special K foi iniciada pelo winget numa janela de "
                "terminal. Quando ela terminar, clique em 'Verificar de novo'.")
    webbrowser.open("https://special-k.info/")
    return ("O winget não está disponível, então abri o site oficial do Special K. "
            "Baixe e instale por lá; depois clique em 'Verificar de novo'.")


COMPONENTES = [
    {"id": "svv", "nome": "SoundVolumeView (NirSoft)",
     "para": "Trocar a saída de som de cada programa. Fica na pasta deste script.",
     "verificar": lambda: os.path.exists(SVV), "instalar": instalar_svv,
     "pagina": "https://www.nirsoft.net/utils/sound_volume_view.html"},
    {"id": "sk", "nome": "Special K",
     "para": "Segundo plano ('Render in Background') e troca de controles por jogo.",
     "verificar": lambda: achar_specialk()[0], "instalar": instalar_specialk,
     "pagina": "https://special-k.info/"},
]


# ================================================================ áudio
def listar_saidas_de_audio():
    """Retorna [(nome_legivel, id_para_linha_de_comando)] das saídas ativas."""
    caminho = os.path.join(tempfile.gettempdir(), "svv_dispositivos.csv")
    subprocess.run([SVV, "/scomma", caminho], creationflags=SEM_JANELA, check=True)
    texto = None
    for enc in ("utf-8-sig", "cp1252"):
        try:
            with open(caminho, encoding=enc) as f:
                texto = f.read()
            break
        except UnicodeDecodeError:
            continue
    saidas = []
    for linha in csv.DictReader(texto.splitlines()):
        if linha.get("Type") != "Device" or linha.get("Direction") != "Render":
            continue
        if linha.get("Device State", "Active") != "Active":
            continue
        id_cmd = linha.get("Command-Line Friendly ID", "")
        if id_cmd:
            saidas.append((f"{linha.get('Name', '')} ({linha.get('Device Name', '')})", id_cmd))
    return saidas


def definir_saida_do_app(pid, id_dispositivo):
    subprocess.run([SVV, "/SetAppDefault", id_dispositivo, "all", str(pid)],
                   creationflags=SEM_JANELA, check=True)


# ================================================================ processos
def pids_com_janela():
    pids = set()

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            pids.add(pid.value)
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return pids


def listar_processos(so_jogos, todos, busca):
    """todos=True inclui processos sem janela (workers, compiladores etc.)."""
    com_janela = pids_com_janela()
    busca = busca.strip().lower()
    resultado = []
    for p in psutil.process_iter(["pid", "name"]):
        pid = p.info["pid"]
        nome = p.info["name"] or ""
        if pid == 0:
            continue
        if not todos and (pid not in com_janela or nome.lower() in IGNORAR):
            continue
        if busca and busca not in nome.lower():
            continue
        if so_jogos:
            try:
                caminho = p.exe().lower()
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
            if not any(x in caminho for x in PASTAS_JOGOS):
                continue
        resultado.append((pid, nome))
    return sorted(resultado, key=lambda x: x[1].lower())


def aplicar_em(proc, nucleos, prioridade):
    """Aplica afinidade/prioridade se ainda não estiverem iguais. True se deu certo."""
    try:
        if nucleos is not None and sorted(proc.cpu_affinity()) != sorted(nucleos):
            proc.cpu_affinity(nucleos)
        if prioridade is not None and proc.nice() != prioridade:
            proc.nice(prioridade)
        return True
    except (psutil.AccessDenied, psutil.NoSuchProcess):
        return False


def com_filhos(proc, incluir_filhos):
    alvo = [proc]
    if incluir_filhos:
        try:
            alvo += proc.children(recursive=True)
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
    return alvo


# ================================================================ configuração salva
def carregar_config():
    try:
        with open(ARQ_CONFIG, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        d = {}
    d.setdefault("nao_avisar_componentes", False)
    d.setdefault("pendentes", [])  # [{"exe", "ini", "edicoes": [[secao, chave, valor], ...]}]
    d.setdefault("jogos", {})          # (antigo) id do jogo -> perfil; migrado ao listar os jogos
    d.setdefault("perfis_jogos", {})   # nome do perfil -> perfil (com "jogo_id")
    d.setdefault("exes", {})           # id do jogo -> caminho do .exe escolhido/aprendido
    return d


def salvar_config(d):
    try:
        with open(ARQ_CONFIG, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ================================================================ interface
class JanelaComponentes(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title("Componentes necessários")
        self.geometry("640x340")
        self.transient(app)
        ttk.Label(self, wraplength=600, justify="left", padding=10,
                  text="Estes componentes de terceiros são usados pelo programa. "
                       "Os que faltarem podem ser baixados aqui. Recursos cujo componente "
                       "estiver faltando ficam sem funcionar.").pack(fill="x")
        self.linhas = {}
        for comp in COMPONENTES:
            quadro = ttk.Frame(self, padding=(10, 6))
            quadro.pack(fill="x")
            quadro.columnconfigure(0, weight=1)
            ttk.Label(quadro, text=comp["nome"], font=("Segoe UI", 10, "bold")).grid(
                row=0, column=0, sticky="w")
            ttk.Label(quadro, text=comp["para"], wraplength=360, justify="left").grid(
                row=1, column=0, sticky="w")
            estado = ttk.Label(quadro, text="")
            estado.grid(row=0, column=1, padx=10)
            botao = ttk.Button(quadro, text="Baixar e instalar",
                               command=lambda c=comp: self.instalar(c))
            botao.grid(row=0, column=2, rowspan=2)
            self.linhas[comp["id"]] = (estado, botao)
        self.nao_avisar = tk.BooleanVar(value=app.config["nao_avisar_componentes"])
        ttk.Checkbutton(self, text="Não avisar sobre componentes faltando ao abrir",
                        variable=self.nao_avisar, command=self.salvar_preferencia).pack(
            anchor="w", padx=10, pady=(10, 0))
        rodape = ttk.Frame(self, padding=10)
        rodape.pack(fill="x", side="bottom")
        ttk.Button(rodape, text="Fechar", command=self.destroy).pack(side="right")
        ttk.Button(rodape, text="Verificar de novo", command=self.verificar).pack(side="right", padx=6)
        self.verificar()

    def salvar_preferencia(self):
        self.app.config["nao_avisar_componentes"] = self.nao_avisar.get()
        salvar_config(self.app.config)

    def verificar(self):
        for comp in COMPONENTES:
            estado, botao = self.linhas[comp["id"]]
            ok = comp["verificar"]()
            estado.config(text="✔ Instalado" if ok else "✖ Faltando")
            botao.config(state="disabled" if ok else "normal")
        self.app.depois_de_instalar()

    def instalar(self, comp):
        estado, botao = self.linhas[comp["id"]]
        botao.config(state="disabled")
        estado.config(text="Baixando…")

        def depois(ok, res):
            if not self.winfo_exists():
                return
            if ok:
                messagebox.showinfo(comp["nome"], res, parent=self)
            elif messagebox.askyesno(
                    comp["nome"],
                    f"Não consegui baixar automaticamente:\n{res}\n\n"
                    "Abrir a página oficial no navegador?", parent=self):
                webbrowser.open(comp["pagina"])
            self.verificar()

        self.app.em_segundo_plano(comp["instalar"], depois)


class PainelRolavel(ttk.Frame):
    """Área com barra de rolagem; coloque os widgets em `.interno`."""

    def __init__(self, pai):
        super().__init__(pai)
        self.canvas = tk.Canvas(self, highlightthickness=0)
        barra = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.interno = ttk.Frame(self.canvas, padding=(0, 0, 10, 0))
        self.janela = self.canvas.create_window((0, 0), window=self.interno, anchor="nw")
        self.canvas.configure(yscrollcommand=barra.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        barra.pack(side="right", fill="y")
        self.interno.bind("<Configure>", lambda e: self.canvas.configure(
            scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfig(self.janela, width=e.width))
        self.canvas.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._roda))
        self.canvas.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _roda(self, evento):
        self.canvas.yview_scroll(int(-evento.delta / 120), "units")


class App(tk.Tk):
    OPCOES_J1 = ["Controle 1 (padrão)", "Controle 2", "Controle 3", "Controle 4",
                 "(não alterar)"]

    def __init__(self):
        super().__init__()
        self.title("Afinidade, prioridade, som e Special K")
        self.geometry("1220x880")
        self.n_cpus = psutil.cpu_count(logical=True)
        self.vars_cpu = [tk.BooleanVar(value=True) for _ in range(self.n_cpus)]
        self.regras = {}        # nome_do_exe(lower) -> {"nucleos", "prioridade", "filhos"}
        self.config = carregar_config()
        self.saidas = []
        self.jogos = []
        self.jogos_por_id = {}
        self.jogo_atual_id = None
        self.cache_exe = {}
        self.ultimo_perfil_lancado = {}
        self.fila_msgs = queue.Queue()

        self.principal = ttk.Notebook(self)
        self.principal.pack(fill="both", expand=True, padx=8, pady=6)
        aba_prog = ttk.Frame(self.principal)
        aba_jogos = ttk.Frame(self.principal, padding=8)
        self.principal.add(aba_prog, text="Programas abertos")
        self.principal.add(aba_jogos, text="Jogos")

        self.montar_filtros_e_lista(aba_prog)
        self.abas = ttk.Notebook(aba_prog)
        self.abas.pack(fill="both", expand=True, pady=6)
        self.montar_aba_cpu(self.nova_aba("CPU e prioridade"))
        self.montar_aba_som(self.nova_aba("Som"))
        self.montar_aba_specialk(self.nova_aba("Special K: segundo plano e controles"))
        self.montar_aba_jogos(aba_jogos)

        self.status = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status, padding=(8, 0, 8, 6)).pack(fill="x")

        self.atualizar()
        self.atualizar_tab_pendentes()
        self.after(2000, self.vigiar)
        self.after(2000, self.vigiar_pendentes)
        self.after(500, self.drenar_fila)
        self.after(400, self.checar_componentes_inicio)
        self.after(900, self.procurar_jogos)

    # ---------------------------------------------------------------- utilidades
    def nova_aba(self, titulo):
        aba = ttk.Frame(self.abas, padding=8)
        self.abas.add(aba, text=titulo)
        return aba

    def em_segundo_plano(self, funcao, depois):
        fila = queue.Queue()

        def alvo():
            try:
                fila.put((True, funcao()))
            except Exception as e:  # noqa: BLE001
                fila.put((False, e))

        threading.Thread(target=alvo, daemon=True).start()

        def checar():
            try:
                ok, res = fila.get_nowait()
            except queue.Empty:
                self.after(150, checar)
                return
            depois(ok, res)

        self.after(150, checar)

    # ---------------------------------------------------------------- componentes
    def checar_componentes_inicio(self):
        if self.config["nao_avisar_componentes"]:
            return
        if any(not c["verificar"]() for c in COMPONENTES):
            self.abrir_componentes()

    def abrir_componentes(self):
        JanelaComponentes(self)

    def depois_de_instalar(self):
        if hasattr(self, "combo_som"):
            self.carregar_saidas()

    # ---------------------------------------------------------------- topo: filtros + lista
    def montar_filtros_e_lista(self, pai):
        topo = ttk.Frame(pai, padding=(0, 8, 0, 0))
        topo.pack(fill="x")
        self.so_jogos = tk.BooleanVar(value=False)
        self.todos = tk.BooleanVar(value=False)
        ttk.Checkbutton(topo, text="Só jogos de launchers conhecidos",
                        variable=self.so_jogos, command=self.atualizar).pack(side="left")
        ttk.Checkbutton(topo, text="Incluir processos sem janela",
                        variable=self.todos, command=self.atualizar).pack(side="left", padx=8)
        ttk.Button(topo, text="Componentes", command=self.abrir_componentes).pack(side="right", padx=(6, 0))
        ttk.Button(topo, text="Atualizar", command=self.atualizar).pack(side="right")

        busca_frame = ttk.Frame(pai, padding=(0, 4))
        busca_frame.pack(fill="x")
        ttk.Label(busca_frame, text="Buscar:").pack(side="left")
        self.busca = tk.StringVar()
        entrada = ttk.Entry(busca_frame, textvariable=self.busca)
        entrada.pack(side="left", fill="x", expand=True, padx=6)
        entrada.bind("<KeyRelease>", lambda _e: self.atualizar())

        self.lista = ttk.Treeview(pai, columns=("pid", "nome"), show="headings", height=7)
        self.lista.heading("pid", text="PID")
        self.lista.heading("nome", text="Programa selecionado para as abas abaixo")
        self.lista.column("pid", width=70, anchor="center")
        self.lista.column("nome", width=600)
        self.lista.pack(fill="x")
        self.lista.bind("<<TreeviewSelect>>", self.ao_selecionar)

    def atualizar(self):
        self.lista.delete(*self.lista.get_children())
        for pid, nome in listar_processos(self.so_jogos.get(), self.todos.get(), self.busca.get()):
            self.lista.insert("", "end", values=(pid, nome))

    def pid_selecionado(self):
        sel = self.lista.selection()
        return int(self.lista.item(sel[0])["values"][0]) if sel else None

    def exe_selecionado(self):
        pid = self.pid_selecionado()
        if pid is None:
            return None
        try:
            return psutil.Process(pid).exe()
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            return None

    def ao_selecionar(self, _):
        pid = self.pid_selecionado()
        if pid is None:
            return
        try:
            p = psutil.Process(pid)
            atuais = p.cpu_affinity()
            for i, var in enumerate(self.vars_cpu):
                var.set(i in atuais)
            nome_prio = NOME_PRIORIDADE.get(p.nice())
            if nome_prio:
                self.combo_prio.set(nome_prio)
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            pass
        self.carregar_estado_specialk()

    # ---------------------------------------------------------------- aba CPU
    def montar_aba_cpu(self, aba):
        quadro = ttk.LabelFrame(aba, text="Núcleos permitidos (lógicos)", padding=8)
        quadro.pack(fill="x")
        colunas = 4
        for i, var in enumerate(self.vars_cpu):
            ttk.Checkbutton(quadro, text=f"CPU {i}", variable=var).grid(
                row=i // colunas, column=i % colunas, sticky="w", padx=8, pady=2)
        atalhos = ttk.Frame(quadro)
        atalhos.grid(row=(self.n_cpus // colunas) + 1, column=0, columnspan=colunas,
                     sticky="w", pady=(6, 0))
        ttk.Button(atalhos, text="Todos", command=lambda: self.marcar(True)).pack(side="left")
        ttk.Button(atalhos, text="Nenhum", command=lambda: self.marcar(False)).pack(side="left", padx=4)
        ttk.Button(atalhos, text="Pares", command=lambda: self.marcar_paridade(0)).pack(side="left")
        ttk.Button(atalhos, text="Ímpares", command=lambda: self.marcar_paridade(1)).pack(side="left", padx=4)

        prio = ttk.LabelFrame(aba, text="Prioridade e opções", padding=8)
        prio.pack(fill="x", pady=6)
        linha1 = ttk.Frame(prio)
        linha1.pack(fill="x")
        ttk.Label(linha1, text="Prioridade:").pack(side="left")
        self.combo_prio = ttk.Combobox(linha1, state="readonly", width=20,
                                       values=[n for n, _ in PRIORIDADES])
        self.combo_prio.current(2)
        self.combo_prio.pack(side="left", padx=6)
        self.filhos = tk.BooleanVar(value=True)
        self.manter = tk.BooleanVar(value=False)
        ttk.Checkbutton(prio, text="Incluir processos filhos (workers/compiladores iniciados por ele)",
                        variable=self.filhos).pack(anchor="w", pady=(6, 0))
        ttk.Checkbutton(prio, text="Manter forçado: reaplicar a cada 2 s a todo processo com este nome",
                        variable=self.manter).pack(anchor="w")
        ttk.Button(prio, text="Aplicar núcleos e prioridade",
                   command=self.aplicar).pack(anchor="e", pady=(6, 0))

        regras = ttk.LabelFrame(aba, text="Regras mantidas ativamente", padding=8)
        regras.pack(fill="x")
        self.tab_regras = ttk.Treeview(regras, columns=("nome", "cpus", "prio"),
                                       show="headings", height=3)
        self.tab_regras.heading("nome", text="Programa")
        self.tab_regras.heading("cpus", text="CPUs")
        self.tab_regras.heading("prio", text="Prioridade")
        self.tab_regras.column("nome", width=200)
        self.tab_regras.column("cpus", width=260)
        self.tab_regras.column("prio", width=130)
        self.tab_regras.pack(side="left", fill="x", expand=True)
        ttk.Button(regras, text="Remover",
                   command=self.remover_regra).pack(side="left", padx=(6, 0), anchor="n")

    def marcar(self, valor):
        for v in self.vars_cpu:
            v.set(valor)

    def marcar_paridade(self, resto):
        for i, v in enumerate(self.vars_cpu):
            v.set(i % 2 == resto)

    def aplicar(self):
        pid = self.pid_selecionado()
        if pid is None:
            messagebox.showwarning("Aviso", "Selecione um programa na lista.")
            return
        nucleos = [i for i, v in enumerate(self.vars_cpu) if v.get()]
        if not nucleos:
            messagebox.showwarning("Aviso", "Marque pelo menos um núcleo.")
            return
        prioridade = PRIORIDADES[self.combo_prio.current()][1]
        try:
            proc = psutil.Process(pid)
            nome = proc.name()
        except psutil.NoSuchProcess:
            messagebox.showerror("Erro", "O processo foi fechado.")
            self.atualizar()
            return
        ok = falhas = 0
        for alvo in com_filhos(proc, self.filhos.get()):
            if aplicar_em(alvo, nucleos, prioridade):
                ok += 1
            else:
                falhas += 1
        if self.manter.get():
            self.regras[nome.lower()] = {"nucleos": nucleos, "prioridade": prioridade,
                                         "filhos": self.filhos.get()}
            self.atualizar_regras()
        msg = f"Aplicado em {ok} processo(s): CPUs {nucleos}, prioridade {self.combo_prio.get()}."
        if falhas:
            msg += (f"\n{falhas} processo(s) negaram acesso; anti-cheats também podem bloquear.")
            messagebox.showwarning("Aplicado com ressalvas", msg)
        else:
            messagebox.showinfo("Pronto", msg)

    def atualizar_regras(self):
        self.tab_regras.delete(*self.tab_regras.get_children())
        for nome, r in self.regras.items():
            cpus = ",".join(map(str, r["nucleos"])) if r["nucleos"] else "(não alterar)"
            self.tab_regras.insert("", "end", iid=nome, values=(
                nome, cpus, NOME_PRIORIDADE.get(r["prioridade"], "(não alterar)")))

    def remover_regra(self):
        for iid in self.tab_regras.selection():
            self.regras.pop(iid, None)
        self.atualizar_regras()

    def vigiar(self):
        """Reaplica as regras a cada 2 s, inclusive em processos novos com o mesmo nome."""
        if self.regras:
            for p in psutil.process_iter(["name"]):
                regra = self.regras.get((p.info["name"] or "").lower())
                if not regra:
                    continue
                for alvo in com_filhos(p, regra["filhos"]):
                    aplicar_em(alvo, regra["nucleos"], regra["prioridade"])
        self.after(2000, self.vigiar)

    # ---------------------------------------------------------------- aba Som
    def montar_aba_som(self, aba):
        som = ttk.LabelFrame(aba, text="Saída de som do programa selecionado", padding=8)
        som.pack(fill="x")
        self.combo_som = ttk.Combobox(som, state="readonly", width=60)
        self.combo_som.pack(side="left", fill="x", expand=True)
        ttk.Button(som, text="Aplicar som", command=self.aplicar_som).pack(side="left", padx=(6, 0))
        self.carregar_saidas()

    def carregar_saidas(self):
        self.saidas = [("Padrão do Windows", PADRAO_WINDOWS)]
        if not os.path.exists(SVV):
            self.combo_som["values"] = ["(SoundVolumeView.exe não encontrado — use o botão Componentes)"]
            self.combo_som.current(0)
            self.atualizar_audio_jogos()
            return
        try:
            self.saidas += listar_saidas_de_audio()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("Erro", f"Não consegui listar os dispositivos de som:\n{e}")
        self.combo_som["values"] = [nome for nome, _ in self.saidas]
        self.combo_som.current(0)
        self.atualizar_audio_jogos()

    def aplicar_som(self):
        pid = self.pid_selecionado()
        if pid is None:
            messagebox.showwarning("Aviso", "Selecione um programa na lista.")
            return
        if not os.path.exists(SVV):
            messagebox.showerror("Falta um componente",
                                 "O SoundVolumeView.exe não foi encontrado. "
                                 "Use o botão Componentes para baixá-lo.")
            return
        nome, id_dispositivo = self.saidas[self.combo_som.current()]
        try:
            definir_saida_do_app(pid, id_dispositivo)
            messagebox.showinfo("Pronto", f"Som do programa enviado para:\n{nome}")
        except subprocess.CalledProcessError as e:
            messagebox.showerror("Erro", f"Falha ao trocar o dispositivo de som:\n{e}")

    # ---------------------------------------------------------------- aba Special K
    def montar_aba_specialk(self, aba):
        ttk.Label(aba, wraplength=700, justify="left", text=(
            "Estas opções editam o SpecialK.ini do jogo selecionado. Para funcionarem, o jogo "
            "precisa ser aberto com o Special K injetado (pelo SKIF ou pela aba Jogos). O Special K "
            "regrava o .ini quando o jogo fecha, por isso as alterações só são gravadas DEPOIS que "
            "o jogo for fechado (mantenha este programa aberto) e valem na próxima vez que o jogo abrir."
        )).pack(fill="x")

        botoes = ttk.Frame(aba)
        botoes.pack(fill="x", pady=8)
        ttk.Button(botoes, text="Abrir o Special K (SKIF)", command=self.abrir_skif).pack(side="left")
        ttk.Button(botoes, text="Abrir pasta de perfis", command=self.abrir_pasta_perfis).pack(side="left", padx=6)
        ttk.Button(botoes, text="Abrir o .ini do jogo", command=self.abrir_ini).pack(side="left")
        ttk.Label(aba, text="Arquivo de configuração do jogo selecionado:").pack(anchor="w")
        self.info_ini = tk.StringVar(value="Selecione um jogo na lista.")
        ttk.Entry(aba, textvariable=self.info_ini, state="readonly").pack(fill="x")

        bg = ttk.LabelFrame(aba, text="Segundo plano", padding=8)
        bg.pack(fill="x", pady=(10, 0))
        self.sk_bg = tk.BooleanVar(value=False)
        ttk.Checkbutton(bg, text="Manter o jogo rodando normalmente em segundo plano "
                                 "([Window.System] RenderInBackground)",
                        variable=self.sk_bg).pack(anchor="w")

        ct = ttk.LabelFrame(aba, text="Controles", padding=8)
        ct.pack(fill="x", pady=(10, 0))
        linha = ttk.Frame(ct)
        linha.pack(fill="x")
        ttk.Label(linha, text="Este jogo usa como Jogador 1:").pack(side="left")
        self.combo_j1 = ttk.Combobox(linha, state="readonly", width=22, values=self.OPCOES_J1)
        self.combo_j1.current(0)
        self.combo_j1.pack(side="left", padx=6)
        ttk.Label(ct, wraplength=700, justify="left", text=(
            "'Controle N' é a posição do controle no Windows (Controle 1 = primeiro conectado). "
            "A troca é feita trocando o Controle 1 com o escolhido ([Input.XInput] SlotReassignment). "
            "A ordem depende da ordem de conexão; se pegar o controle errado, ajuste aqui.")).pack(
            anchor="w", pady=(6, 0))

        acoes = ttk.Frame(aba)
        acoes.pack(fill="x", pady=10)
        ttk.Button(acoes, text="Aplicar ao jogo selecionado",
                   command=lambda: self.aplicar_specialk(restaurar=False)).pack(side="left")
        ttk.Button(acoes, text="Restaurar padrão do jogo",
                   command=lambda: self.aplicar_specialk(restaurar=True)).pack(side="left", padx=6)

        pend = ttk.LabelFrame(aba, text="Aguardando o jogo fechar para gravar", padding=8)
        pend.pack(fill="x")
        self.tab_pend = ttk.Treeview(pend, columns=("jogo", "mudancas"), show="headings", height=3)
        self.tab_pend.heading("jogo", text="Jogo")
        self.tab_pend.heading("mudancas", text="Alterações")
        self.tab_pend.column("jogo", width=200)
        self.tab_pend.column("mudancas", width=400)
        self.tab_pend.pack(side="left", fill="x", expand=True)
        ttk.Button(pend, text="Cancelar", command=self.cancelar_pendencia).pack(
            side="left", padx=(6, 0), anchor="n")

    def carregar_estado_specialk(self):
        """Mostra o caminho do .ini e preenche os controles com os valores atuais dele."""
        exe = self.exe_selecionado()
        if not exe:
            return
        caminho, existe = escolher_ini(exe)
        if not existe:
            self.info_ini.set(f"(ainda não existe) {caminho}")
            self.sk_bg.set(False)
            self.combo_j1.current(0)
            return
        self.info_ini.set(caminho)
        bg = ler_valor_ini(caminho, "Window.System", "RenderInBackground")
        self.sk_bg.set((bg or "").lower() == "true")
        lista = ler_valor_ini(caminho, "Input.XInput", "SlotReassignment")
        if lista is None:
            self.combo_j1.current(0)
        else:
            n = jogador1_de(lista)
            self.combo_j1.current(n if n is not None else 4)

    def aplicar_specialk(self, restaurar):
        exe = self.exe_selecionado()
        if not exe:
            messagebox.showwarning("Aviso", "Selecione um jogo na lista.")
            return
        if restaurar:
            edicoes = [["Window.System", "RenderInBackground", "false"],
                       ["Input.XInput", "SlotReassignment", "0,1,2,3"]]
        else:
            edicoes = edicoes_specialk(self.sk_bg.get(), self.combo_j1.current())
        caminho, existe = escolher_ini(exe)
        if not existe and not messagebox.askyesno(
                "Special K",
                "O .ini desse jogo ainda não existe (o Special K cria depois de rodar o jogo "
                "uma vez).\n\nCriar um arquivo novo só com estas opções em:\n" + caminho + "?"):
            return
        nome = os.path.basename(exe)
        resumo = "; ".join(f"{k}={v}" for _, k, v in edicoes)
        self.config["pendentes"] = [p for p in self.config["pendentes"] if p["exe"].lower() != exe.lower()]
        self.config["pendentes"].append({"exe": exe, "ini": caminho, "edicoes": edicoes, "resumo": resumo})
        salvar_config(self.config)
        self.atualizar_tab_pendentes()
        if jogo_em_execucao(exe):
            self.status.set(f"{nome}: as alterações serão gravadas quando o jogo fechar.")
        else:
            self.vigiar_pendentes(reagendar=False)

    def atualizar_tab_pendentes(self):
        self.tab_pend.delete(*self.tab_pend.get_children())
        for p in self.config["pendentes"]:
            self.tab_pend.insert("", "end", iid=p["exe"], values=(
                os.path.basename(p["exe"]), p.get("resumo", "")))

    def cancelar_pendencia(self):
        sel = set(self.tab_pend.selection())
        self.config["pendentes"] = [p for p in self.config["pendentes"] if p["exe"] not in sel]
        salvar_config(self.config)
        self.atualizar_tab_pendentes()

    def vigiar_pendentes(self, reagendar=True):
        """Grava as alterações no .ini assim que o jogo correspondente fechar."""
        try:
            restantes = []
            for p in self.config["pendentes"]:
                if jogo_em_execucao(p["exe"]):
                    restantes.append(p)
                    continue
                try:
                    escrever_edicoes(p["ini"], [tuple(e) for e in p["edicoes"]])
                    self.status.set(f"Special K: configuração gravada para {os.path.basename(p['exe'])}.")
                except OSError as e:
                    self.status.set(f"Erro ao gravar {p['ini']}: {e}")
            if len(restantes) != len(self.config["pendentes"]):
                self.config["pendentes"] = restantes
                salvar_config(self.config)
                self.atualizar_tab_pendentes()
        finally:
            if reagendar:
                self.after(2000, self.vigiar_pendentes)

    def abrir_skif(self):
        _, skif = achar_specialk()
        if skif:
            os.startfile(skif)
        else:
            messagebox.showwarning("Special K", "SKIF.exe não encontrado. Use o botão Componentes.")

    def abrir_pasta_perfis(self):
        pasta = pasta_perfis_specialk()
        if os.path.isdir(pasta):
            os.startfile(pasta)
        else:
            messagebox.showinfo("Special K", f"A pasta ainda não existe:\n{pasta}\n\n"
                                             "Ela é criada depois que um jogo roda com o Special K.")

    def abrir_ini(self):
        exe = self.exe_selecionado()
        if not exe:
            messagebox.showwarning("Aviso", "Selecione um jogo na lista.")
            return
        caminho, existe = escolher_ini(exe)
        if existe:
            os.startfile(caminho)
        else:
            messagebox.showinfo("Special K",
                                "Não achei o .ini desse jogo. Rode o jogo com o Special K uma vez "
                                "e feche.\n\nOnde ele deve aparecer:\n" + caminho)

    # ---------------------------------------------------------------- aba Jogos
    def tree_com_barra(self, pai, colunas, cabecalhos, larguras, altura):
        quadro = ttk.Frame(pai)
        tree = ttk.Treeview(quadro, columns=colunas, show="headings", height=altura)
        for c, t, w in zip(colunas, cabecalhos, larguras):
            tree.heading(c, text=t)
            tree.column(c, width=w)
        barra = ttk.Scrollbar(quadro, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=barra.set)
        tree.pack(side="left", fill="both", expand=True)
        barra.pack(side="right", fill="y")
        return quadro, tree

    def montar_aba_jogos(self, aba):
        aba.columnconfigure(0, weight=1, minsize=400)
        aba.columnconfigure(1, weight=2, minsize=580)
        aba.rowconfigure(0, weight=1)

        # ---- coluna esquerda: jogos instalados + perfis salvos
        esq = ttk.Frame(aba)
        esq.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        esq.columnconfigure(0, weight=1)
        esq.rowconfigure(2, weight=3)
        esq.rowconfigure(4, weight=2)
        topo = ttk.Frame(esq)
        topo.grid(row=0, column=0, sticky="ew")
        ttk.Button(topo, text="Procurar jogos instalados", command=self.procurar_jogos).pack(side="left")
        self.lbl_jogos = ttk.Label(topo, text="")
        self.lbl_jogos.pack(side="left", padx=8)
        busca = ttk.Frame(esq)
        busca.grid(row=1, column=0, sticky="ew", pady=4)
        ttk.Label(busca, text="Buscar:").pack(side="left")
        self.busca_jogos = tk.StringVar()
        entrada = ttk.Entry(busca, textvariable=self.busca_jogos)
        entrada.pack(side="left", fill="x", expand=True, padx=6)
        entrada.bind("<KeyRelease>", lambda _e: self.preencher_lista_jogos())
        quadro, self.tab_jogos = self.tree_com_barra(
            esq, ("launcher", "nome"), ("Launcher", "Jogo"), (80, 300), 12)
        quadro.grid(row=2, column=0, sticky="nsew")
        self.tab_jogos.column("launcher", anchor="center", stretch=False)
        self.tab_jogos.bind("<<TreeviewSelect>>", self.ao_selecionar_jogo)

        ttk.Label(esq, text="Perfis salvos (dê dois cliques para iniciar)",
                  font=("Segoe UI", 10, "bold")).grid(row=3, column=0, sticky="w", pady=(10, 2))
        quadro_p, self.tab_perfis_jogos = self.tree_com_barra(
            esq, ("nome", "jogo"), ("Perfil", "Jogo"), (190, 190), 6)
        quadro_p.grid(row=4, column=0, sticky="nsew")
        self.tab_perfis_jogos.bind("<<TreeviewSelect>>", self.ao_selecionar_perfil)
        self.tab_perfis_jogos.bind("<Double-1>", lambda _e: self.iniciar_perfil_selecionado())
        botoes_p = ttk.Frame(esq)
        botoes_p.grid(row=5, column=0, sticky="ew", pady=(6, 0))
        ttk.Button(botoes_p, text="Iniciar perfil selecionado",
                   command=self.iniciar_perfil_selecionado).pack(side="left")
        ttk.Button(botoes_p, text="Excluir perfil", command=self.excluir_perfil).pack(side="left", padx=6)

        # ---- coluna direita: configuração (com rolagem)
        painel = PainelRolavel(aba)
        painel.grid(row=0, column=1, sticky="nsew")
        p = painel.interno

        nome = ttk.LabelFrame(p, text="Nome do perfil", padding=6)
        nome.pack(fill="x", pady=(0, 8))
        self.var_nome_perfil = tk.StringVar()
        ttk.Entry(nome, textvariable=self.var_nome_perfil).pack(fill="x")

        ex = ttk.LabelFrame(p, text="Executável do jogo (usado pelo Special K e pelas regras)", padding=6)
        ex.pack(fill="x", pady=(0, 8))
        self.var_exe_jogo = tk.StringVar()
        ttk.Entry(ex, textvariable=self.var_exe_jogo).pack(side="left", fill="x", expand=True)
        ttk.Button(ex, text="Procurar…", command=self.escolher_exe_jogo).pack(side="left", padx=(6, 0))

        cpu = ttk.LabelFrame(p, text="CPU", padding=6)
        cpu.pack(fill="x", pady=(0, 8))
        self.var_usar_cpu = tk.BooleanVar(value=False)
        ttk.Checkbutton(cpu, text="Definir núcleos permitidos", variable=self.var_usar_cpu).grid(
            row=0, column=0, columnspan=8, sticky="w")
        self.vars_cpu_jogo = [tk.BooleanVar(value=True) for _ in range(self.n_cpus)]
        colunas = 8
        for i, var in enumerate(self.vars_cpu_jogo):
            ttk.Checkbutton(cpu, text=f"{i}", variable=var).grid(
                row=1 + i // colunas, column=i % colunas, sticky="w", padx=6)
        fim = 2 + (self.n_cpus - 1) // colunas
        ttk.Label(cpu, text="Prioridade:").grid(row=fim, column=0, sticky="w", pady=(6, 0))
        self.combo_prio_jogo = ttk.Combobox(cpu, state="readonly", width=20,
                                            values=["(não alterar)"] + [n for n, _ in PRIORIDADES])
        self.combo_prio_jogo.current(0)
        self.combo_prio_jogo.grid(row=fim, column=1, columnspan=4, sticky="w", pady=(6, 0))

        som = ttk.LabelFrame(p, text="Som", padding=6)
        som.pack(fill="x", pady=(0, 8))
        ttk.Label(som, text="Saída de som do jogo:").pack(side="left")
        self.combo_audio_jogo = ttk.Combobox(som, state="readonly", width=50, values=["(não alterar)"])
        self.combo_audio_jogo.current(0)
        self.combo_audio_jogo.pack(side="left", padx=6, fill="x", expand=True)

        tela = ttk.LabelFrame(p, text="Tela onde o jogo abre", padding=6)
        tela.pack(fill="x", pady=(0, 8))
        self.telas = []
        self.combo_tela_jogo = ttk.Combobox(tela, state="readonly", width=50, values=["(não alterar)"])
        self.combo_tela_jogo.current(0)
        self.combo_tela_jogo.pack(side="left", fill="x", expand=True)
        ttk.Button(tela, text="Atualizar telas", command=self.atualizar_telas).pack(side="left", padx=(6, 0))
        ttk.Label(p, wraplength=560, justify="left", text=(
            "O programa leva a janela do jogo para a tela escolhida depois que ela abre. "
            "Funciona melhor em janela ou tela cheia sem bordas; tela cheia exclusiva pode ignorar.")
                  ).pack(fill="x", pady=(0, 8))

        sk = ttk.LabelFrame(p, text="Special K e controle", padding=6)
        sk.pack(fill="x", pady=(0, 8))
        self.var_sk = tk.BooleanVar(value=True)
        self.var_sk_bg = tk.BooleanVar(value=True)
        self.var_sk_parar = tk.BooleanVar(value=True)
        ttk.Checkbutton(sk, text="Abrir o jogo com o Special K", variable=self.var_sk).pack(anchor="w")
        ttk.Checkbutton(sk, text="Manter em segundo plano (RenderInBackground)",
                        variable=self.var_sk_bg).pack(anchor="w")
        ttk.Label(sk, text="Controle que será o Jogador 1:").pack(anchor="w", pady=(6, 0))
        linha_ctrl = ttk.Frame(sk)
        linha_ctrl.pack(fill="x")
        self.controles_xi = []
        self.controles_di = []
        self.rotulos_j1 = rotulos_controle_por_posicao([], [])
        self.combo_j1_jogo = ttk.Combobox(linha_ctrl, state="readonly", width=44,
                                          values=["(não alterar)"])
        self.combo_j1_jogo.current(0)
        self.combo_j1_jogo.pack(side="left", fill="x", expand=True)
        ttk.Button(linha_ctrl, text="Atualizar", command=self.atualizar_controles_jogo).pack(
            side="left", padx=(6, 0))
        ttk.Button(linha_ctrl, text="Identificar…", command=self.identificar_controle_ui).pack(
            side="left", padx=(6, 0))
        self.lbl_ctrl = ttk.Label(sk, text="")
        self.lbl_ctrl.pack(anchor="w")
        ttk.Checkbutton(sk, text="Parar o serviço de injeção depois que o Special K entrar no jogo",
                        variable=self.var_sk_parar).pack(anchor="w", pady=(6, 0))
        ttk.Label(sk, wraplength=560, justify="left", text=(
            "Feche o SKIF antes de iniciar por aqui: ele também controla o serviço de injeção. "
            "O jogo precisa estar fechado; o .ini é gravado antes da abertura. "
            "'Identificar…' seleciona o controle em que você apertar um botão.")).pack(anchor="w", pady=(4, 0))

        acoes = ttk.Frame(p)
        acoes.pack(fill="x", pady=8)
        ttk.Button(acoes, text="Salvar perfil",
                   command=lambda: self.salvar_perfil_jogo(avisar=True)).pack(side="left")
        ttk.Button(acoes, text="Salvar e iniciar o jogo com este perfil",
                   command=self.iniciar_jogo).pack(side="left", padx=8)
        ttk.Button(acoes, text="Iniciar sem salvar",
                   command=lambda: self.iniciar_jogo(salvar=False)).pack(side="left")

        self.atualizar_telas()
        self.atualizar_controles_jogo()
        self.atualizar_audio_jogos()

    # --- telas e controles
    def atualizar_telas(self):
        escolhida = self.tela_do_form()
        self.telas = listar_telas()
        self.combo_tela_jogo["values"] = ["(não alterar)"] + [t["rotulo"] for t in self.telas]
        self.combo_tela_jogo.current(self.indice_tela(escolhida))

    def indice_tela(self, tela_salva):
        if tela_salva:
            for i, t in enumerate(self.telas):
                if t["dispositivo"] == tela_salva.get("dispositivo"):
                    return i + 1
        return 0

    def tela_do_form(self):
        i = self.combo_tela_jogo.current() - 1
        if 0 <= i < len(self.telas):
            return {"dispositivo": self.telas[i]["dispositivo"], "rotulo": self.telas[i]["rotulo"]}
        return None

    def atualizar_controles_jogo(self):
        escolhido = self.controle_do_form()
        self.controles_xi = listar_controles_xinput()
        self.controles_di = listar_controles_directinput()
        self.rotulos_j1 = rotulos_controle_por_posicao(self.controles_xi, self.controles_di)
        self.combo_j1_jogo["values"] = ["(não alterar)"] + [c["rotulo"] for c in self.controles_xi]
        self.combo_j1_jogo["values"] = ["(nao alterar)"] + self.rotulos_j1
        self.combo_j1_jogo.current(self.indice_controle(escolhido))
        n = len(self.controles_xi)
        self.lbl_ctrl.config(text="Nenhum controle XInput detectado. Conecte e clique em Atualizar."
                             if n == 0 else
                             "1 controle detectado." if n == 1 else f"{n} controles detectados.")

        if self.controles_di:
            self.lbl_ctrl.config(text=self.lbl_ctrl.cget("text") +
                                 "\nDirectInput/HID detectado: " +
                                 ", ".join(self.controles_di) +
                                 ". Os nomes sao apenas referencia; a troca usa a posicao "
                                 "Controle 1-4, como na versao anterior.")

    def indice_controle(self, salvo):
        """salvo = {"j1_slot", "j1_nome"} (ou None) -> índice no combobox (0 = não alterar)."""
        if salvo and isinstance(salvo.get("j1_slot"), int) and 0 <= salvo["j1_slot"] < 4:
            return salvo["j1_slot"] + 1
        if not salvo or salvo.get("j1_slot") is None:
            return 0
        mesmos = [i for i, c in enumerate(self.controles_xi) if c["nome"] == salvo.get("j1_nome")]
        if len(mesmos) == 1:
            return mesmos[0] + 1
        for i, c in enumerate(self.controles_xi):
            if c["slot"] == salvo["j1_slot"]:
                return i + 1
        return 0

    def controle_do_form(self):
        i = self.combo_j1_jogo.current() - 1
        if 0 <= i < 4:
            nome = self.rotulos_j1[i] if i < len(self.rotulos_j1) else None
            return {"j1_slot": i, "j1_nome": nome}
        return {"j1_slot": None, "j1_nome": None}

    def identificar_controle_ui(self):
        self.status.set("Aperte um botão no controle que o jogo deve usar (10 segundos)…")

        def depois(ok, slot):
            if not ok or slot is None:
                self.status.set("Nenhum botão foi apertado.")
                return
            self.atualizar_controles_jogo()
            for i, c in enumerate(self.controles_xi):
                if c["slot"] == slot:
                    self.combo_j1_jogo.current(i + 1)
                    self.status.set(f"Controle identificado: {c['rotulo']}.")
                    return

        self.em_segundo_plano(identificar_controle, depois)

    def atualizar_audio_jogos(self):
        if not hasattr(self, "combo_audio_jogo"):
            return
        atual = self.combo_audio_jogo.current()
        self.combo_audio_jogo["values"] = ["(não alterar)"] + [n for n, _ in self.saidas]
        self.combo_audio_jogo.current(atual if 0 <= atual < len(self.saidas) + 1 else 0)

    # --- lista de jogos
    def procurar_jogos(self):
        self.lbl_jogos.config(text="Procurando…")

        def depois(ok, res):
            if not ok:
                self.lbl_jogos.config(text="")
                messagebox.showerror("Erro", f"Não consegui listar os jogos:\n{res}")
                return
            self.jogos = res
            self.jogos_por_id = {j["id"]: j for j in res}
            self.migrar_perfis_antigos()
            self.preencher_lista_jogos()
            self.preencher_lista_perfis()

        self.em_segundo_plano(varrer_jogos, depois)

    def preencher_lista_jogos(self):
        filtro = self.busca_jogos.get().strip().lower()
        self.tab_jogos.delete(*self.tab_jogos.get_children())
        mostrados = 0
        for j in self.jogos:
            if filtro and filtro not in j["nome"].lower():
                continue
            try:
                self.tab_jogos.insert("", "end", iid=j["id"], values=(j["launcher"], j["nome"]))
                mostrados += 1
            except tk.TclError:
                continue
        contagem = {}
        for j in self.jogos:
            contagem[j["launcher"]] = contagem.get(j["launcher"], 0) + 1
        resumo = ", ".join(f"{k}: {v}" for k, v in contagem.items())
        self.lbl_jogos.config(text=f"{mostrados} de {len(self.jogos)} jogos" + (f" ({resumo})" if resumo else ""))

    def jogo_selecionado(self):
        return self.jogos_por_id.get(self.jogo_atual_id)

    def exe_do_jogo(self, jogo):
        exe = self.config["exes"].get(jogo["id"]) or jogo.get("exe")
        if not exe:
            if jogo["id"] not in self.cache_exe:
                self.cache_exe[jogo["id"]] = adivinhar_exe(jogo.get("pasta"))
            exe = self.cache_exe[jogo["id"]]
        return exe or ""

    def perfil_padrao(self, jogo):
        return {"jogo_id": jogo["id"], "nucleos": None, "prioridade_idx": None, "audio_id": None,
                "tela": None, "sk": achar_specialk()[0], "sk_bg": True, "j1_slot": None,
                "j1_nome": None, "sk_parar": True}

    def ao_selecionar_jogo(self, _):
        sel = self.tab_jogos.selection()
        if not sel or sel[0] == self.jogo_atual_id:
            return
        jogo = self.jogos_por_id.get(sel[0])
        if not jogo:
            return
        self.jogo_atual_id = jogo["id"]
        existente = self.config["perfis_jogos"].get(jogo["nome"])
        if existente and existente.get("jogo_id") == jogo["id"]:
            self.carregar_form(jogo, jogo["nome"], existente)
        else:
            self.carregar_form(jogo, jogo["nome"], self.perfil_padrao(jogo))

    def carregar_form(self, jogo, nome_perfil, perfil):
        self.var_nome_perfil.set(nome_perfil)
        self.var_exe_jogo.set(self.exe_do_jogo(jogo))
        nucleos = perfil.get("nucleos")
        self.var_usar_cpu.set(bool(nucleos))
        for i, var in enumerate(self.vars_cpu_jogo):
            var.set(True if not nucleos else i in nucleos)
        idx = perfil.get("prioridade_idx")
        self.combo_prio_jogo.current(0 if idx is None else idx + 1)
        indice_audio = 0
        for i, (_, ident) in enumerate(self.saidas):
            if ident == perfil.get("audio_id"):
                indice_audio = i + 1
        self.combo_audio_jogo.current(indice_audio)
        self.telas = listar_telas()
        self.combo_tela_jogo["values"] = ["(não alterar)"] + [t["rotulo"] for t in self.telas]
        self.combo_tela_jogo.current(self.indice_tela(perfil.get("tela")))
        self.controles_xi = listar_controles_xinput()
        self.combo_j1_jogo["values"] = ["(não alterar)"] + [c["rotulo"] for c in self.controles_xi]
        self.combo_j1_jogo["values"] = ["(nao alterar)"] + self.rotulos_j1
        self.controles_di = listar_controles_directinput()
        self.rotulos_j1 = rotulos_controle_por_posicao(self.controles_xi, self.controles_di)
        self.combo_j1_jogo["values"] = ["(nao alterar)"] + self.rotulos_j1
        self.combo_j1_jogo.current(self.indice_controle(perfil))
        self.var_sk.set(perfil.get("sk", True))
        self.var_sk_bg.set(perfil.get("sk_bg", True))
        self.var_sk_parar.set(perfil.get("sk_parar", True))

    def escolher_exe_jogo(self):
        jogo = self.jogo_selecionado()
        inicial = jogo["pasta"] if jogo and os.path.isdir(jogo.get("pasta") or "") else None
        caminho = filedialog.askopenfilename(
            title="Escolha o executável do jogo", initialdir=inicial,
            filetypes=[("Executável", "*.exe")])
        if caminho:
            self.var_exe_jogo.set(os.path.normpath(caminho))

    # --- perfis nomeados
    def preencher_lista_perfis(self):
        self.tab_perfis_jogos.delete(*self.tab_perfis_jogos.get_children())
        for nome in sorted(self.config["perfis_jogos"], key=str.lower):
            perfil = self.config["perfis_jogos"][nome]
            jogo = self.jogos_por_id.get(perfil.get("jogo_id"))
            try:
                self.tab_perfis_jogos.insert("", "end", iid=nome, values=(
                    nome, jogo["nome"] if jogo else "(jogo não encontrado)"))
            except tk.TclError:
                continue

    def migrar_perfis_antigos(self):
        antigos = self.config.get("jogos") or {}
        mudou = False
        for jid, p in list(antigos.items()):
            jogo = self.jogos_por_id.get(jid)
            if not jogo:
                continue
            if p.get("exe"):
                self.config["exes"].setdefault(jid, p["exe"])
            if jogo["nome"] not in self.config["perfis_jogos"]:
                j1 = p.get("j1", 4)
                self.config["perfis_jogos"][jogo["nome"]] = {
                    "jogo_id": jid, "nucleos": p.get("nucleos"),
                    "prioridade_idx": p.get("prioridade_idx"), "audio_id": p.get("audio_id"),
                    "tela": None, "sk": p.get("sk", True), "sk_bg": p.get("sk_bg", True),
                    "j1_slot": j1 if isinstance(j1, int) and j1 < 4 else None, "j1_nome": None,
                    "sk_parar": p.get("sk_parar", True)}
            del antigos[jid]
            mudou = True
        if mudou:
            salvar_config(self.config)

    def ao_selecionar_perfil(self, _):
        sel = self.tab_perfis_jogos.selection()
        if not sel:
            return
        nome = sel[0]
        perfil = self.config["perfis_jogos"].get(nome)
        jogo = self.jogos_por_id.get(perfil.get("jogo_id")) if perfil else None
        if not jogo:
            self.status.set("O jogo deste perfil não está instalado ou não foi encontrado.")
            return
        self.jogo_atual_id = jogo["id"]
        self.carregar_form(jogo, nome, perfil)
        if self.tab_jogos.exists(jogo["id"]):
            self.tab_jogos.selection_set(jogo["id"])
            self.tab_jogos.see(jogo["id"])

    def iniciar_perfil_selecionado(self):
        if not self.tab_perfis_jogos.selection():
            messagebox.showinfo("Perfis", "Selecione um perfil salvo na lista.")
            return
        self.ao_selecionar_perfil(None)
        self.iniciar_jogo()

    def excluir_perfil(self):
        sel = self.tab_perfis_jogos.selection()
        if not sel:
            return
        if messagebox.askyesno("Excluir perfil", f"Excluir o perfil '{sel[0]}'?"):
            self.config["perfis_jogos"].pop(sel[0], None)
            salvar_config(self.config)
            self.preencher_lista_perfis()

    def salvar_perfil_jogo(self, avisar=False):
        """Salva o formulário como perfil nomeado. Devolve (nome, perfil) ou None."""
        jogo = self.jogo_selecionado()
        if not jogo:
            messagebox.showwarning("Aviso", "Selecione um jogo na lista.")
            return None
        nome = self.var_nome_perfil.get().strip() or jogo["nome"]
        existente = self.config["perfis_jogos"].get(nome)
        if existente and existente.get("jogo_id") != jogo["id"] and not messagebox.askyesno(
                "Perfil existente", f"Já existe um perfil '{nome}' para outro jogo. Substituir?"):
            return None
        nucleos = [i for i, v in enumerate(self.vars_cpu_jogo) if v.get()] if self.var_usar_cpu.get() else None
        if self.var_usar_cpu.get() and not nucleos:
            messagebox.showwarning("Aviso", "Marque pelo menos um núcleo (ou desmarque 'Definir núcleos').")
            return None
        idx_prio = self.combo_prio_jogo.current() - 1
        idx_audio = self.combo_audio_jogo.current() - 1
        perfil = {"jogo_id": jogo["id"], "nucleos": nucleos,
                  "prioridade_idx": idx_prio if idx_prio >= 0 else None,
                  "audio_id": self.saidas[idx_audio][1] if idx_audio >= 0 else None,
                  "tela": self.tela_do_form(), "sk": self.var_sk.get(), "sk_bg": self.var_sk_bg.get(),
                  "sk_parar": self.var_sk_parar.get()}
        perfil.update(self.controle_do_form())
        exe = self.var_exe_jogo.get().strip()
        if exe:
            self.config["exes"][jogo["id"]] = exe
        self.config["perfis_jogos"][nome] = perfil
        salvar_config(self.config)
        self.preencher_lista_perfis()
        if self.tab_perfis_jogos.exists(nome):
            self.tab_perfis_jogos.selection_set(nome)
        self.var_nome_perfil.set(nome)
        if avisar:
            self.status.set(f"Perfil salvo: {nome}.")
        return nome, perfil

    def registrar_regra_cpu(self, exe, perfil):
        if not exe or (perfil.get("nucleos") is None and perfil.get("prioridade_idx") is None):
            return
        idx = perfil.get("prioridade_idx")
        self.regras[os.path.basename(exe).lower()] = {
            "nucleos": perfil.get("nucleos"),
            "prioridade": PRIORIDADES[idx][1] if idx is not None else None,
            "filhos": True}
        self.atualizar_regras()

    def perfil_temporario_jogo(self, jogo):
        """Monta um perfil do formulario sem altera-lo no arquivo de configuracao."""
        nucleos = ([i for i, var in enumerate(self.vars_cpu_jogo) if var.get()]
                   if self.var_usar_cpu.get() else None)
        if self.var_usar_cpu.get() and not nucleos:
            messagebox.showwarning("Aviso", "Marque pelo menos um nucleo (ou desmarque 'Definir nucleos').")
            return None
        idx_prio = self.combo_prio_jogo.current() - 1
        idx_audio = self.combo_audio_jogo.current() - 1
        perfil = {
            "jogo_id": jogo["id"], "nucleos": nucleos,
            "prioridade_idx": idx_prio if idx_prio >= 0 else None,
            "audio_id": self.saidas[idx_audio][1] if idx_audio >= 0 else None,
            "tela": self.tela_do_form(), "sk": self.var_sk.get(),
            "sk_bg": self.var_sk_bg.get(), "sk_parar": self.var_sk_parar.get(),
        }
        perfil.update(self.controle_do_form())
        return perfil

    def iniciar_jogo(self, salvar=True):
        jogo = self.jogo_selecionado()
        if not jogo:
            messagebox.showwarning("Aviso", "Selecione um jogo na lista.")
            return
        if salvar:
            salvo = self.salvar_perfil_jogo()
            if salvo is None:
                return
            nome_perfil, perfil = salvo
            exe = self.config["exes"].get(jogo["id"]) or self.var_exe_jogo.get().strip()
        else:
            perfil = self.perfil_temporario_jogo(jogo)
            if perfil is None:
                return
            nome_perfil = "temporario"
            exe = self.var_exe_jogo.get().strip() or self.config["exes"].get(jogo["id"])
        if not exe or not os.path.exists(exe):
            messagebox.showwarning("Executável não encontrado",
                                   "Não achei o executável do jogo. Use o botão 'Procurar…' "
                                   "para escolher o .exe principal dele.")
            return
        if jogo_em_execucao(exe):
            messagebox.showwarning("Jogo aberto", "O jogo já está aberto. Feche-o antes de iniciar "
                                                  "por aqui (o Special K precisa entrar na abertura).")
            return
        usar_sk = perfil["sk"]
        servicos = []
        if usar_sk:
            if not achar_specialk()[0]:
                messagebox.showerror("Special K", "O Special K não está instalado. Use o botão Componentes.")
                return
            slot = None
            if perfil.get("j1_slot") is not None:
                slot = perfil["j1_slot"] if 0 <= perfil["j1_slot"] < 4 else None
                if slot is None and not messagebox.askyesno(
                        "Controle",
                        "O controle escolhido neste perfil não está conectado agora.\n\n"
                        "Iniciar mesmo assim, sem trocar o controle?"):
                    return
            try:
                escrever_edicoes(escolher_ini(exe)[0], edicoes_specialk(perfil["sk_bg"], slot))
                adicionar_whitelist(os.path.basename(exe))
            except OSError as e:
                messagebox.showerror("Special K", f"Não consegui preparar o Special K:\n{e}")
                return
            servicos = achar_skifsvc()
            if servicos:
                iniciar_servicos(servicos)
            else:
                messagebox.showwarning(
                    "Special K",
                    "Não achei o SKIFsvc (serviço de injeção) na pasta do Special K. O jogo será "
                    "aberto mesmo assim, mas o Special K só entrará se o serviço estiver rodando "
                    "(por exemplo, iniciado pelo SKIF).")
        tela = None
        if perfil.get("tela"):
            tela = next((t for t in listar_telas()
                         if t["dispositivo"] == perfil["tela"]["dispositivo"]), None)
            if tela is None:
                self.status.set("A tela escolhida no perfil não está conectada; o jogo abrirá onde o Windows quiser.")
        self.ultimo_perfil_lancado[jogo["id"]] = perfil
        self.registrar_regra_cpu(exe, perfil)
        try:
            abrir_jogo(jogo["lancar"])
        except OSError as e:
            if servicos:
                parar_servicos(servicos)
            messagebox.showerror("Erro", f"Não consegui abrir o jogo:\n{e}")
            return
        self.status.set(f"Iniciando {jogo['nome']} (perfil {nome_perfil})…")
        enviar = self.fila_msgs.put
        threading.Thread(
            target=vigiar_lancamento,
            args=(jogo["nome"], exe, jogo.get("pasta"), perfil.get("audio_id"), usar_sk,
                  perfil["sk_parar"], servicos, tela,
                  lambda tipo, dado, j=jogo["id"]: enviar((tipo, dado, j))),
            daemon=True).start()

    def drenar_fila(self):
        try:
            while True:
                tipo, dado, id_jogo = self.fila_msgs.get_nowait()
                if tipo == "status":
                    self.status.set(dado)
                elif tipo == "exe":
                    self.config["exes"][id_jogo] = dado
                    salvar_config(self.config)
                    if id_jogo == self.jogo_atual_id:
                        self.var_exe_jogo.set(dado)
                    perfil = self.ultimo_perfil_lancado.get(id_jogo)
                    if perfil:
                        self.registrar_regra_cpu(dado, perfil)
                    self.status.set(f"Executável do jogo atualizado para {os.path.basename(dado)} "
                                    "(o Special K entra com ele na próxima abertura).")
        except queue.Empty:
            pass
        self.after(500, self.drenar_fila)


if __name__ == "__main__":
    App().mainloop()
