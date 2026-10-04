"""La puerta pública de Celestia.

Escucha sólo en 127.0.0.1 (delante va un túnel de Cloudflare). Por aquí
entran dos clases de persona:

* las visitas, que sólo pueden mandar mensajes al chat;
* el dueño, que con su llave (cabecera ``X-Celestia-Token``) llega a la
  API privada de Celestia.

La llave nunca se escribe en un log ni viaja en una respuesta, y tampoco
se reenvía hacia la API privada.
"""

from __future__ import annotations

import hmac
import json
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

#: Tamaño máximo del cuerpo de una petición de visita.
MAX_CUERPO = 64 * 1024
#: Tamaño máximo del cuerpo de una petición con llave (subidas, notas de voz).
MAX_CUERPO_PRIVADO = 32 * 1024 * 1024
#: Número máximo de mensajes por petición.
MAX_MENSAJES = 20
#: Longitud máxima de cada mensaje.
MAX_CARACTERES = 2000
#: Espera máxima al hablar con la API privada.
ESPERA_API = 200

#: Métodos que pueden llevar cuerpo.
METODOS_CON_CUERPO = frozenset({"POST", "PUT", "PATCH", "DELETE"})

#: Rutas de la carcasa del chat: no llevan datos y se sirven sin llave.
CARCASA_PUBLICA = frozenset({
    "/chat",
    "/chat/",
    "/manifest.webmanifest",
    "/sw.js",
    "/app/icono-192.png",
    "/app/icono-512.png",
    "/app/insignia.png",
})

#: Cabeceras que no se reenvían (hop-by-hop, identificativas o la llave).
SALTAR_CABECERAS = frozenset({
    "host",
    "x-celestia-token",
    "content-length",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
})


class PuertaPublica:
    """Servidor HTTP que hace de puerta entre internet y Celestia."""

    def __init__(
        self,
        charlar,
        api_privada="http://127.0.0.1:8765",
        llave="",
        origenes=(),
        puerto=8767,
        limite_por_ip=20,
        ventana_s=600.0,
        limite_diario=300,
        reloj=time.time,
        timeout_s=15.0,
        max_conexiones=32,
    ):
        self.charlar = charlar
        self.api_privada = api_privada.rstrip("/")
        self.llave = llave or ""
        self.origenes = tuple(origenes)
        self.puerto = puerto
        self.limite_por_ip = limite_por_ip
        self.ventana_s = ventana_s
        self.limite_diario = limite_diario
        self.reloj = reloj
        # Contra quien abre conexiones y no termina de mandar (Codex, 24 sep):
        # cada conexión tiene un plazo y hay un máximo atendiéndose a la vez.
        self.timeout_s = timeout_s
        self._huecos = threading.BoundedSemaphore(max(int(max_conexiones), 1))

        self._servidor = None
        self._hilo = None
        self._candado = threading.Lock()
        # ip -> instantes de las peticiones válidas dentro de la ventana
        self._por_ip = {}
        # día (entero desde la época, en UTC) -> peticiones válidas
        self._por_dia = {}

    # ------------------------------------------------------------------
    # Ciclo de vida
    # ------------------------------------------------------------------

    def arrancar(self) -> None:
        """Sirve en un hilo daemon. Con ``puerto=0`` guarda el puerto real."""
        clase = type("ManejadorPuerta", (_Manejador,),
                     {"puerta": self, "timeout": self.timeout_s})
        self._servidor = ThreadingHTTPServer(("127.0.0.1", self.puerto), clase)
        self.puerto = self._servidor.server_address[1]
        self._hilo = threading.Thread(
            target=self._servidor.serve_forever,
            name="puerta-publica",
            daemon=True,
        )
        self._hilo.start()

    def parar(self) -> None:
        servidor, self._servidor = self._servidor, None
        if servidor is not None:
            servidor.shutdown()
            servidor.server_close()
        hilo, self._hilo = self._hilo, None
        if hilo is not None:
            hilo.join(timeout=5)


