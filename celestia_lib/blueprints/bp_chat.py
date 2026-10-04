#!/usr/bin/env python3
"""Blueprint del chat web y del estado en vivo: /chat y /actividad.

`/chat` sirve una única página sin dependencias externas —ni un CDN, ni una
fuente remota, ni un framework— porque Celestia corre en un móvil y muchas veces
sin datos: una interfaz que necesita bajarse medio internet para pintar un
mensaje no sirve de nada en el sitio donde vive.

La página es sólo la carcasa (no lleva ni un dato dentro), así que se puede
abrir escribiendo la URL aunque haya token configurado; es el propio navegador
el que luego lo pide y lo guarda. `/actividad` sí va protegido: dice en qué anda
Celestia e incluye el principio del mensaje que está atendiendo.

La paleta y la geometría del logo NO se escriben en el HTML: se inyectan desde
`celestia_lib/marca.py`, que es lo mismo que dibuja el orbe en el terminal. Así
el logo del móvil y el del navegador son el mismo objeto y no se pueden ir
separando con el tiempo.
"""
import base64
import io
import json
import logging
import re
import time
from pathlib import Path

from flask import Blueprint, Response, jsonify, request as flask_request

from .. import actividad, buzon, marca, push
from ..paths import RECIBIDOS_DIR, ROOT
from ..tools import AgentTools

logger = logging.getLogger("celestia_v1")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
PAGINA = WEB_DIR / "chat.html"
PAGINA_DESCARGAS = WEB_DIR / "descargar.html"

# Lo que llega desde el chat aterriza aquí. Está dentro de las rutas que
# `AgentTools._ruta_segura` permite, así que Celestia puede leer lo que le
# pases sin que eso abra la puerta al resto del disco.
BUZON = RECIBIDOS_DIR

# Tope de subida. El límite global de Flask son 32 MB; aquí se corta antes para
# dar un error claro en vez de que Werkzeug devuelva un 413 pelado.
#
# Se escribe en megas de archivo —que es lo que ve quien lo elige— y el tope en
# base64 se deduce (4 bytes por cada 3). Antes eran dos números sueltos: el
# límite decía 25 MB y el error que se leía decía 18.
MAX_SUBIDA_MB = 18
MAX_SUBIDA_B64 = MAX_SUBIDA_MB * 1024 * 1024 * 4 // 3 + 8

# Cuánto texto del archivo se le pasa a Celestia. El plan gratuito de Groq da
# 8.000 tokens por minuto: un documento entero se come el turno y la respuesta
# sale cortada. Con esto entra la parte que casi siempre importa y se avisa de
# que hay más.
MAX_TEXTO_ARCHIVO = 4000

# Extensiones que se leen como texto llano. Lo que no esté aquí se intenta por
# su formato (PDF, DOCX) y, si tampoco, se queda solo guardado.
EXT_TEXTO = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".xml", ".html", ".htm", ".log", ".ini", ".cfg", ".conf", ".toml",
    ".py", ".js", ".ts", ".java", ".c", ".h", ".cpp", ".cs", ".go", ".rs",
    ".rb", ".php", ".sh", ".bash", ".sql", ".r", ".m", ".swift", ".kt",
}

SERVICE_WORKER = WEB_DIR / "sw.js"
LOGO = ROOT / "imagenes" / "celestia_logo_transparente.png"

# Lo que la app pide sin llave: el manifiesto, los iconos y el service worker
# son la carcasa de la app, igual que /chat. No llevan ni un dato.
RUTAS_APP = {"/manifest.webmanifest", "/sw.js", "/app/icono-192.png",
             "/app/icono-512.png", "/app/insignia.png", "/push/clave",
             "/descargar"}

_iconos: dict = {}

# Apuntarse a los avisos sólo desde el propio móvil, con llave o sin ella: la
# WiFi de casa va sin llave (decisión de Enzo) y una suscripción ajena se
# llevaría copia de cada recado. Desde otro aparato el navegador tampoco deja
# (el push exige https, y ahí es http), así que no se pierde nada.
_LOOPBACK = {"127.0.0.1", "::1"}


