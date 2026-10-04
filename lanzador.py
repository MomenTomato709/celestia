#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lanzador de escritorio: doble clic → Celestia encendida y abierta.

Es lo que ejecuta el acceso directo del instalador (Windows, Mac, Linux). En
el móvil no se usa: allí sigue mandando `arrancar.sh`.

1. Si ya hay una Celestia encendida en este aparato, sólo la abre.
2. Si no, la arranca en un puerto libre y espera a que conteste.
3. La abre en SU PROPIA VENTANA, como cualquier programa (pywebview: el motor
   web del sistema — Edge WebView2 en Windows, WebKit en Mac). Cerrar la
   ventana la apaga, salvo que la persona haya pedido en Ajustes que siga
   despierta para avisarle (celestia_lib/escritorio.py): un servidor que se
   queda vivo sin que nadie lo haya pedido es peor.

`--segundo-plano` (lo que se apunta en el arranque del sistema): la enciende
sin ventana y se va; abrir Celestia después sólo abre la ventana.

Si no hay con qué hacer la ventana (un Linux sin GTK ni Qt), vuelve a lo de
antes: una ventanita con «Abrir el chat» y «Apagar», y el chat en el navegador.
Sin `tkinter` tampoco, lo hace sin ventana y se apaga con Ctrl+C.
"""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
# El primero se puede elegir (CELESTIA_PUERTO): para tener dos en el mismo
# aparato, o para probar una instalación sin tropezar con la de verdad.
_PRIMERO = int(os.environ.get("CELESTIA_PUERTO", "") or 8765)
PUERTOS = range(_PRIMERO, _PRIMERO + 20)
ESPERA_MAX_SEG = 120


def _es_celestia(puerto: int, espera: float = 10) -> bool:
    # 10 s y no 3: en Windows /estado llegó a tardar 3,5 s (3 oct 2026). Con 3,
    # una Celestia ya encendida parecía apagada y se arrancaba otra al lado.
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{puerto}/estado", timeout=espera) as r:
            return "version" in json.loads(r.read(100_000) or b"{}")
    except Exception:
        return False


def _libre(puerto: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", puerto))
            return True
        except OSError:
            return False


def _zona_del_sistema() -> str:
    """El nombre IANA de la zona del aparato («America/Bogota»). Celestia la
    necesita para los recordatorios; si no, usaría la de Madrid."""
    try:
        import tzlocal
        nombre = tzlocal.get_localzone_name()
        if nombre:
            return nombre
    except Exception:
        pass
    try:                                  # Linux y Mac sin tzlocal
        destino = os.path.realpath("/etc/localtime")
        if "zoneinfo/" in destino:
            return destino.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return ""


def _seguir_despierta() -> bool:
    """Ajustes → «Seguir despierta con la ventana cerrada» (escritorio.py)."""
    try:
        datos = json.loads((RAIZ / "memoria" / "escritorio.json").read_text(encoding="utf-8"))
        return bool(datos.get("segundo_plano"))
    except (OSError, ValueError, AttributeError):
        return False


def _del_env(clave: str) -> str:
    try:
        for linea in (RAIZ / ".env").read_text(encoding="utf-8").splitlines():
            if linea.strip().startswith(f"{clave}="):
                return linea.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


class Lanzador:
    def __init__(self) -> None:
        self.proceso: subprocess.Popen | None = None
        self.puerto = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.puerto}/chat"

    def encender(self) -> str:
        """Deja una Celestia contestando. Devuelve un error legible o ""."""
        for p in PUERTOS:
            # Un puerto libre no tiene a nadie detrás: preguntarle en Windows
            # cuesta ~2 s por puerto (reintenta la conexión rechazada).
            if not _libre(p) and _es_celestia(p):
                self.puerto = p            # ya estaba encendida
                return ""
        libre = next((p for p in PUERTOS if _libre(p)), 0)
        if not libre:
            return (f"Todos los puertos que uso ({PUERTOS.start}-{PUERTOS.stop - 1}) "
                    "están ocupados.")
        self.puerto = libre

        entorno = dict(os.environ)
        if not (entorno.get("CELESTIA_TZ") or _del_env("CELESTIA_TZ")):
            zona = _zona_del_sistema()
            if zona:
                entorno["CELESTIA_TZ"] = zona
        entorno.setdefault("PYTHONIOENCODING", "utf-8")
        # En Windows, open() sin encoding lee en cp1252 y rompe las tildes del
        # .env y de la memoria. Con esto, UTF-8 en todas partes.
        entorno.setdefault("PYTHONUTF8", "1")
        (RAIZ / "logs").mkdir(exist_ok=True)
        salida = open(RAIZ / "logs" / "lanzador.log", "ab")
        extra = {}
        if sys.platform == "win32":
            extra["creationflags"] = 0x08000000      # CREATE_NO_WINDOW
        else:
            # En su propia sesión: si se pidió que siga despierta, cerrar la
            # app (en el Mac, launchd recoge el grupo de procesos de la app al
            # salir) o la terminal no se la lleva por delante.
            extra["start_new_session"] = True
        # El acceso directo usa pythonw.exe (sin ventana negra), pero el hijo
        # va con python.exe: con pythonw no hay salida estándar y cualquier
        # print() de Celestia fallaría. CREATE_NO_WINDOW evita la ventana igual.
        python = Path(sys.executable)
        if python.name.lower() == "pythonw.exe" and (python.parent / "python.exe").exists():
            python = python.parent / "python.exe"
        self.proceso = subprocess.Popen(
            [str(python), str(RAIZ / "celestia.py"),
             "--modo", "whatsapp", "--wa-puerto", str(libre)],
            cwd=str(RAIZ), env=entorno, stdout=salida, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, **extra)

        limite = time.time() + ESPERA_MAX_SEG
        while time.time() < limite:
            if self.proceso.poll() is not None:
                return ("Celestia se ha cerrado al arrancar. Lo que pasó está en "
                        f"{RAIZ / 'logs' / 'lanzador.log'}")
            if _es_celestia(libre):
                return ""
            time.sleep(1)
        return "Celestia tarda demasiado en arrancar. Prueba a abrirla otra vez."

    def abrir_chat(self) -> None:
        webbrowser.open(self.url)

    def apagar(self) -> None:
        if self.proceso and self.proceso.poll() is None:
            self.proceso.terminate()
            try:
                self.proceso.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proceso.kill()


_ESTILO_PANTALLA = (
    "<style>html,body{height:100%;margin:0;background:#0B0D17;color:#E8E6FF;"
    "font:15px/1.6 system-ui,-apple-system,'Segoe UI',sans-serif}"
    "body{display:grid;place-items:center;text-align:center}"
    ".orbe{width:74px;height:74px;margin:0 auto 22px;border-radius:50%;"
    "background:radial-gradient(circle at 50% 50%,#E9E3FF 0,#A78BFA 18%,transparent 46%),"
    "radial-gradient(circle,#2A2352 0,#0B0D17 70%);box-shadow:0 0 40px -6px #8B7CF6;"
    "animation:latir 1.6s ease-in-out infinite}"
    "@keyframes latir{50%{transform:scale(1.07);box-shadow:0 0 56px -4px #8B7CF6}}"
    ".nombre{letter-spacing:.34em;font-weight:600;text-transform:uppercase;"
    "background:linear-gradient(100deg,#8B7CF6,#22D3EE);-webkit-background-clip:text;"
    "background-clip:text;color:transparent}"
    "p{color:#9A98B8;margin:6px 24px 0;max-width:460px}</style>")

PANTALLA_CARGA = (
    "<!doctype html><html lang='es'><meta charset='utf-8'>" + _ESTILO_PANTALLA +
    "<body><div><div class='orbe'></div><div class='nombre'>Celestia</div>"
    "<p>Encendiéndome…</p></div></body></html>")


def _pantalla_error(texto: str) -> str:
    import html
    return ("<!doctype html><html lang='es'><meta charset='utf-8'>" + _ESTILO_PANTALLA +
            "<body><div><div class='nombre'>Celestia</div>"
            f"<p>{html.escape(texto)}</p></div></body></html>")


def _version() -> str:
    """La de celestia_lib/version.py, leída como texto (el lanzador no carga
    Celestia entera para esto)."""
    try:
        texto = (RAIZ / "celestia_lib" / "version.py").read_text(encoding="utf-8")
        return texto.split('VERSION = "', 1)[1].split('"', 1)[0]
    except (OSError, IndexError):
        return "0"


def _icono() -> Path | None:
    """El icono de la ventana: el .ico que deja el instalador en Windows, el
    .png en Linux, o el logo del repositorio."""
    candidatos = ([RAIZ / "celestia.ico"] if sys.platform == "win32" else []) + [
        RAIZ / "celestia.png", RAIZ / "imagenes" / "celestia_logo.png"]
    return next((p for p in candidatos if p.is_file()), None)


def _con_app(lz: Lanzador) -> bool:
    """Celestia en su propia ventana. False si no se puede (y entonces se
    vuelve a la ventanita con el navegador)."""
    if os.environ.get("CELESTIA_SIN_VENTANA_PROPIA"):
        return False
    try:
        import webview
    except Exception:
        return False
    if sys.platform == "win32":
        # Sin esto la barra de tareas la agrupa con cualquier otro Python
        # abierto y le pone su icono.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Celestia.App")
        except Exception:
            pass
    # Las descargas de la pestaña Archivos (documentos, proyectos en ZIP).
    webview.settings["ALLOW_DOWNLOADS"] = True
    ventana = webview.create_window(
        "Celestia", html=PANTALLA_CARGA, width=1120, height=820,
        min_size=(380, 520), background_color="#0B0D17")

    # Celestia se enciende YA, en paralelo, y no cuando la ventana termine de
    # salir: si el motor web tarda o se atasca, el servidor arranca igual (y
    # la prueba de los instaladores, que le habla por la API, no depende de él).
    resultado: dict = {}
    encendiendo = threading.Thread(
        target=lambda: resultado.update(error=lz.encender()), daemon=True)
    encendiendo.start()

    def arrancar() -> None:
        encendiendo.join()
        if resultado.get("error"):
            ventana.load_html(_pantalla_error(resultado["error"]))
        else:
            ventana.load_url(lz.url)

    icono = _icono()
    try:
        # NO en modo privado: el chat guarda en el navegador el tema, los
        # ajustes y la llave, y en modo privado se perdían al cerrar.
        webview.start(arrancar, private_mode=False,
                      storage_path=str(RAIZ / "memoria" / "ventana"),
                      icon=str(icono) if icono else None,
                      # Para que el chat sepa que está en la app: los avisos
                      # los manda la app, no el navegador (chat.html, EN_APP).
                      user_agent=f"Mozilla/5.0 (Celestia) CelestiaApp/{_version()}")
    except Exception as e:
        # Sin motor web (un Linux sin GTK ni Qt): a lo de siempre. Se espera a
        # que termine de encenderse para que la ventanita la encuentre ya
        # encendida y no arranque otra al lado.
        print(f"Sin ventana propia ({e}); abro el chat en el navegador.", file=sys.stderr)
        encendiendo.join()
        return False
    return True


NAVEGADORES_APP = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                   "microsoft-edge", "microsoft-edge-stable", "brave-browser")


def _con_navegador_app(lz: Lanzador) -> bool:
    """En Linux, la ventana propia sin pywebview (que pediría GTK o Qt del
    sistema): un Chrome/Chromium/Edge/Brave en «modo aplicación» — su propia
    ventana, sin barra de direcciones ni pestañas — con un perfil SÓLO para
    Celestia, así que es un proceso aparte y cerrarlo apaga Celestia.
    False si no hay ninguno (entonces, la ventanita y el navegador)."""
    if not sys.platform.startswith("linux") or os.environ.get("CELESTIA_SIN_VENTANA_PROPIA"):
        return False
    # Sin pantalla (un servidor, las máquinas de prueba de GitHub) no hay
    # ventana que abrir aunque esté Chrome instalado.
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    import shutil
    programa = next((p for p in map(shutil.which, NAVEGADORES_APP) if p), None)
    if not programa:
        return False
    error = lz.encender()
    if error:
        print(error, file=sys.stderr)
        return False
    perfil = RAIZ / "memoria" / "ventana-linux"
    perfil.mkdir(parents=True, exist_ok=True)
    ventana = subprocess.Popen(
        [programa, f"--app={lz.url}", f"--user-data-dir={perfil}", "--class=Celestia",
         "--no-first-run", "--no-default-browser-check", "--window-size=1120,820"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    inicio = time.time()
    salida = ventana.wait()
    # Si se cierra al instante con error, no llegó a abrirse: a lo de siempre
    # (Celestia ya está encendida y la ventanita la encontrará).
    if salida != 0 and time.time() - inicio < 5:
        return False
    return True


def _con_ventana(lz: Lanzador) -> bool:
    try:
        import tkinter as tk
    except Exception:
        return False
    try:
        raiz = tk.Tk()
    except Exception:                     # sin pantalla (un servidor)
        return False
    raiz.title("Celestia")
    raiz.resizable(False, False)
    raiz.configure(bg="#0b0d17", padx=22, pady=18)
    icono = RAIZ / "imagenes" / "celestia_logo.png"
    try:
        raiz.iconphoto(True, tk.PhotoImage(file=str(icono)))
    except Exception:
        pass
    estado = tk.StringVar(value="Encendiendo Celestia…")
    tk.Label(raiz, textvariable=estado, fg="#e8e6ff", bg="#0b0d17",
             font=("Segoe UI", 12), wraplength=300, justify="left").pack(anchor="w")
    tk.Label(raiz, text="Si cierras esta ventana, Celestia se apaga.",
             fg="#8b8aa8", bg="#0b0d17", font=("Segoe UI", 9)).pack(anchor="w", pady=(4, 12))
    botones = tk.Frame(raiz, bg="#0b0d17")
    botones.pack(anchor="w")
    abrir = tk.Button(botones, text="Abrir el chat", state="disabled",
                      command=lz.abrir_chat, padx=12, pady=4)
    abrir.pack(side="left")

    def salir() -> None:
        estado.set("Apagando…")
        raiz.update_idletasks()
        lz.apagar()
        raiz.destroy()

    tk.Button(botones, text="Apagar", command=salir, padx=12, pady=4).pack(side="left", padx=(8, 0))
    raiz.protocol("WM_DELETE_WINDOW", salir)

    def arrancar() -> None:
        error = lz.encender()

        def listo() -> None:
            if error:
                estado.set(error)
                return
            estado.set("Celestia está encendida.")
            abrir.configure(state="normal")
            lz.abrir_chat()
        raiz.after(0, listo)

    threading.Thread(target=arrancar, daemon=True).start()
    raiz.mainloop()
    return True


def main() -> int:
    lz = Lanzador()
    # Sin esto, un SIGTERM (apagar el PC, `kill`, cerrar la sesión) mataba al
    # lanzador sin pasar por `finally` y Celestia se quedaba viva y huérfana.
    def _al_terminar(*_):
        raise SystemExit(0)
    for nombre in ("SIGTERM", "SIGHUP"):
        if hasattr(signal, nombre):
            signal.signal(getattr(signal, nombre), _al_terminar)
    if "--segundo-plano" in sys.argv[1:]:
        # Al encender el ordenador: Celestia despierta, sin ventana, y el
        # lanzador se va. No se apaga al salir (es justo lo que se pidió).
        error = lz.encender()
        if error:
            print(error, file=sys.stderr)
        return 1 if error else 0
    try:
        if _con_app(lz):
            return 0
        if _con_navegador_app(lz):
            return 0
        if _con_ventana(lz):
            return 0
        print("Encendiendo Celestia…", flush=True)
        error = lz.encender()
        if error:
            print(error, file=sys.stderr)
            return 1
        print(f"Celestia está encendida: {lz.url}  (Ctrl+C para apagarla)", flush=True)
        lz.abrir_chat()
        if lz.proceso:
            lz.proceso.wait()
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        # Con «seguir despierta», cerrar la ventana no la apaga: así puede
        # avisar de un recordatorio a las nueve aunque no haya ventana. Se
        # apaga desde Ajustes → «Apagar Celestia».
        if not _seguir_despierta():
            lz.apagar()


if __name__ == "__main__":
    sys.exit(main())