class _Manejador(BaseHTTPRequestHandler):
    """Atiende una petición. La instancia de la puerta va en ``puerta``."""

    puerta = None
    protocol_version = "HTTP/1.0"

    # Nada por consola: ni la llave ni el rastro de cada visita.
    def log_message(self, formato, *args):  # noqa: D401 - firma heredada
        pass

    _OCUPADA = b'{"error": "ocupada"}'

    def handle(self):
        """Un hueco por conexión; sin hueco, 503 al momento y sin leer nada.

        El plazo de lectura lo pone `timeout` (StreamRequestHandler lo aplica
        al socket) y `handle_one_request` cierra la conexión al vencer, sin
        llegar a llamar a charlar ni a reenviar."""
        if not self.puerta._huecos.acquire(blocking=False):
            # Aún no se ha leído la petición: la respuesta va en crudo.
            try:
                self.wfile.write(
                    b"HTTP/1.0 503 Service Unavailable\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Content-Length: " + str(len(self._OCUPADA)).encode() + b"\r\n"
                    b"Connection: close\r\n\r\n" + self._OCUPADA)
            except OSError:
                pass
            return
        try:
            super().handle()
        except OSError:
            pass
        finally:
            self.puerta._huecos.release()

    # ------------------------------------------------------------------
    # Respuestas
    # ------------------------------------------------------------------

    def _cors(self):
        origen = self.headers.get("Origin")
        if origen and origen in self.puerta.origenes:
            return {
                "Access-Control-Allow-Origin": origen,
                "Vary": "Origin",
                "Access-Control-Allow-Headers": "Content-Type, X-Celestia-Token",
                "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
            }
        return {}

    def _enviar_crudo(self, codigo, cuerpo, content_type):
        if isinstance(cuerpo, str):
            cuerpo = cuerpo.encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(cuerpo)))
        for clave, valor in self._cors().items():
            self.send_header(clave, valor)
        self.end_headers()
        if cuerpo:
            try:
                self.wfile.write(cuerpo)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _enviar_json(self, codigo, datos):
        cuerpo = json.dumps(datos, ensure_ascii=False).encode("utf-8")
        self._enviar_crudo(codigo, cuerpo, "application/json; charset=utf-8")

    def _enviar_sin_cuerpo(self, codigo):
        self.send_response(codigo)
        for clave, valor in self._cors().items():
            self.send_header(clave, valor)
        self.end_headers()

    # ------------------------------------------------------------------
    # Entrada
    # ------------------------------------------------------------------

    def _leer_cuerpo(self, limite):
        """Lee el cuerpo hasta ``limite``. Devuelve (datos, ¿se pasa?)."""
        cabecera = self.headers.get("Content-Length")
        try:
            largo = int(cabecera) if cabecera else 0
        except (TypeError, ValueError):
            largo = 0
        if largo < 0:
            largo = 0
        if largo == 0:
            return b"", False
        leido = self.rfile.read(min(largo, limite + 1))
        return leido, largo > limite

    def do_GET(self):
        ruta = urlsplit(self.path).path
        if ruta == "/estado":
            # Con la llave buena, el dueño ve el estado de verdad (lo sirve
            # la API privada). Sin llave o con llave mala, la web sólo
            # necesita saber que Celestia está encendida: respuesta mínima,
            # sin 401 y sin tocar la API privada.
            if self._llave_valida():
                self._con_llave()
            else:
                self._enviar_json(200, {"ok": True, "nombre": "Celestia"})
        elif ruta in CARCASA_PUBLICA:
            self._reenviar_carcasa()
        else:
            self._con_llave()

    def do_POST(self):
        ruta = urlsplit(self.path).path
        if ruta == "/visita/mensaje":
            self._visita()
        else:
            self._con_llave()

    def do_PUT(self):
        self._con_llave()

    def do_PATCH(self):
        self._con_llave()

    def do_DELETE(self):
        self._con_llave()

    def do_OPTIONS(self):
        self._enviar_sin_cuerpo(204)

    # ------------------------------------------------------------------
    # Visitas
    # ------------------------------------------------------------------

    def _visita(self):
        datos, grande = self._leer_cuerpo(MAX_CUERPO)
        if grande:
            self._enviar_json(413, {"error": "cuerpo demasiado grande"})
            return

        try:
            cuerpo = json.loads(datos.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._enviar_json(400, {"error": "JSON inválido"})
            return

        if not isinstance(cuerpo, dict):
            self._enviar_json(400, {"error": "se esperaba un objeto JSON"})
            return

        mensajes = cuerpo.get("mensajes")
        motivo = self._validar(mensajes)
        if motivo:
            self._enviar_json(400, {"error": motivo})
            return

        if self._pasado_de_limite():
            self._enviar_json(429, {"error": "rate_limited"})
            return

        try:
            respuesta = self.puerta.charlar(mensajes)
        except Exception:  # noqa: BLE001 - cualquier fallo es el mismo 503
            self._enviar_json(503, {"error": "Celestia no puede contestar ahora"})
            return

        if not isinstance(respuesta, str) or not respuesta.strip():
            self._enviar_json(503, {"error": "Celestia no puede contestar ahora"})
            return

        self._enviar_json(200, {"respuesta": respuesta})

    @staticmethod
    def _validar(mensajes):
        """Devuelve el motivo del 400, o cadena vacía si todo está bien."""
        if not isinstance(mensajes, list) or not mensajes:
            return "no hay mensajes"
        if len(mensajes) > MAX_MENSAJES:
            return "demasiados mensajes"
        for mensaje in mensajes:
            if not isinstance(mensaje, dict):
                return "mensaje inválido"
            if mensaje.get("role") not in ("user", "assistant"):
                return "rol inválido"
            contenido = mensaje.get("content")
            if not isinstance(contenido, str):
                return "contenido inválido"
            if len(contenido) > MAX_CARACTERES:
                return "mensaje demasiado largo"
        if mensajes[-1].get("role") != "user":
            return "el último mensaje debe ser del usuario"
        return ""

    def _pasado_de_limite(self):
        """Comprueba y anota el gasto. Cuenta PETICIONES, no mensajes.

        La página manda el historial entero en cada POST, así que contar
        mensajes bloquearía a la visita al cuarto intercambio. Cada POST
        válido a /visita/mensaje cuenta 1.
        """
        puerta = self.puerta
        ip = self.headers.get("CF-Connecting-IP") or self.client_address[0]
        ahora = puerta.reloj()
        dia = int(ahora // 86400)
        cuantos = 1

        with puerta._candado:
            recientes = [
                t for t in puerta._por_ip.get(ip, ())
                if ahora - t < puerta.ventana_s
            ]
            cabe = (
                len(recientes) + cuantos <= puerta.limite_por_ip
                and puerta._por_dia.get(dia, 0) + cuantos <= puerta.limite_diario
            )
            if cabe:
                recientes.extend([ahora] * cuantos)
                puerta._por_ip[ip] = recientes
                puerta._por_dia[dia] = puerta._por_dia.get(dia, 0) + cuantos
        return not cabe

    # ------------------------------------------------------------------
    # Llave: reenvío a la API privada
    # ------------------------------------------------------------------

    def _llave_valida(self):
        """Compara la llave en tiempo constante. Sin llave, siempre falla."""
        token = self.headers.get("X-Celestia-Token") or ""
        llave = self.puerta.llave
        if not llave or not token:
            return False
        try:
            return hmac.compare_digest(
                token.encode("utf-8"), llave.encode("utf-8")
            )
        except Exception:  # noqa: BLE001 - ante la duda, cerrado
            return False

    def _con_llave(self):
        # La llave se comprueba ANTES de tocar el cuerpo: sin llave no se lee
        # ni un byte, aunque el Content-Length sea enorme.
        if not self._llave_valida():
            self._enviar_json(401, {"error": "unauthorized"})
            return

        if self.command in METODOS_CON_CUERPO:
            self._datos, grande = self._leer_cuerpo(MAX_CUERPO_PRIVADO)
        else:
            self._datos, grande = b"", False
        if grande:
            self._enviar_json(413, {"error": "cuerpo demasiado grande"})
            return

        self._reenviar_privado()

    def _reenviar_privado(self):
        datos = self._datos if self.command in METODOS_CON_CUERPO else None
        peticion = urllib.request.Request(
            self.puerta.api_privada + self.path,
            data=datos,
            method=self.command,
        )
        for nombre, valor in self.headers.items():
            bajo = nombre.lower()
            if bajo in SALTAR_CABECERAS:
                continue
            if bajo.startswith("x-forwarded-") or bajo.startswith("cf-"):
                continue
            peticion.add_header(nombre, valor)

        try:
            with urllib.request.urlopen(peticion, timeout=ESPERA_API) as respuesta:
                codigo = respuesta.getcode()
                tipo = respuesta.headers.get("Content-Type", "application/json")
                cuerpo = respuesta.read()
        except urllib.error.HTTPError as error:
            codigo = error.code
            tipo = "application/json"
            if error.headers is not None:
                tipo = error.headers.get("Content-Type", tipo)
            cuerpo = error.read()
        except Exception:  # noqa: BLE001 - la API no contesta
            self._enviar_json(502, {"error": "Celestia no responde"})
            return
        self._enviar_crudo(codigo, cuerpo, tipo or "application/json")

    # ------------------------------------------------------------------
    # Carcasa pública (sólo lectura, sin llave)
    # ------------------------------------------------------------------

    def _reenviar_carcasa(self):
        peticion = urllib.request.Request(
            self.puerta.api_privada + self.path, method="GET"
        )
        try:
            with urllib.request.urlopen(peticion, timeout=ESPERA_API) as respuesta:
                codigo = respuesta.getcode()
                tipo = respuesta.headers.get("Content-Type", "application/octet-stream")
                cuerpo = respuesta.read()
        except urllib.error.HTTPError as error:
            codigo = error.code
            tipo = "application/octet-stream"
            if error.headers is not None:
                tipo = error.headers.get("Content-Type", tipo)
            cuerpo = error.read()
        except Exception:  # noqa: BLE001 - la API no contesta
            self._enviar_json(502, {"error": "Celestia no responde"})
            return
        self._enviar_crudo(codigo, cuerpo, tipo or "application/octet-stream")
