"""Celestia como app de Android.

Al abrirse: arranca el servidor de Celestia en un hilo (el mismo `celestia.py`
de siempre, en modo API), espera a que conteste y enseña el chat web en una
vista web a pantalla completa. Mientras arranca se ve una portada.

Los datos de la persona (memoria, claves, lo aprendido) van a la carpeta
privada de la app (`CELESTIA_DATOS`), que sobrevive a las actualizaciones; el
código se reemplaza entero en cada una.
"""
from __future__ import annotations

import os
import runpy
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

import toga
from toga.style import Pack

def _puerto_libre(desde: int = 8765) -> int:
    """El primero libre desde 8765. En el móvil de Enzo el 8765 lo tiene la
    Celestia de Termux: la app se habría quedado sin servidor y enseñando el
    chat de la otra sin que se notara."""
    import socket
    for puerto in range(desde, desde + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", puerto))
                return puerto
            except OSError:
                continue
    return 0                                # ninguno: lo dice la portada


PUERTO = _puerto_libre()
URL = f"http://127.0.0.1:{PUERTO}"
AQUI = Path(__file__).resolve().parent

# La portada mientras arranca. Enzo (3 oct 2026): «no está centrado el inicio
# ni se ve bonito». Centrada con `position:fixed; inset:0` (en la vista web de
# Android `height:100%` no siempre llega al alto de la pantalla) y con el orbe
# de Celestia dibujado igual que en el chat.
PORTADA = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<style>
  html,body{{margin:0;background:#0B0D17;color:#F5F3FF;
    font-family:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}}
  .c{{position:fixed;inset:0;display:flex;flex-direction:column;align-items:center;
    justify-content:center;text-align:center;padding:24px;box-sizing:border-box}}
  svg{{width:120px;height:120px;margin-bottom:22px;
    filter:drop-shadow(0 0 26px rgba(139,124,246,.55));animation:latir 2.4s ease-in-out infinite}}
  @keyframes latir{{50%{{transform:scale(1.06);filter:drop-shadow(0 0 40px rgba(139,124,246,.8))}}}}
  .anillo{{transform-origin:50px 50px;animation:girar 9s linear infinite}}
  @keyframes girar{{to{{transform:rotate(336deg)}}}}
  h1{{margin:0;font-weight:600;letter-spacing:.34em;font-size:17px;text-transform:uppercase;
    background:linear-gradient(100deg,#A78BFA,#4FD1E0);-webkit-background-clip:text;
    background-clip:text;color:transparent}}
  p{{color:#8890B5;font-size:14px;line-height:1.5;margin:10px 0 0;max-width:320px}}
  pre{{text-align:left;white-space:pre-wrap;font-size:11px;color:#C9C7E8;
    max-height:45vh;overflow:auto;margin-top:16px;width:100%}}
</style></head><body><div class="c">
<svg viewBox="0 0 100 100" aria-hidden="true"><defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
<stop offset="0" stop-color="#C4B5FD"/><stop offset=".55" stop-color="#8B7CF6"/><stop offset="1" stop-color="#4FD1E0"/>
</linearGradient></defs>
<g class="anillo"><ellipse cx="50" cy="50" rx="44" ry="17" transform="rotate(-24 50 50)" fill="none" stroke="url(#g)" stroke-width="2.6"/>
<circle cx="88" cy="38" r="4" fill="#F472B6"/></g>
<ellipse cx="50" cy="50" rx="17" ry="44" transform="rotate(-24 50 50)" fill="none" stroke="url(#g)" stroke-width="2" opacity=".5"/>
<path d="M50 14Q50 50 86 50Q50 50 50 86Q50 50 14 50Q50 50 50 14Z" fill="url(#g)"/></svg>
<h1>Celestia</h1><p>{mensaje}</p>{detalle}</div></body></html>"""

FONDO = "#0B0D17"            # el de la portada y el del chat: sin franjas de otro color


def _zona_horaria() -> str:
    try:
        from java.util import TimeZone      # Chaquopy: Java desde Python
        return str(TimeZone.getDefault().getID())
    except Exception:
        return ""


# Una marca por arranque: sólo vale el /estado que la devuelve. Sin esto, si
# otra Celestia del aparato contestaba en ese puerto (la de Termux), la app
# enseñaba su chat como si fuera el propio (lo cazó Codex).
INSTANCIA = __import__("secrets").token_hex(8)


def _contesta() -> bool:
    import json
    try:
        with urllib.request.urlopen(URL + "/estado", timeout=3) as r:
            return json.loads(r.read(200_000)).get("instancia") == INSTANCIA
    except Exception:
        return False


class CelestiaApp(toga.App):
    def startup(self):
        datos = Path(self.paths.data)
        datos.mkdir(parents=True, exist_ok=True)
        os.environ.setdefault("CELESTIA_DATOS", str(datos))
        # Lo que crea (documentos, imágenes) también en su carpeta: sin permiso
        # de almacenamiento, /sdcard no es escribible para una app normal.
        os.environ.setdefault("CELESTIA_SALIDA", str(datos / "mis_archivos"))
        # Aún no maneja el móvil desde la app (Shizuku llegará después): que lo
        # diga en vez de buscar herramientas de Termux que aquí no existen.
        os.environ.setdefault("CELESTIA_SIN_MOVIL", "1")
        os.environ.setdefault("PYTHONUTF8", "1")
        os.environ["CELESTIA_INSTANCIA"] = INSTANCIA
        # Es la app: se actualiza sola con la APK (celestia_lib/actualizar.py).
        os.environ["CELESTIA_APP_ANDROID"] = "1"
        zona = _zona_horaria()
        if zona:
            os.environ.setdefault("CELESTIA_TZ", zona)

        self.web = toga.WebView(style=Pack(flex=1, background_color=FONDO))
        self._portada("Encendiendo…")
        self.main_window = toga.MainWindow(title=self.formal_name)
        self.main_window.content = self.web
        self.main_window.show()
        self._colores_del_sistema()
        self._respetar_barras_y_teclado()
        self._que_no_parezca_una_web()
        self._lo_que_hace_un_navegador()
        self._pedir_permiso_avisos()
        self._enganchar_a_celestia()

        self._error = ""
        if not PUERTO:
            self._portada("No he podido arrancar.",
                          "<p>Todos los puertos que uso (8765-8814) están ocupados.</p>")
            return
        threading.Thread(target=self._servidor, daemon=True, name="celestia").start()
        threading.Thread(target=self._esperar, daemon=True, name="espera").start()

    # ── Lo propio de Android (por Chaquopy: Java desde Python) ──────────────
    # Todo va con try: si una versión de Android no tiene algo, la app sigue.

    @staticmethod
    def _actividad():
        from org.beeware.android import MainActivity
        return MainActivity.singletonThis

    def _colores_del_sistema(self) -> None:
        """Barra de estado y de navegación del color de Celestia. Enzo: «se ve
        algo verde arriba siempre» — era el verde de la plantilla de Android."""
        try:
            from android.graphics import Color
            ventana = self._actividad().getWindow()
            ventana.setStatusBarColor(Color.parseColor(FONDO))
            ventana.setNavigationBarColor(Color.parseColor(FONDO))
            ventana.getDecorView().setBackgroundColor(Color.parseColor(FONDO))
        except Exception as e:
            print(f"Celestia: no pude poner los colores ({e})")

    def _respetar_barras_y_teclado(self) -> None:
        """Deja libre el hueco de la barra de estado, la de navegación y el
        teclado. Enzo (3 oct 2026, Android 16): «se ve algo arriba del todo
        cortado» y lo que escribe «se queda abajo», tapado. Desde Android 15
        las apps se dibujan por DEBAJO de las barras y del teclado y cada una
        tiene que apartarse sola; toga no lo hace. Se pone de relleno en la
        vista que contiene todo, y se recalcula cuando sale o se va el teclado."""
        try:
            from android import R
            from android.os import Build
            from android.view import View, WindowInsets
            from java import dynamic_proxy

            class Bordes(dynamic_proxy(View.OnApplyWindowInsetsListener)):
                def onApplyWindowInsets(self, vista, bordes):
                    if Build.VERSION.SDK_INT >= 30:
                        b = bordes.getInsets(WindowInsets.Type.systemBars()
                                             | WindowInsets.Type.ime())
                        vista.setPadding(b.left, b.top, b.right, b.bottom)
                    else:                         # Android 8-10
                        vista.setPadding(bordes.getSystemWindowInsetLeft(),
                                         bordes.getSystemWindowInsetTop(),
                                         bordes.getSystemWindowInsetRight(),
                                         bordes.getSystemWindowInsetBottom())
                    return bordes

            contenido = self._actividad().findViewById(R.id.content)
            self._bordes = Bordes()               # que no lo recoja el recolector
            contenido.setOnApplyWindowInsetsListener(self._bordes)
            contenido.requestApplyInsets()
        except Exception as e:
            print(f"Celestia: no pude apartar el contenido de las barras ({e})")

    def _que_no_parezca_una_web(self) -> None:
        """Lo que delata a una página web dentro de una app. Enzo: «se sigue
        notando que es básicamente una web, y aún más porque se puede hacer
        zoom»: toga activa el zoom con dos dedos a propósito."""
        try:
            from android.graphics import Color
            from android.view import View
            from celestia_lib.version import VERSION
            nativa = self.web._impl.native
            ajustes = nativa.getSettings()
            ajustes.setSupportZoom(False)
            ajustes.setBuiltInZoomControls(False)
            ajustes.setDisplayZoomControls(False)
            ajustes.setTextZoom(100)          # el tamaño lo elige el chat (Ajustes)
            # «Escuchar sola su respuesta»: sin esto la vista web no deja sonar
            # un audio que no se ha tocado, y la voz se quedaba muda.
            ajustes.setMediaPlaybackRequiresUserGesture(False)
            nativa.setOverScrollMode(View.OVER_SCROLL_NEVER)   # sin el rebote azul
            nativa.setBackgroundColor(Color.parseColor(FONDO))  # sin fogonazo blanco
            # Para que el chat sepa que está en la app y no en un navegador
            # (los avisos los manda la app, no el navegador).
            ajustes.setUserAgentString(f"{ajustes.getUserAgentString()} CelestiaApp/{VERSION}")
        except Exception as e:
            print(f"Celestia: no pude ajustar la vista web ({e})")

    # ── Lo que en el navegador viene de serie ───────────────────────────────
    # En la web del móvil (Chrome) el micrófono, el «+», las descargas y los
    # enlaces funcionaban solos. La vista web de la app no hace nada de eso si
    # nadie se lo dice: el micrófono fallaba, el «+» no abría nada y un enlace
    # sustituía al chat por la página, sin forma de volver.

    def _lo_que_hace_un_navegador(self) -> None:
        nativa = self.web._impl.native
        try:
            from .cromo import Cromo
            self._cromo = Cromo(self)             # que no lo recoja el recolector
            nativa.setWebChromeClient(self._cromo)
        except Exception as e:
            print(f"Celestia: sin micrófono ni «+» en la vista web ({e})")
        try:
            from android.webkit import DownloadListener
            from java import dynamic_proxy
            app = self

            class Descargas(dynamic_proxy(DownloadListener)):
                def onDownloadStart(self, url, agente, disposicion, tipo, largo):
                    threading.Thread(target=app._descargar, args=(url, disposicion, tipo),
                                     daemon=True, name="descarga").start()

            self._descargas = Descargas()
            nativa.setDownloadListener(self._descargas)
        except Exception as e:
            print(f"Celestia: sin descargas en la vista web ({e})")
        try:
            self.web.on_navigation_starting = self._navegar
        except Exception as e:
            print(f"Celestia: los enlaces se abrirán dentro ({e})")

    def _navegar(self, widget, url: str, **_kw) -> bool:
        """Lo de Celestia, aquí; cualquier otra página, en el navegador."""
        if not url or url.startswith((URL, "about:", "data:", "blob:", "javascript:")):
            return True
        try:
            from android.content import Intent
            from android.net import Uri
            fuera = Intent(Intent.ACTION_VIEW, Uri.parse(url))
            fuera.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            self._actividad().startActivity(fuera)
        except Exception as e:
            print(f"Celestia: no pude abrir {url[:80]} fuera ({e})")
        return False

    def _elegir_archivos(self, devolver, opciones) -> bool:
        """El «+» del chat: el selector de archivos de Android (Cromo)."""
        try:
            from android.app import Activity
            from android.content import Intent
            from android.net import Uri
            from android.webkit import WebChromeClient
            from java import jarray
            pedido = opciones.createIntent()
            pedido.addCategory(Intent.CATEGORY_OPENABLE)
            if opciones.getMode() == WebChromeClient.FileChooserParams.MODE_OPEN_MULTIPLE:
                pedido.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, True)

            def al_volver(codigo, datos):
                elegidos = []
                if codigo == Activity.RESULT_OK and datos is not None:
                    clip = datos.getClipData()
                    if clip is not None and clip.getItemCount() > 0:
                        elegidos = [clip.getItemAt(i).getUri() for i in range(clip.getItemCount())]
                    elif datos.getData() is not None:
                        elegidos = [datos.getData()]
                # Siempre se contesta, aunque sea con nada: si no, la vista web
                # no vuelve a abrir el selector nunca más.
                devolver.onReceiveValue(jarray(Uri)(elegidos) if elegidos else None)

            self._impl.start_activity(pedido, on_complete=al_volver)
        except Exception as e:
            print(f"Celestia: no pude abrir el selector de archivos ({e})")
            devolver.onReceiveValue(None)
        return True

    def _permiso_de_la_pagina(self, peticion) -> None:
        """El micrófono para las notas de voz (Cromo). Sólo a la página de
        Celestia, y sólo el micrófono: la cámara o cualquier otra cosa, no."""
        from android.content.pm import PackageManager
        from android.webkit import PermissionRequest
        audio = PermissionRequest.RESOURCE_AUDIO_CAPTURE
        try:
            origen = str(peticion.getOrigin())
            if not origen.startswith(URL) or audio not in list(peticion.getResources()):
                peticion.deny()
                return
            permiso = "android.permission.RECORD_AUDIO"
            if self._actividad().checkSelfPermission(permiso) == PackageManager.PERMISSION_GRANTED:
                peticion.grant([audio])
                return

            def al_responder(_permisos, resultados):
                if len(resultados) and resultados[0] == PackageManager.PERMISSION_GRANTED:
                    peticion.grant([audio])
                else:
                    peticion.deny()
            self._impl.request_permissions([permiso], on_complete=al_responder)
        except Exception as e:
            print(f"Celestia: no pude dar el micrófono a la página ({e})")
            peticion.deny()

    def _descargar(self, url: str, disposicion: str, tipo: str) -> None:
        """Lo que el chat da para descargar → Descargas/Celestia (en un hilo)."""
        import re
        import urllib.parse
        if not url.startswith(URL):
            self.loop.call_soon_threadsafe(lambda: self._navegar(None, url))
            return
        try:
            with urllib.request.urlopen(url, timeout=180) as r:
                datos = r.read()
                disposicion = disposicion or r.headers.get("Content-Disposition", "")
                tipo = tipo or r.headers.get_content_type()
            m = (re.search(r"filename\*=UTF-8''([^;]+)", disposicion or "", re.I)
                 or re.search(r'filename="?([^";]+)"?', disposicion or "", re.I))
            nombre = urllib.parse.unquote(m.group(1)) if m else \
                urllib.parse.unquote(urllib.parse.urlparse(url).path.rsplit("/", 1)[-1]) or "descarga"
            nombre = re.sub(r'[\\/:*?"<>|]+', "_", nombre).strip() or "descarga"
            self._guardar_en_descargas(nombre, tipo or "application/octet-stream", datos)
            self._aviso_corto(f"Guardado en Descargas/Celestia: {nombre}")
        except Exception as e:
            self._aviso_corto(f"No he podido descargarlo ({e})")

    def _guardar_en_descargas(self, nombre: str, tipo: str, datos: bytes) -> None:
        from android.os import Build, Environment
        act = self._actividad()
        if Build.VERSION.SDK_INT >= 29:
            from android.content import ContentValues
            from android.provider import MediaStore
            valores = ContentValues()
            valores.put(MediaStore.MediaColumns.DISPLAY_NAME, nombre)
            valores.put(MediaStore.MediaColumns.MIME_TYPE, tipo)
            valores.put(MediaStore.MediaColumns.RELATIVE_PATH,
                        f"{Environment.DIRECTORY_DOWNLOADS}/Celestia")
            resolver = act.getContentResolver()
            destino = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, valores)
            salida = resolver.openOutputStream(destino)
            try:
                salida.write(datos)
            finally:
                salida.close()
        else:
            # Android 8-9: la carpeta de descargas de la propia app (sin permisos).
            carpeta = Path(str(act.getExternalFilesDir(Environment.DIRECTORY_DOWNLOADS)))
            carpeta.mkdir(parents=True, exist_ok=True)
            (carpeta / nombre).write_bytes(datos)

    def _aviso_corto(self, texto: str) -> None:
        """Un mensajito de Android abajo (Toast), desde cualquier hilo."""
        def mostrar():
            try:
                from android.widget import Toast
                Toast.makeText(self._actividad(), texto, Toast.LENGTH_LONG).show()
            except Exception as e:
                print(f"Celestia: {texto} ({e})")
        self.loop.call_soon_threadsafe(mostrar)

    def _pedir_permiso_avisos(self) -> None:
        """Android 13+ pide permiso para mandar notificaciones (una vez)."""
        try:
            from android.os import Build
            if Build.VERSION.SDK_INT < 33:
                return
            from android.content.pm import PackageManager
            act = self._actividad()
            permiso = "android.permission.POST_NOTIFICATIONS"
            if act.checkSelfPermission(permiso) != PackageManager.PERMISSION_GRANTED:
                from java import jarray, jclass
                act.requestPermissions(jarray(jclass("java.lang.String"))([permiso]), 7)
        except Exception as e:
            print(f"Celestia: no pude pedir el permiso de avisos ({e})")

    def _enganchar_a_celestia(self) -> None:
        """Le da a Celestia (que corre aquí dentro) las dos cosas que sólo sabe
        hacer la app: avisar y ponerse una versión nueva."""
        try:
            from celestia_lib import actualizar, push
            push.NATIVO_APP = self._avisar
            actualizar.INSTALAR_APK = self._instalar_apk
        except Exception as e:
            print(f"Celestia: no pude enganchar avisos/actualización ({e})")

    def _avisar(self, titulo: str, texto: str) -> bool:
        """Una notificación de Android; al tocarla se abre la app."""
        from android.app import Notification, NotificationChannel, NotificationManager, PendingIntent
        from android.content import Context
        from android.content.pm import PackageManager
        from android.os import Build
        act = self._actividad()
        if (Build.VERSION.SDK_INT >= 33 and act.checkSelfPermission(
                "android.permission.POST_NOTIFICATIONS") != PackageManager.PERMISSION_GRANTED):
            # Sin permiso Android lo tira sin decir nada: se vuelve a pedir.
            self.loop.call_soon_threadsafe(self._pedir_permiso_avisos)
            return False
        gestor = act.getSystemService(Context.NOTIFICATION_SERVICE)
        canal = "celestia"
        gestor.createNotificationChannel(NotificationChannel(
            canal, "Avisos de Celestia", NotificationManager.IMPORTANCE_DEFAULT))
        abrir = act.getPackageManager().getLaunchIntentForPackage(act.getPackageName())
        toque = PendingIntent.getActivity(act, 0, abrir, PendingIntent.FLAG_IMMUTABLE
                                          | PendingIntent.FLAG_UPDATE_CURRENT)
        aviso = (Notification.Builder(act, canal)
                 .setSmallIcon(act.getApplicationInfo().icon)
                 .setContentTitle(titulo).setContentText(texto)
                 .setStyle(Notification.BigTextStyle().bigText(texto))
                 .setContentIntent(toque).setAutoCancel(True).build())
        self._avisos = getattr(self, "_avisos", 0) + 1
        gestor.notify(self._avisos, aviso)
        return True

    def _instalar_apk(self, url: str, tamano: int) -> None:
        """Baja la APK nueva con el gestor de descargas de Android y abre su
        instalador. Android exige que la persona toque «Instalar»; la primera
        vez, además, que permita a Celestia instalar apps. Va en un hilo
        (actualizar.instalar) y espera a que la descarga termine."""
        from android.app import DownloadManager
        from android.content import Context, Intent
        from android.net import Uri
        from android.provider import Settings
        from java import jarray, jlong
        from celestia_lib import actualizar
        act = self._actividad()
        dm = act.getSystemService(Context.DOWNLOAD_SERVICE)
        pedido = DownloadManager.Request(Uri.parse(url))
        pedido.setTitle("Celestia: versión nueva")
        pedido.setMimeType("application/vnd.android.package-archive")
        pedido.setDestinationInExternalFilesDir(act, None, f"Celestia-{int(time.time())}.apk")
        pedido.setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE)
        ident = dm.enqueue(pedido)
        actualizar.poner_fase("android", 0)

        # La primera vez Android no deja a una app instalar otras: se abre ese
        # ajuste YA, mientras descarga, en vez de al final (Enzo: «tarda mucho»).
        def puede_instalar() -> bool:
            return bool(act.getPackageManager().canRequestPackageInstalls())
        if not puede_instalar():
            ajuste = Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
                            Uri.parse(f"package:{act.getPackageName()}"))
            ajuste.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            act.startActivity(ajuste)

        consulta = DownloadManager.Query()
        consulta.setFilterById(jarray(jlong)([ident]))
        esperas = {DownloadManager.PAUSED_WAITING_FOR_NETWORK: "esperando conexión",
                   DownloadManager.PAUSED_QUEUED_FOR_WIFI: "esperando a tener WiFi",
                   DownloadManager.PAUSED_WAITING_TO_RETRY: "reintentando"}
        descargada = False
        limite = time.time() + 1800
        while time.time() < limite:
            cursor = dm.query(consulta)
            try:
                if not cursor.moveToFirst():
                    raise RuntimeError("la descarga se ha cancelado")
                col = cursor.getColumnIndex
                estado = cursor.getInt(col(DownloadManager.COLUMN_STATUS))
                hecho = cursor.getLong(col(DownloadManager.COLUMN_BYTES_DOWNLOADED_SO_FAR))
                total = cursor.getLong(col(DownloadManager.COLUMN_TOTAL_SIZE_BYTES)) or tamano
                if estado == DownloadManager.STATUS_FAILED:
                    raise RuntimeError("la descarga falló (¿hay internet?)")
                descargada = estado == DownloadManager.STATUS_SUCCESSFUL
                progreso = 100 if descargada else (int(hecho * 100 // total) if total > 0 else None)
                pausa = (esperas.get(cursor.getInt(col(DownloadManager.COLUMN_REASON)), "en pausa")
                         if estado == DownloadManager.STATUS_PAUSED else "")
            finally:
                cursor.close()
            if not puede_instalar():
                actualizar.poner_fase("android-permiso", progreso)
            elif descargada:
                break
            else:
                actualizar.poner_fase("android", progreso, pausa and f"Android la tiene {pausa}.")
            time.sleep(1)
        else:
            raise RuntimeError("tarda demasiado (media hora sin terminar)")
        actualizar.poner_fase("android-instalar", 100)
        apk = dm.getUriForDownloadedFile(ident)
        instalar = Intent(Intent.ACTION_VIEW)
        instalar.setDataAndType(apk, "application/vnd.android.package-archive")
        instalar.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_ACTIVITY_NEW_TASK)
        act.startActivity(instalar)

    def _portada(self, mensaje: str, detalle: str = "") -> None:
        self.web.set_content(URL, PORTADA.format(mensaje=mensaje, detalle=detalle))

    def _servidor(self) -> None:
        """El `celestia.py` de siempre, como si se lanzara a mano."""
        try:
            sys.argv = ["celestia.py", "--modo", "whatsapp", "--wa-puerto", str(PUERTO)]
            # Como módulo y no como fichero: dentro de la APK el código no está
            # en el disco (Chaquopy lo carga del propio paquete), y run_path
            # fallaba con «can't find '__main__' module».
            runpy.run_module("celestia_movil.nucleo", run_name="__main__", alter_sys=True)
        except SystemExit:
            pass
        except BaseException:
            self._error = traceback.format_exc()
        if not self._error:
            self._error = "Celestia se ha cerrado."

    def _esperar(self) -> None:
        inicio = time.time()
        while time.time() - inicio < 240:
            if _contesta():
                self.loop.call_soon_threadsafe(self._abrir_chat)
                return
            if self._error:
                break
            time.sleep(1)
        error = self._error or "Tarda demasiado en arrancar."
        detalle = "<pre>" + error[-3000:].replace("&", "&amp;").replace("<", "&lt;") + "</pre>"
        self.loop.call_soon_threadsafe(
            lambda: self._portada("No he podido arrancar.", detalle))

    def _abrir_chat(self) -> None:
        self.web.url = URL + "/chat"


def main():
    return CelestiaApp("Celestia", "io.github.momentomato709.celestia_movil")
