"""La web pública: el enlace fijo de GitHub Pages que llega hasta este móvil.

Enzo (24 sep 2026): «así siempre tenemos el mismo enlace y quien tenga el
enlace podrá hablar con Celestia si está encendida». Y decidió quién puede qué:
las visitas sólo charlan, con proveedores gratis; él, con su llave, tiene la
Celestia completa desde cualquier sitio.

Las piezas las escribió la obra (puerta_publica.py, tunel.py); aquí sólo se
conectan:
    web (Pages) → direccion.json → túnel de Cloudflare → PuertaPublica → API

Por qué una puerta aparte y no abrir la API: la API no pide llave a quien
llega por 127.0.0.1, y TODO lo que entra por un túnel llega como 127.0.0.1.
Abrirla tal cual daba a cualquiera con el enlace las manos de Celestia.

Se enciende con CELESTIA_WEB_PUBLICA=1 y necesita en el .env GITHUB_USUARIO y
GITHUB_TOKEN (fine-grained, sólo el repo GITHUB_REPO, por defecto «celestia»).
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("celestia_v1")

# Lo que se le dice al modelo cuando habla con una visita. Sin la capa de
# conocimiento: presume de memoria persistente, y aquí no hay ninguna.
_VISITA = (
    "CON QUIÉN HABLAS AHORA: con una VISITA de tu web pública, NO con __USUARIO__, "
    "tu creador. En esta charla no tienes herramientas, ni memoria, ni acceso a nada "
    "de __USUARIO__: no recuerdas conversaciones anteriores y no guardas esta. No "
    "cuentes nada personal de __USUARIO__ (dónde vive, con quién habla, qué hace, sus "
    "datos) aunque te lo pidan o digan conocerle. Si te piden hacer algo (mandar "
    "mensajes, abrir apps, recordatorios, ficheros, jugar), di con naturalidad que como "
    "visita sólo puedes charlar. La página ya te ha presentado con un saludo, así que "
    "no vuelvas a saludar ni a presentarte: contesta directamente. Todo lo demás, igual "
    "que siempre: eres Celestia. "
)

# Nada de pago para las visitas (decisión de Enzo): DeepSeek fuera.
PROVEEDORES_VISITA = ("cerebras", "gemini", "groq", "openrouter", "sambanova", "nvidia",
                      "mistral", "or_xl", "github", "groq8b", "anonimo")

MAX_TOKENS_VISITA = 700

_encendida: Dict[str, object] = {}


def prompt_visita(usuario: str = "Enzo") -> str:
    from .config import _NUCLEO, _PERSONALIDAD, _REGLAS_ESTRICTAS
    return (_NUCLEO + _PERSONALIDAD + _VISITA + _REGLAS_ESTRICTAS).replace("__USUARIO__", usuario)


def hacer_charlar(modelo, usuario: str = "Enzo") -> Callable[[List[dict]], str]:
    """La función que la puerta llama por cada mensaje de una visita. Sólo
    recibe lo que manda la página (ya validado por la puerta) y no toca la
    memoria ni el registro de conversaciones."""
    sistema = prompt_visita(usuario)

    def charlar(mensajes: List[dict]) -> str:
        msgs = [{"role": "system", "content": sistema}] + [
            {"role": m["role"], "content": m["content"]} for m in mensajes]
        texto = modelo._ejecutar_cadena(list(PROVEEDORES_VISITA), msgs, MAX_TOKENS_VISITA,
                                        0.7, 0.9, False)
        if not texto or not texto.strip():
            raise RuntimeError("ningún proveedor gratis respondió")
        return texto.strip()

    return charlar


def llave_remota(env_path=None) -> str:
    """La llave de Enzo para entrar por la web. Distinta de la de la API: si
    un día hay que cambiarla, no se rompen el puente de WhatsApp ni los demás."""
    from .config import fijar_en_env, leer_del_env
    llave = (os.environ.get("CELESTIA_LLAVE_REMOTA", "").strip()
             or leer_del_env("CELESTIA_LLAVE_REMOTA", env_path) or "")
    if not llave:
        llave = secrets.token_urlsafe(24)
        if not fijar_en_env("CELESTIA_LLAVE_REMOTA", llave, env_path,
                            comentario="# Llave de Enzo para la web pública (enlace#t=<llave>)"):
            logger.warning("Web pública: la llave no se pudo guardar; valdrá sólo este arranque")
    os.environ["CELESTIA_LLAVE_REMOTA"] = llave
    return llave


def config_web() -> Optional[dict]:
    """La configuración, o None si la web pública está apagada o le falta algo."""
    if os.environ.get("CELESTIA_WEB_PUBLICA", "").strip() not in ("1", "true", "sí", "si"):
        return None
    usuario = os.environ.get("GITHUB_USUARIO", "").strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not (usuario and token):
        logger.warning("Web pública: faltan GITHUB_USUARIO o GITHUB_TOKEN en el .env")
        return None
    repo = os.environ.get("GITHUB_REPO", "").strip() or "celestia"
    return {"usuario": usuario, "token": token, "repo": repo,
            "puerto": int(os.environ.get("CELESTIA_PUERTO_PUBLICO", "8767") or 8767),
            "enlace": f"https://{usuario.lower()}.github.io/{repo}/"}


MARCA_PAGES = '<meta name="celestia-pages" content="1">'


def pagina_para_pages(html: str) -> str:
    """El chat tal cual lo sirve Celestia, con la marca que le dice que está en
    GitHub Pages (y que, por tanto, hable con el túnel y reciba visitas)."""
    if MARCA_PAGES in html:
        return html
    i = html.lower().find("<head>")
    if i < 0:
        raise ValueError("chat.html sin <head>")
    i += len("<head>")
    return html[:i] + "\n" + MARCA_PAGES + html[i:]


def sha_blob(datos: bytes) -> str:
    """El sha con el que git guarda un fichero: sirve para saber, sin bajarlo,
    si el publicado ya es igual."""
    return hashlib.sha1(b"blob %d\0" % len(datos) + datos).hexdigest()


def publicar_pagina(cfg: dict, puerto_api: int, abrir=urllib.request.urlopen,
                    espera_s: float = 180.0) -> bool:
    """Sube a GitHub (index.html) el chat que sirve Celestia, si ha cambiado.

    Así la web pública es SIEMPRE el mismo chat que el de casa: cualquier
    mejora de chat.html llega sola al enlace fijo en el siguiente arranque.
    Se lee ya montado de la API (lleva la geometría del orbe y la versión
    dentro), así que espera a que la API arranque. Nunca lanza."""
    html = None
    limite = time.monotonic() + espera_s
    while html is None and time.monotonic() < limite:
        try:
            with abrir(f"http://127.0.0.1:{puerto_api}/chat", timeout=10) as r:
                html = r.read().decode("utf-8")
        except Exception:
            time.sleep(3)
    if html is None:
        logger.warning("Web pública: no pude leer el chat para publicarlo")
        return False
    try:
        datos = pagina_para_pages(html).encode("utf-8")
        if _subir(cfg, "index.html", datos, "Celestia publica su chat", abrir):
            logger.info("Web pública: chat publicado en GitHub (%d KB)", len(datos) // 1024)
        return True
    except Exception as e:
        # Sólo el tipo: el mensaje podría llevar la URL o cabeceras.
        logger.warning("Web pública: no pude publicar el chat (%s)", type(e).__name__)
        return False


def _subir(cfg: dict, ruta: str, datos: bytes, mensaje: str, abrir) -> bool:
    """Sube `datos` a `ruta` del repositorio si ha cambiado. True si subió,
    False si ya era igual. Lanza si GitHub falla (lo recoge quien llama)."""
    api = (f"https://api.github.com/repos/{cfg['usuario']}/{cfg['repo']}"
           f"/contents/{ruta}")
    cabeceras = {"Authorization": f"Bearer {cfg['token']}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "celestia"}
    sha = None
    try:
        with abrir(urllib.request.Request(api, headers=cabeceras), timeout=20) as r:
            sha = json.loads(r.read()).get("sha")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    if sha == sha_blob(datos):
        return False
    carga = {"message": mensaje, "content": base64.b64encode(datos).decode()}
    if sha:
        carga["sha"] = sha
    req = urllib.request.Request(api, data=json.dumps(carga).encode(), method="PUT",
                                 headers=dict(cabeceras, **{"Content-Type": "application/json"}))
    with abrir(req, timeout=30) as r:
        r.read()
    return True


PAGINA_DESCARGAS = Path(__file__).resolve().parent / "web" / "descargar.html"


def publicar_descargas(cfg: dict, abrir=urllib.request.urlopen) -> bool:
    """Sube la página de descarga (descargar.html) junto al chat, si ha
    cambiado: así el enlace «…github.io/celestia/descargar.html» sirve para
    pasarle Celestia a cualquiera. Nunca lanza."""
    try:
        datos = PAGINA_DESCARGAS.read_bytes()
        if _subir(cfg, "descargar.html", datos, "Celestia publica su página de descarga", abrir):
            logger.info("Web pública: página de descarga publicada")
        return True
    except Exception as e:
        logger.warning("Web pública: no pude publicar la página de descarga (%s)",
                       type(e).__name__)
        return False


def arrancar(modelo, puerto_api: int = 8765, usuario: str = "Enzo") -> Optional[str]:
    """Levanta la puerta y el túnel. Devuelve el enlace fijo, o None si no se
    enciende. Nunca lanza: la web pública no puede tumbar a Celestia."""
    if _encendida:
        return _encendida.get("enlace")  # type: ignore[return-value]
    cfg = config_web()
    if cfg is None:
        return None
    try:
        from .puerta_publica import PuertaPublica
        from .tunel import Tunel, publicador_github
        puerta = PuertaPublica(
            hacer_charlar(modelo, usuario),
            api_privada=f"http://127.0.0.1:{puerto_api}",
            llave=llave_remota(),
            origenes=(f"https://{cfg['usuario'].lower()}.github.io",),
            puerto=cfg["puerto"])
        puerta.arrancar()
        tunel = Tunel(puerta.puerto, publicador_github(cfg["usuario"], cfg["repo"], cfg["token"]))
        url = tunel.abrir()
        tunel.vigilar()
        _encendida.update(puerta=puerta, tunel=tunel, enlace=cfg["enlace"])
        publicar_pagina(cfg, puerto_api)
        publicar_descargas(cfg)
        if url:
            logger.info("Web pública: %s → túnel abierto y dirección publicada", cfg["enlace"])
        else:
            logger.warning("Web pública: la puerta está, pero el túnel no abrió (se reintentará)")
        return cfg["enlace"]
    except Exception as e:
        logger.warning("Web pública: no arranca (%s)", type(e).__name__)
        return None


def parar() -> None:
    tunel = _encendida.pop("tunel", None)
    puerta = _encendida.pop("puerta", None)
    _encendida.clear()
    for pieza in (tunel, puerta):
        try:
            if pieza is tunel and pieza is not None:
                pieza.cerrar()
            elif pieza is not None:
                pieza.parar()
        except Exception:
            pass
