"""Avisos al móvil: Celestia avisa aunque la app esté cerrada.

En la Celestia INSTALADA (3 oct 2026) van primero los avisos propios: los de
la app de Android (`NATIVO_APP`) y los del sistema en un PC (`_escritorio`:
Windows, Mac, Linux). Lo que sigue es la Celestia del móvil de Enzo.

Dos caminos, en este orden:

1. **Nativo**: la notificación la publica Android directamente. Primero con
   `termux-notification` (la API viene dentro del Termux de Google Play): al
   tocarlo abre el chat y no necesita Shizuku. Si no sale, por el canal de
   Shizuku (`cmd notification post`). Es el bueno en el móvil de Enzo.
   Medido el 23 sep 2026 en su Nothing Phone con Android 16: el push de Chrome
   llega a Google (201) y despierta a Chrome, pero Android lo congela
   (`isFrozen=true`) antes de enseñarlo, y el aviso sale al volver a abrir la
   app, 13 minutos después; con la app en segundo plano, igual. El nativo sale
   al momento. El de Shizuku no abre nada al tocarlo (Android no deja que el
   shell ponga una acción que se haga pasar por otra app); el de Termux sí.
2. **Push web**, si el nativo no puede (Shizuku parado tras un reinicio, otro
   aparato):

El chat web ya sacaba avisos del sistema, pero sólo con la pestaña abierta: lo
que tenía que avisar era el propio JavaScript de la página. Con la app
instalada (PWA) el aviso lo saca el service worker, que Chrome despierta cuando
llega un push aunque no haya ninguna ventana de Celestia abierta.

El camino: Celestia firma el mensaje con su clave VAPID y lo manda al servicio
de push del navegador (el de Chrome es de Google, gratis); ese servicio lo
entrega al móvil. El contenido va cifrado de punta a punta con las claves de
la suscripción: el intermediario lo transporta pero no lo puede leer.

Aquí sólo se AVISA. El recado sigue en la bandeja (`bandeja.py`) y el chat lo
recoge al abrirse como siempre: si un push se pierde, no se pierde nada más.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .paths import ES_ANDROID, LOG_DIR, MEM_DIR, ROOT, SIN_MOVIL

logger = logging.getLogger("celestia_v1")

# Las dos fuera de git (`memoria/` está en .gitignore): la clave privada firma
# en nombre de Celestia y las suscripciones son direcciones para escribirle al
# móvil.
CLAVES_FILE = MEM_DIR / "push_vapid.json"
SUSCRIPCIONES_FILE = MEM_DIR / "push_suscripciones.json"

# El servicio de push exige un contacto para el remitente.
CONTACTO = "mailto:celestia@localhost"

# Un aviso es un aviso: el recado entero se lee al abrir la app.
MAX_CUERPO = 180

_lock = threading.Lock()

# Sólo manda quien lo pide expresamente: `celestia.py` al arrancar el servidor
# de verdad (`habilitar()`). Nada de adivinar si esto es un test por el nombre
# del proceso (Codex, 23 sep 2026): un test que no lo llama no puede mandar
# nada, lo lance quien lo lance.
_habilitado = False
# Donde escucha el chat: el aviso de Termux lo abre al tocarlo.
_puerto = 8765

# Los servicios de push de los navegadores. Celestia hace POST a la dirección
# de la suscripción, así que sólo se aceptan éstas: ni un servidor ajeno que
# se quede con los recados, ni una dirección de la red de casa.
_SERVICIOS_PUSH = ("fcm.googleapis.com", "updates.push.services.mozilla.com",
                   "web.push.apple.com", "notify.windows.com")


def habilitar(puerto: int = 8765) -> None:
    global _habilitado, _puerto
    _habilitado = True
    _puerto = puerto
    threading.Thread(target=preparar_abrir_app, daemon=True, name="push-abrir-app").start()


def _escribir_json(ruta: Path, datos: Any) -> None:
    tmp = ruta.with_suffix(".tmp")
    tmp.write_text(json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, ruta)


def _b64url(crudo: bytes) -> str:
    return base64.urlsafe_b64encode(crudo).rstrip(b"=").decode("ascii")


def claves() -> Dict[str, str]:
    """El par VAPID de Celestia, creado la primera vez que se pide.

    `publica` es la que el navegador necesita para suscribirse (formato
    applicationServerKey); `privada_pem` firma los envíos.
    """
    with _lock:
        try:
            datos = json.loads(CLAVES_FILE.read_text(encoding="utf-8"))
            if datos.get("publica") and datos.get("privada_pem"):
                return datos
        except FileNotFoundError:
            pass
        except Exception as e:
            # Un fichero roto NO se pisa: con otra clave, todas las
            # suscripciones que ya existen dejarían de valer sin avisar.
            raise RuntimeError(f"no puedo leer las claves de push: {e}") from e
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        privada = ec.generate_private_key(ec.SECP256R1())
        publica = privada.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        datos = {
            "publica": _b64url(publica),
            "privada_pem": privada.private_bytes(
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption()).decode("ascii"),
        }
        _escribir_json(CLAVES_FILE, datos)
        logger.info("Push: claves VAPID nuevas creadas")
        return datos


def _suscripciones() -> List[Dict[str, Any]]:
    try:
        datos = json.loads(SUSCRIPCIONES_FILE.read_text(encoding="utf-8"))
        return datos if isinstance(datos, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _valida(sus: Any) -> bool:
    """Una suscripción tal como la da `PushSubscription.toJSON()`. El endpoint
    tiene que ser https: es la dirección a la que Celestia va a hacer POST, y
    no puede ser una puerta para que alguien la mande a la red de casa."""
    if not isinstance(sus, dict):
        return False
    claves_sus = sus.get("keys") or {}
    endpoint = str(sus.get("endpoint") or "")
    from urllib.parse import urlsplit
    try:
        partes = urlsplit(endpoint)
        host = (partes.hostname or "").lower()
    except ValueError:
        return False
    conocido = any(host == d or host.endswith("." + d) for d in _SERVICIOS_PUSH)
    return (partes.scheme == "https" and conocido and partes.port in (None, 443)
            and len(endpoint) < 1000
            and isinstance(claves_sus, dict)
            and bool(claves_sus.get("p256dh")) and bool(claves_sus.get("auth")))


def suscribir(sus: Dict[str, Any]) -> bool:
    if not _valida(sus):
        return False
    limpia = {"endpoint": sus["endpoint"],
              "keys": {"p256dh": sus["keys"]["p256dh"], "auth": sus["keys"]["auth"]}}
    with _lock:
        lista = [s for s in _suscripciones() if s.get("endpoint") != limpia["endpoint"]]
        lista.append(limpia)
        # Un móvil, una tablet, el portátil… no hacen falta más.
        _escribir_json(SUSCRIPCIONES_FILE, lista[-10:])
    return True


def dar_de_baja(endpoint: str) -> None:
    with _lock:
        lista = _suscripciones()
        nueva = [s for s in lista if s.get("endpoint") != endpoint]
        if len(nueva) != len(lista):
            _escribir_json(SUSCRIPCIONES_FILE, nueva)


def hay_suscripciones() -> bool:
    return bool(_suscripciones())


def _enviar_a_todas(carga: Dict[str, Any]) -> int:
    if _apagado():
        return 0
    from pywebpush import WebPushException, webpush
    try:
        privada = claves()["privada_pem"]
    except Exception as e:
        logger.warning("Push: sin claves, no aviso (%s)", e)
        return 0
    from py_vapid import Vapid
    vapid = Vapid.from_pem(privada.encode("ascii"))
    enviados, caducadas = 0, []
    for sus in _suscripciones():
        try:
            webpush(subscription_info=sus, data=json.dumps(carga, ensure_ascii=False),
                    vapid_private_key=vapid, vapid_claims={"sub": CONTACTO},
                    ttl=24 * 3600, timeout=10,
                    # Sin esto va con prioridad normal y Android, ahorrando
                    # batería, lo guarda hasta que el móvil despierte: un
                    # recordatorio que llega tarde no sirve.
                    headers={"Urgency": "high"})
            enviados += 1
        except WebPushException as e:
            codigo = getattr(e.response, "status_code", None)
            # 404/410: el navegador la dio de baja (desinstalada, permisos
            # quitados). Guardarla sólo haría fallar cada aviso futuro.
            if codigo in (404, 410):
                caducadas.append(sus.get("endpoint"))
            logger.warning("Push: falló un envío (%s)", codigo or e)
        except Exception as e:
            logger.warning("Push: falló un envío (%s)", e)
    for endpoint in caducadas:
        dar_de_baja(endpoint)
    # El envío bueno también se apunta: sin esto no hay forma de saber, sin
    # preguntarle a Enzo, si un aviso salió.
    logger.info("Push: aviso entregado a %d aparato(s)", enviados)
    return enviados


def _apagado() -> bool:
    """Las suscripciones del disco son las del móvil de Enzo: un test o una
    copia examinándose no pueden hacerlo vibrar (la trampa de la sesión 58).
    Por eso hace falta el permiso de `habilitar()`, y además se respetan las
    redes de siempre por si alguien lo llama desde donde no debe."""
    if not _habilitado:
        return True
    if os.environ.get("CELESTIA_SIN_PUSH", "").strip() in ("1", "true", "sí", "si"):
        return True
    return bool(os.environ.get("CELESTIA_TESTS")) or "pytest" in sys.modules


RISH = "/data/data/com.termux/files/usr/bin/rish"
TERMUX_AVISO = "/data/data/com.termux/files/usr/bin/termux-notification"
# Una etiqueta fija: el aviso nuevo sustituye al anterior en vez de apilarse,
# y sirve para comprobar que Android lo enseña.
ETIQUETA_TERMUX = "celestia-aviso"
# El icono tiene que estar donde el shell de Android lo pueda leer: su carpeta
# temporal. Se copia una vez desde el disco compartido (/sdcard).
ICONO_MOVIL = "/data/local/tmp/celestia_orbe.png"
_icono_listo = False
# Un aviso detrás de otro: una ráfaga de recados no puede acumular hilos
# esperando al móvil o a Google (Codex, 23 sep 2026).
_envio = threading.Lock()


def _preparar_icono() -> str:
    """Devuelve la opción del icono grande, o "" si no se pudo poner."""
    global _icono_listo
    if _icono_listo:
        return f"-I file://{ICONO_MOVIL} "
    try:
        from PIL import Image
        origen = LOG_DIR / "celestia_orbe_aviso.png"
        logo = Image.open(ROOT / "imagenes" / "celestia_logo_transparente.png").convert("RGBA")
        fondo = Image.new("RGBA", (192, 192), "#0B0D17")
        fondo.alpha_composite(logo.resize((192, 192), Image.LANCZOS))
        fondo.save(origen)
        # /root/Celestia es /sdcard/Celestia visto desde dentro de Ubuntu.
        en_sdcard = "/sdcard/Celestia/" + str(origen.relative_to(ROOT))
        r = subprocess.run([RISH, "-c", f"cp {shlex.quote(en_sdcard)} {ICONO_MOVIL} && "
                                        f"chmod 644 {ICONO_MOVIL}"],
                           capture_output=True, timeout=8)
        _icono_listo = r.returncode == 0
    except Exception as e:
        logger.debug("Push: sin icono para el aviso nativo (%s)", e)
    return f"-I file://{ICONO_MOVIL} " if _icono_listo else ""


def _nativo(titulo: str, cuerpo: str) -> bool:
    """Publica el aviso en Android. True si Android lo aceptó.

    El freno va AQUÍ, en el único sitio que ejecuta algo en el móvil, y no
    sólo en `avisar()`: quien llame a esto directamente tampoco puede hacer
    vibrar el móvil desde un test o desde la copia del examen."""
    if _apagado() or SIN_MOVIL or not ES_ANDROID or not os.path.exists(RISH):
        return False
    orden = (f"cmd notification post -S bigtext -t {shlex.quote(titulo)} "
             f"{_preparar_icono()}celestia {shlex.quote(cuerpo)}")
    try:
        r = subprocess.run([RISH, "-c", orden], capture_output=True, timeout=12)
    except Exception as e:
        logger.warning("Push: el aviso nativo no salió (%s)", e)
        return False
    salida = (r.stdout + r.stderr).decode("utf-8", "replace")
    if r.returncode != 0 or "posting" not in salida:
        logger.warning("Push: el aviso nativo no salió (%s)", salida.strip()[:160])
        return False
    return True


# Termux da a TODOS sus avisos la misma acción interna (mismo PendingIntent) y la
# deja congelada con lo que llevaba el primero hasta que se reinicia: medido el
# 23 sep 2026, avisos con acciones distintas abrían todos lo del primero; y
# salir de Termux no lo suelta (24 sep). Por eso la acción es siempre ésta,
# idéntica, y lo que cambia es el script.
SCRIPT_ABRIR = "/data/data/com.termux/files/home/.celestia_abrir.sh"
ACCION_ABRIR = f"bash {SCRIPT_ABRIR}"
_escribiendo_script = threading.Lock()
# Marca de un script que abre la app por Shizuku: sin Shizuku no se sabe más,
# y uno así no se tira.
_POR_SHIZUKU = "rish -c"


def _paquete_webapk(consulta: str) -> Optional[str]:
    """De `cmd package query-activities` para el chat, la app que Chrome
    instaló para Celestia (WebAPK), o None si no hay."""
    m = re.search(r"^\s*(org\.chromium\.webapk\.[\w.]+)/", consulta, re.M)
    return m.group(1) if m else None


def preparar_abrir_app(con_shizuku: bool = True) -> None:
    """Escribe el script que abre Celestia al tocar un aviso.

    Android 16 no deja que el aviso abra una app «de rebote»: Termux recibe el
    toque y su `termux-open-url` sale bloqueado (medido el 24 sep 2026:
    «Background activity launch blocked», con Termux en segundo plano). El
    shell de Shizuku sí puede, así que la orden va por `rish`, a la app
    instalada si la hay y si no a Chrome; sin Shizuku queda el navegador."""
    if _apagado() or SIN_MOVIL or not ES_ANDROID:
        return
    chat = f"http://127.0.0.1:{int(_puerto)}/chat"
    paquete = None
    if con_shizuku and os.path.exists(RISH):
        try:
            r = subprocess.run([RISH, "-c", "cmd package query-activities --brief --user 0 "
                                "-a android.intent.action.VIEW -c android.intent.category.BROWSABLE "
                                f"-d {shlex.quote(chat)}"], capture_output=True, timeout=30)
            if r.returncode == 0:
                paquete = _paquete_webapk(r.stdout.decode("utf-8", "replace")) or "com.android.chrome"
        except Exception as e:
            logger.debug("Push: no se pudo buscar la app instalada (%s)", e)
    if paquete is None and os.path.exists(SCRIPT_ABRIR):
        # Sin Shizuku no se sabe qué hay instalado: un script que ya abría
        # por Shizuku sigue valiendo.
        try:
            with open(SCRIPT_ABRIR, encoding="utf-8") as f:
                if not con_shizuku or _POR_SHIZUKU in f.read():
                    return
        except OSError:
            pass
    orden = ""
    if paquete:
        abrir = (f"am start --user 0 -a android.intent.action.VIEW -d {shlex.quote(chat)} "
                 f"-p {paquete} -f 0x10000000")
        orden = f"{RISH} -c {shlex.quote(abrir)} >/dev/null 2>&1 || "
    texto = ("#!/data/data/com.termux/files/usr/bin/bash\n"
             "# Lo escribe Celestia (push.py): tocar un aviso abre esto.\n"
             + orden + f"termux-open-url {shlex.quote(chat)}\n")
    # Se escribe aparte y se cambia de golpe: tocar un aviso a media escritura
    # no puede ejecutar un script vacío o cortado (Codex, 23 sep 2026).
    try:
        with _escribiendo_script:
            temporal = f"{SCRIPT_ABRIR}.{os.getpid()}.tmp"
            with open(temporal, "w", encoding="utf-8") as f:
                f.write(texto)
            os.replace(temporal, SCRIPT_ABRIR)
        logger.info("Push: tocar un aviso abrirá %s",
                    "la app instalada" if paquete and paquete != "com.android.chrome"
                    else "el chat en el navegador")
    except OSError as e:
        logger.warning("Push: no se pudo escribir %s (%s)", SCRIPT_ABRIR, e)


def _termux(titulo: str, cuerpo: str, url: str) -> bool:
    """Publica el aviso como Termux, con la acción de abrir el chat al tocarlo.

    `termux-notification` sale con 0 aunque Android lo tire (así pasó el 23 sep
    2026 con las notificaciones de Termux bloqueadas en Ajustes), así que su
    código no vale: si hay Shizuku se pregunta a Android si lo está enseñando.
    Sin Shizuku no se puede mirar y se da por bueno."""
    if _apagado() or SIN_MOVIL or not ES_ANDROID or not os.path.exists(TERMUX_AVISO):
        return False
    if not os.path.exists(SCRIPT_ABRIR):
        preparar_abrir_app(con_shizuku=False)
    entorno = dict(os.environ)
    entorno["PATH"] = entorno.get("PATH", "") + ":" + os.path.dirname(TERMUX_AVISO)
    # Fuera el aviso anterior con la misma etiqueta: si siguiera ahí, contarlo
    # daría por publicado uno nuevo que Android tiró (Codex, 23 sep 2026).
    try:
        subprocess.run([TERMUX_AVISO + "-remove", ETIQUETA_TERMUX],
                       capture_output=True, timeout=15, env=entorno)
    except Exception:
        pass
    try:
        r = subprocess.run([TERMUX_AVISO, "--id", ETIQUETA_TERMUX, "-t", titulo,
                            "-c", cuerpo, "--action", ACCION_ABRIR],
                           capture_output=True, timeout=20, env=entorno)
    except Exception as e:
        logger.warning("Push: el aviso de Termux no salió (%s)", e)
        return False
    if r.returncode != 0:
        logger.warning("Push: el aviso de Termux no salió (%s)",
                       (r.stdout + r.stderr).decode("utf-8", "replace").strip()[:160])
        return False
    if not os.path.exists(RISH):
        return True
    # Termux lo publica un momento después de volver: se mira hasta tres veces.
    # Si tarda aún más saldrá repetido por Shizuku, que es mejor que perderlo.
    for espera in (1.0, 2.5, 4.0):
        time.sleep(espera)
        try:
            mira = subprocess.run(
                [RISH, "-c", f"dumpsys notification | grep -c 'tag={ETIQUETA_TERMUX} '"],
                capture_output=True, timeout=12)
        except Exception:
            return True
        cuenta = mira.stdout.decode("utf-8", "replace").strip()
        if not cuenta.isdigit():
            return True  # Shizuku parado: no se sabe, y no se repite el aviso.
        if int(cuenta) > 0:
            return True
    logger.warning("Push: Android no enseña el aviso de Termux (¿notificaciones de "
                   "Termux bloqueadas en Ajustes?)")
    return False


# ── Avisos de la app instalada (3 oct 2026) ─────────────────────────────────
# Enzo: «que pueda mandar notificaciones, como que hay una actualización nueva
# o que ha terminado de hacer tal tarea». Lo de arriba (Termux, Shizuku, push
# web) es para la Celestia del móvil de Enzo; la que se instala no tiene nada
# de eso.

# Lo pone la app de Android al arrancar: (título, texto) → True si lo publicó.
NATIVO_APP: Optional[Callable[[str, str], bool]] = None

# Windows sólo enseña avisos de una «aplicación» registrada con este nombre.
# El mismo que pone lanzador.py a su ventana, para que vayan juntos.
AUMID = "Celestia.App"

_AVISO_WINDOWS = r"""
$null = [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
$null = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
$x = New-Object Windows.Data.Xml.Dom.XmlDocument
$x.LoadXml($env:CELESTIA_AVISO)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:CELESTIA_AUMID).Show([Windows.UI.Notifications.ToastNotification]::new($x))
"""


def _registrar_en_windows() -> None:
    """Da de alta «Celestia» como remitente de avisos, con su icono."""
    import winreg
    icono = ROOT / "imagenes" / "celestia_logo.png"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER,
                          rf"Software\Classes\AppUserModelId\{AUMID}") as k:
        winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, "Celestia")
        if icono.is_file():
            winreg.SetValueEx(k, "IconUri", 0, winreg.REG_SZ, str(icono))


def _escritorio(titulo: str, cuerpo: str) -> bool:
    """El aviso del propio sistema en un ordenador. Sin nada que instalar."""
    # Los tests llaman a `_entregar` directamente: en Windows sacaban avisos
    # de verdad en el escritorio de quien los pasaba (3 oct 2026).
    if ES_ANDROID or os.environ.get("CELESTIA_EN_TESTS"):
        return False
    try:
        if sys.platform == "win32":
            from xml.sax.saxutils import escape
            _registrar_en_windows()
            xml = ("<toast><visual><binding template='ToastGeneric'>"
                   f"<text>{escape(titulo)}</text><text>{escape(cuerpo)}</text>"
                   "</binding></visual></toast>")
            r = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _AVISO_WINDOWS],
                env=dict(os.environ, CELESTIA_AVISO=xml, CELESTIA_AUMID=AUMID),
                capture_output=True, timeout=30, creationflags=0x08000000)
        elif sys.platform == "darwin":
            # Por argumentos y no metido en el guion: unas comillas en el texto
            # no pueden romperlo.
            r = subprocess.run(
                ["osascript", "-e", "on run argv", "-e",
                 "display notification (item 2 of argv) with title (item 1 of argv)",
                 "-e", "end run", titulo, cuerpo], capture_output=True, timeout=30)
        else:
            if not shutil.which("notify-send"):
                return False
            icono = ROOT / "imagenes" / "celestia_logo.png"
            r = subprocess.run(["notify-send", "-a", "Celestia", "-i", str(icono), titulo, cuerpo],
                               capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception as e:
        logger.info("Push: el aviso de escritorio no salió (%s)", type(e).__name__)
        return False


def _entregar(carga: Dict[str, Any]) -> None:
    with _envio:
        if NATIVO_APP is not None:
            try:
                if NATIVO_APP(carga["titulo"], carga["cuerpo"]):
                    logger.info("Push: aviso publicado por la app de Android")
                    return
            except Exception as e:
                logger.info("Push: la app de Android no pudo avisar (%s)", type(e).__name__)
        if _escritorio(carga["titulo"], carga["cuerpo"]):
            logger.info("Push: aviso de escritorio publicado")
            return
        if _termux(carga["titulo"], carga["cuerpo"], carga.get("url") or "/chat"):
            logger.info("Push: aviso de Termux publicado en el móvil")
            return
        if _nativo(carga["titulo"], carga["cuerpo"]):
            logger.info("Push: aviso nativo publicado en el móvil")
            return
        if hay_suscripciones():
            _enviar_a_todas(carga)


def avisar(texto: str, titulo: str = "Celestia", url: str = "/chat") -> None:
    """Manda el aviso en segundo plano: quien encola un recado no espera a
    Google. Sin suscripciones no hace nada (ni importa pywebpush)."""
    if _apagado():
        return
    cuerpo = " ".join((texto or "Te ha dejado algo").split())
    if len(cuerpo) > MAX_CUERPO:
        cuerpo = cuerpo[:MAX_CUERPO - 1].rstrip() + "…"
    carga = {"titulo": titulo, "cuerpo": cuerpo, "url": url}
    threading.Thread(target=_entregar, args=(carga,), daemon=True,
                     name="push-aviso").start()


def publica() -> Optional[str]:
    try:
        return claves()["publica"]
    except Exception as e:
        logger.warning("Push: %s", e)
        return None