def _desde_el_movil() -> bool:
    return (flask_request.remote_addr or "") in _LOOPBACK


def _icono(lado: int, insignia: bool = False) -> bytes:
    """El orbe como icono de app, hecho una vez y guardado en memoria.

    Icono: el orbe sobre el fondo «noche», con margen para que Android pueda
    recortarlo en círculo o en gota («maskable») sin comerse el anillo.
    Insignia: la silueta en blanco sobre transparente; Android pinta con ella
    el iconito de la barra de estado y sólo mira la transparencia.
    """
    clave = (lado, insignia)
    if clave not in _iconos:
        from PIL import Image
        logo = Image.open(LOGO).convert("RGBA")
        if insignia:
            alfa = logo.getchannel("A").resize((lado, lado), Image.LANCZOS)
            img = Image.new("RGBA", (lado, lado), (255, 255, 255, 0))
            img.putalpha(alfa)
        else:
            img = Image.new("RGBA", (lado, lado), marca.PALETA["noche"])
            interior = int(lado * 0.72)       # zona segura de un maskable: 80 %
            orbe = logo.resize((interior, interior), Image.LANCZOS)
            hueco = (lado - interior) // 2
            img.alpha_composite(orbe, (hueco, hueco))
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=True)
        _iconos[clave] = buf.getvalue()
    return _iconos[clave]


def manifiesto() -> dict:
    return {
        "name": "Celestia",
        "short_name": "Celestia",
        "description": "Tu asistente personal",
        "lang": "es",
        "start_url": "/chat",
        "scope": "/",
        "id": "/chat",
        "display": "standalone",
        "orientation": "portrait",
        "background_color": marca.PALETA["noche"],
        "theme_color": marca.PALETA["noche"],
        "icons": [
            {"src": "/app/icono-192.png", "sizes": "192x192", "type": "image/png",
             "purpose": "any maskable"},
            {"src": "/app/icono-512.png", "sizes": "512x512", "type": "image/png",
             "purpose": "any maskable"},
        ],
    }


# La página se relee si cambió en disco: durante el desarrollo del tema, tener
# que reiniciar Celestia entera (~60 s) para ver un color distinto no es viable.
_cache: dict = {"mtime": 0.0, "html": ""}


_huella: dict = {"mtime": None, "valor": "0"}


def version_pagina() -> str:
    """Un identificador de la versión servida de `chat.html`.

    En un móvil la pestaña del chat se queda abierta días: se cambia de app y
    se vuelve, pero no se recarga. Con esto, la página que está abierta puede
    comparar lo que trae dentro con lo que sirve el daemon ahora y avisar de
    que hay una versión nueva.

    Es una huella del CONTENIDO, no la fecha del fichero (4 oct 2026): en la
    app de Android la fecha cambia sin que cambie nada (los ficheros se sacan
    del paquete al usarse) y el aviso «Hay una versión nueva del chat» salía
    siempre. Se recalcula sólo si cambia la fecha: no cuesta leer el fichero
    en cada `/estado`.
    """
    try:
        mtime = PAGINA.stat().st_mtime
        if _huella["mtime"] != mtime:
            import hashlib
            _huella.update(mtime=mtime,
                           valor=hashlib.sha1(PAGINA.read_bytes()).hexdigest()[:12])
        return _huella["valor"]
    except OSError:
        return "0"


def _render() -> str:
    """El HTML con la paleta y la geometría del logo ya dentro."""
    try:
        mtime = PAGINA.stat().st_mtime
    except OSError:
        return "<h1>Falta celestia_lib/web/chat.html</h1>"
    if _cache["mtime"] != mtime:
        html = PAGINA.read_text(encoding="utf-8")
        html = html.replace("/*__VARIABLES__*/", marca.css_variables())
        html = html.replace("/*__GEOMETRIA__*/",
                            json.dumps(marca.geometria_web(), ensure_ascii=False))
        html = html.replace("/*__ESTADOS__*/",
                            json.dumps(marca.ESTADOS, ensure_ascii=False))
        html = html.replace("/*__PALETA__*/",
                            json.dumps(marca.PALETA, ensure_ascii=False))
        html = html.replace("/*__VERSION__*/",
                            json.dumps(version_pagina(), ensure_ascii=False))
        _cache.update(mtime=mtime, html=html)
    return _cache["html"]


