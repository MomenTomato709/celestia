<p align="center">
  <img src="imagenes/celestia_logo.png" alt="El orbe de Celestia" width="160">
</p>

<h1 align="center">Celestia</h1>

<p align="center">Tu IA personal: vive en tu aparato, recuerda lo que importa y hace cosas de verdad.</p>

---

## Descargar

**[momentomato709.github.io/celestia/descargar](https://momentomato709.github.io/celestia/descargar.html)**:
la página reconoce tu sistema y te da el instalador. Hay para **Windows**, **Mac** (Apple e
Intel), **Linux** y **Android**. Se instala sin permisos de administrador y se actualiza
sola.

La primera vez te pedirá una **clave de IA gratis** (Gemini o Groq): se saca en un minuto
y se pega en el chat. Celestia te dice dónde.

## Qué hace

- **Charla** como una persona, en tu idioma, y **recuerda** lo que le cuentas entre
  conversaciones.
- **Busca en internet** (varios buscadores a la vez) y te da el dato, no el anuncio.
- **Ve** tus fotos y capturas, **oye** tus notas de voz y **contesta con voz**.
- **Recordatorios** que llegan aunque cierres la ventana.
- **Crea** documentos (PDF, Word…), proyectos enteros e imágenes, y convierte fotos en
  dibujos de líneas.
- En el ordenador, **abre y cierra programas**, cambia el volumen y mira la pantalla.
- **Aprende** habilidades nuevas cuando se lo pides («aprende a…»).
- Si quieres, también por **WhatsApp, Telegram o Discord** (con tu número o tu bot).

En **Ajustes → Sentidos** ves qué puede hacer en tu aparato y qué clave gratis le falta
para el resto.

## Privacidad

Tu memoria, tus conversaciones y tus claves se guardan **sólo en tu aparato**. Para
pensar usa el proveedor de IA de la clave que tú le das (Gemini, Groq, OpenRouter,
DeepSeek…); no hay ningún servidor de Celestia en medio.

## Para desarrolladores

Celestia es Python (Flask) con un chat web propio (`celestia_lib/web/chat.html`).

```bash
python celestia.py --modo whatsapp --wa-puerto 8765   # el servidor; chat en /chat
python lanzador.py                                    # lo mismo, en su ventana
```

- `instalador/construir.py <destino>` fabrica el instalador de cada sistema (Python
  portátil + librerías ya compiladas + el código). Lo usa `.github/workflows/instaladores.yml`.
- `android/` es la app de Android (BeeWare Briefcase + Chaquopy); la fabrica
  `.github/workflows/android.yml`.
- Dependencias del instalador de escritorio: `dependencias/escritorio.txt`.

## Licencia

Ver [`LICENSE`](./LICENSE).
