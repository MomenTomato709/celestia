"""Lo que el chat necesita del navegador y la vista web de Android no hace sola.

En el navegador del móvil (la web de siempre) el micrófono, el «+» para mandar
archivos y las descargas funcionaban porque Chrome los trae de serie. Dentro
de la app, la vista web de Android no hace nada de eso si nadie se lo dice:
el botón del micrófono fallaba y el «+» no abría nada.

`Cromo` (un WebChromeClient) abre el selector de archivos de Android y concede
el micrófono a la página de Celestia (y sólo a ella); el trabajo lo hace la
app (`app.py`), aquí sólo está el enganche. Las descargas y los enlaces de
fuera también van en `app.py`.

Es una clase de Java hecha en Python (`static_proxy`): Chaquopy la genera al
fabricar la APK porque está declarada en pyproject.toml
(`build_gradle_extra_content`). Por eso este módulo sólo importa Java arriba.
"""
from android.webkit import PermissionRequest, ValueCallback, WebChromeClient, WebView
from java import Override, jboolean, jvoid, static_proxy


class Cromo(static_proxy(WebChromeClient)):
    def __init__(self, app):
        super().__init__()
        self.app = app                    # la CelestiaApp (Python)

    @Override(jboolean, [WebView, ValueCallback, WebChromeClient.FileChooserParams])
    def onShowFileChooser(self, vista, devolver, opciones):
        return self.app._elegir_archivos(devolver, opciones)

    @Override(jvoid, [PermissionRequest])
    def onPermissionRequest(self, peticion):
        self.app._permiso_de_la_pagina(peticion)