def _nombre_limpio(nombre: str) -> str:
    """Un nombre de archivo que no pueda salirse de su carpeta.

    Lo manda el navegador, así que se le quita todo lo que no sea texto llano:
    barras, dos puntos y los «..» que permitirían escribir en otro sitio.
    """
    base = Path(str(nombre or "archivo")).name          # se queda la última parte
    base = re.sub(r"[^\w.\- ]", "_", base, flags=re.UNICODE).strip(". ")
    return (base or "archivo")[:80]


def _extraer_texto(ruta: Path) -> str:
    """El contenido legible del archivo, o "" si no lo hay.

    Se lee aquí, en el servidor, y no se le pide al modelo que use una
    herramienta: probado con un acta de reunión, el LLM contestó con un acta
    **inventada** en vez de abrir el fichero. Un dato que se puede leer no
    puede depender de que el modelo de turno decida leerlo.
    """
    ext = ruta.suffix.lower()
    try:
        if ext in EXT_TEXTO:
            return ruta.read_text(encoding="utf-8", errors="replace")
        if ext == ".pdf":
            from pypdf import PdfReader
            lector = PdfReader(str(ruta))
            partes = []
            for pagina in lector.pages[:40]:          # tope: un PDF puede ser enorme
                partes.append(pagina.extract_text() or "")
                if sum(len(t) for t in partes) > MAX_TEXTO_ARCHIVO * 2:
                    break
            return "\n".join(partes)
        if ext == ".docx":
            import docx
            return "\n".join(p.text for p in docx.Document(str(ruta)).paragraphs)
    except Exception as e:                            # un PDF corrupto, un docx raro
        logger.info("No pude extraer texto de %s: %s", ruta.name, e)
    return ""


# La marca que pone el puente de WhatsApp al guardar un documento
# (`whatsapp_bridge/bridge.js`). Hasta el 3 oct 2026 el modelo solo veía esa
# línea, nunca el contenido: con «celestia.py» contestó «[ABRIENDO ARCHIVO…]» y
# describió microservicios y redes neuronales que no existían (chat del 3 jun).
_MARCA_RECIBIDO_RE = re.compile(r"\[ARCHIVO RECIBIDO:\s*([^\]\n]+?)\s+—\s+\d+\s+bytes\]")


def anexar_archivo_recibido(texto: str) -> str:
    """Cambia la marca `[ARCHIVO RECIBIDO: …]` por lo que pone el archivo.

    Lo mismo que hace el chat web con `/subir`, para los documentos que llegan
    por WhatsApp. De la ruta de la marca solo vale el NOMBRE, buscado dentro de
    `recibidos/`: la marca va en el texto del mensaje, y cualquiera puede
    escribirla a mano apuntando al `.env`. Si no se puede leer, se dice tal
    cual en vez de dejar que el modelo lo imagine.
    """
    m = _MARCA_RECIBIDO_RE.search(texto or "")
    if not m:
        return texto
    nombre = _nombre_limpio(m.group(1).replace("\\", "/").rsplit("/", 1)[-1])
    ruta = BUZON / nombre
    seguro = AgentTools._ruta_segura(str(ruta))
    leido = ""
    if (seguro is not None and BUZON.resolve() in seguro.parents
            and seguro.is_file()):
        leido = (_extraer_texto(seguro) or "").strip()
    if leido:
        truncado = len(leido) > MAX_TEXTO_ARCHIVO
        bloque = (f"[CONTENIDO DEL ARCHIVO «{nombre}» QUE ACABAS DE ABRIR"
                  + (" — solo el principio, el archivo sigue" if truncado else "")
                  + ". Habla SOLO de lo que pone aquí; si algo no aparece, "
                  "dilo en vez de suponerlo]\n" + leido[:MAX_TEXTO_ARCHIVO])
    else:
        bloque = (f"[Te han pasado el archivo «{nombre}», pero no es texto que "
                  "puedas leer. NO describas su contenido: di que lo tienes "
                  "guardado y pregunta qué hacer con él]")
    return texto[:m.start()] + bloque + texto[m.end():]


def crear(api) -> Blueprint:
    bp = Blueprint("chat", __name__)

    @bp.route("/chat", methods=["GET"])
    def chat():
        """La interfaz de conversación. Abre esto en el navegador."""
        resp = Response(_render(), mimetype="text/html")
        # Sin caché: el chat se sirve desde la propia Celestia y una versión
        # vieja pegada en el navegador confunde más de lo que ahorra.
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @bp.route("/descargar", methods=["GET"])
    def descargar():
        """La página para llevarse Celestia a otro aparato. La misma que se
        publica en la web pública; aquí, para verla y pasarla sin internet."""
        try:
            html = PAGINA_DESCARGAS.read_text(encoding="utf-8")
        except OSError:
            return jsonify({"error": "falta descargar.html"}), 404
        resp = Response(html, mimetype="text/html")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    # ── La app instalable (PWA) ────────────────────────────────────────
    @bp.route("/manifest.webmanifest", methods=["GET"])
    def app_manifiesto():
        resp = Response(json.dumps(manifiesto(), ensure_ascii=False),
                        mimetype="application/manifest+json")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @bp.route("/sw.js", methods=["GET"])
    def app_service_worker():
        try:
            codigo = SERVICE_WORKER.read_text(encoding="utf-8")
        except OSError:
            return Response("// falta sw.js", mimetype="text/javascript"), 404
        resp = Response(codigo, mimetype="text/javascript")
        # Chrome ya revisa el service worker cada 24 h como mucho; sin caché,
        # un cambio llega en la siguiente apertura de la app.
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @bp.route("/app/icono-<int:lado>.png", methods=["GET"])
    def app_icono(lado):
        if lado not in (192, 512):
            return jsonify({"error": "tamaño no disponible"}), 404
        resp = Response(_icono(lado), mimetype="image/png")
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp

    @bp.route("/app/insignia.png", methods=["GET"])
    def app_insignia():
        resp = Response(_icono(96, insignia=True), mimetype="image/png")
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp

    @bp.route("/push/clave", methods=["GET"])
    def push_clave():
        """La clave pública para suscribirse. Pública de verdad: es la que el
        servicio de push usa para comprobar que el aviso viene de Celestia."""
        clave = push.publica()
        if not clave:
            return jsonify({"error": "sin claves de push"}), 503
        return jsonify({"clave": clave})

    @bp.route("/push/suscribir", methods=["POST"])
    def push_suscribir():
        if not _desde_el_movil():
            return jsonify({"error": "los avisos sólo se activan desde el propio móvil"}), 403
        data = flask_request.get_json(silent=True)
        if not push.suscribir(data):
            return jsonify({"error": "suscripción no válida"}), 400
        return jsonify({"ok": True})

    @bp.route("/push/baja", methods=["POST"])
    def push_baja():
        if not _desde_el_movil():
            return jsonify({"error": "los avisos sólo se activan desde el propio móvil"}), 403
        data = flask_request.get_json(silent=True) or {}
        push.dar_de_baja(str(data.get("endpoint") or ""))
        return jsonify({"ok": True})

    @bp.route("/push/recibido", methods=["POST"])
    def push_recibido():
        """El service worker cuenta qué pasó con un aviso: si llegó y si Android
        le dejó enseñarlo. Es la única ventana a lo que ocurre dentro del móvil."""
        if not _desde_el_movil():
            return jsonify({"error": "sólo desde el propio móvil"}), 403
        data = flask_request.get_json(silent=True) or {}
        logger.info("Push: el móvil dice %s (permiso %s)",
                    str(data.get("resultado", "?"))[:120], str(data.get("permiso", "?"))[:20])
        return jsonify({"ok": True})

    @bp.route("/push/probar", methods=["POST"])
    def push_probar():
        if not _desde_el_movil():
            return jsonify({"error": "los avisos sólo se activan desde el propio móvil"}), 403
        """Un aviso de prueba desde Ajustes, para ver que llega con la app cerrada."""
        push.avisar("Así te avisaré cuando tenga algo para ti ✨")
        return jsonify({"ok": True})

    @bp.route("/subir", methods=["POST"])
    def subir():
        """Guarda un archivo que le pasas por el chat y devuelve dónde quedó.

        El navegador no puede darle a Celestia una ruta —no tiene disco que
        compartir—, así que manda el contenido y aquí se deja en `recibidos/`.
        A partir de ahí es un archivo más del dispositivo: Celestia puede
        leerlo, resumirlo o trabajar con él con las herramientas de siempre.

        Las imágenes NO pasan por aquí: van en el propio mensaje para que las
        mire con visión.
        """
        data = flask_request.get_json(force=True, silent=True)
        if not isinstance(data, dict):
            return jsonify({"error": "body debe ser objeto JSON"}), 400
        datos = data.get("datos_b64") or ""
        if not datos:
            return jsonify({"error": "datos_b64 requerido"}), 400
        if len(datos) > MAX_SUBIDA_B64:
            return jsonify({"error": f"archivo demasiado grande "
                                     f"(máx. {MAX_SUBIDA_MB} MB)"}), 413
        try:
            crudo = base64.b64decode(datos, validate=True)
        except Exception:
            return jsonify({"error": "datos_b64 inválido"}), 400
        if not crudo:
            return jsonify({"error": "archivo vacío"}), 400

        nombre = _nombre_limpio(data.get("nombre"))
        destino = BUZON / f"{int(time.time())}_{nombre}"
        # Última red: aunque el nombre ya viene saneado, se comprueba que el
        # resultado siga dentro del buzón y de las rutas que Celestia tiene
        # permitidas. Si algún día cambia el saneado, esto sigue en pie.
        seguro = AgentTools._ruta_segura(str(destino))
        if seguro is None or BUZON.resolve() not in seguro.parents:
            return jsonify({"error": "nombre de archivo no permitido"}), 400
        try:
            BUZON.mkdir(parents=True, exist_ok=True)
            seguro.write_bytes(crudo)
        except OSError as e:
            logger.warning("No pude guardar %s: %s", destino, e)
            return jsonify({"error": f"no pude guardarlo: {e}"}), 500
        texto = (_extraer_texto(seguro) or "").strip()
        truncado = len(texto) > MAX_TEXTO_ARCHIVO
        if truncado:
            texto = texto[:MAX_TEXTO_ARCHIVO]
        logger.info("Chat ← archivo recibido: %s (%d bytes, %d chars legibles)",
                    seguro.name, len(crudo), len(texto))
        return jsonify({
            "ruta": str(seguro), "nombre": nombre, "bytes": len(crudo),
            # El contenido ya leído: el chat lo mete en el mensaje para que
            # Celestia hable de lo que pone de verdad.
            "texto": texto, "truncado": truncado,
        })

    # ── Los chats ────────────────────────────────────────────────────────
    # La lista vive en el servidor y no en el navegador a propósito: el chat se
    # abre desde el móvil y desde el portátil, y en los dos sitios tienen que
    # aparecer las mismas conversaciones.

    def _titulo_de(texto: str) -> str:
        """El nombre del chat: lo primero que se dijo, recortado con cabeza."""
        t = " ".join((texto or "").split())
        if len(t) <= 42:
            return t or "Chat sin título"
        corte = t[:42].rsplit(" ", 1)[0]      # no partir una palabra por la mitad
        return (corte or t[:42]) + "…"

    @bp.route("/hilos", methods=["GET"])
    def listar_hilos():
        """Los chats guardados, del más reciente al más antiguo."""
        cur = api.orch.memory.conn.cursor()
        cur.execute(
            "SELECT hilo, COUNT(*), MAX(ts), MIN(ts) FROM conversations "
            "WHERE hilo IS NOT NULL AND hilo != '' "
            "GROUP BY hilo ORDER BY MAX(ts) DESC LIMIT 200"
        )
        filas = cur.fetchall()
        hilos = []
        for hilo, n, ultimo, primero in filas:
            cur.execute(
                "SELECT user_input FROM conversations WHERE hilo=? "
                "ORDER BY ts ASC LIMIT 1", (hilo,))
            fila = cur.fetchone()
            if hilo == buzon.HILO:
                continue          # lo que Enzo escriba ahí se cuenta con el buzón
            hilos.append({
                "id": hilo,
                "titulo": _titulo_de(fila[0] if fila else ""),
                "mensajes": n,
                "ultimo_ts": ultimo,
                "creado_ts": primero,
            })
        # El buzón (lo que Celestia manda por su cuenta) es un chat más.
        caja = buzon.resumen()
        if caja:
            hilos.append(caja)
            hilos.sort(key=lambda h: h.get("ultimo_ts") or 0, reverse=True)
        return jsonify({"hilos": hilos})

    @bp.route("/hilos/<hilo_id>", methods=["GET"])
    def abrir_hilo(hilo_id):
        """Los mensajes de un chat, para pintarlo al volver a él."""
        hilo_id = (hilo_id or "").strip()[:64]
        cur = api.orch.memory.conn.cursor()
        cur.execute(
            "SELECT user_input, ai_response, ts FROM conversations "
            "WHERE hilo=? ORDER BY ts ASC LIMIT 400", (hilo_id,))
        mensajes = []
        for user, ia, ts in cur.fetchall():
            # Lo guardado lleva los anexos del turno ([RESULTADO] de una
            # búsqueda, la nota de voz…): al volver al chat se veían en la
            # burbuja de quien escribió. Se pinta sólo lo que escribió.
            user = type(api.orch)._solo_mensaje_usuario(user or "").strip()
            if user:
                mensajes.append({"quien": "yo", "texto": user, "ts": ts})
            if ia:
                mensajes.append({"quien": "ella", "texto": ia, "ts": ts})
        if hilo_id == buzon.HILO:
            mensajes = sorted(buzon.mensajes() + mensajes, key=lambda m: m.get("ts") or 0)
        return jsonify({"id": hilo_id, "mensajes": mensajes})

    @bp.route("/hilos/<hilo_id>", methods=["DELETE"])
    def borrar_hilo(hilo_id):
        """Borra un chat entero: de la memoria viva y de la base de datos.

        Lo que se borra se borra: es lo que se espera al mantener pulsado y
        decir que sí. Los hechos que Celestia aprendiera de esa conversación
        siguen donde estaban — son suyos, no del chat.
        """
        hilo_id = (hilo_id or "").strip()[:64]
        if not hilo_id:
            return jsonify({"error": "falta el chat"}), 400
        api.orch.olvidar_hilo(hilo_id)
        cur = api.orch.memory.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM conversations WHERE hilo=?", (hilo_id,))
        n = cur.fetchone()[0]
        cur.execute("DELETE FROM conversations WHERE hilo=?", (hilo_id,))
        if hilo_id == buzon.HILO:
            buzon.vaciar()
        api.orch.memory.conn.commit()
        logger.info("Chat borrado: %s (%d mensajes)", hilo_id, n)
        return jsonify({"borrado": hilo_id, "mensajes": n})

    @bp.route("/actividad", methods=["GET"])
    def actividad_actual():
        """En qué anda Celestia ahora mismo: fase, detalle y recorrido del turno.

        Lo consulta el chat mientras espera una respuesta para que el logo
        cambie de estado con lo que de verdad está pasando por dentro.
        """
        return jsonify(actividad.instantanea())

    return bp
