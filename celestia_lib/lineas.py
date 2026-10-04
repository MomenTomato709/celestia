"""Pasar una foto a blanco y negro o dejarla sólo en líneas, sin ningún modelo.

26 sep 2026, chat web: Enzo mandó un diseño y pidió «dámelo en blanco y negro
para hacerlo tattoo e imprimirlo», y después «simplemente quiero las líneas
para pintarlo yo». Celestia contestó las dos veces que no podía editar fotos.
Es procesado de imagen de toda la vida: numpy, PIL y scipy, que ya están.

Dos salidas:
- `blanco_y_negro`: escala de grises con el contraste estirado, para imprimir.
- `lineas`: sólo los contornos, negro sobre blanco, para calcar o colorear.
  Es una diferencia de gaussianas (los bordes son donde la imagen cambia) con
  las motas sueltas quitadas: sin eso, la textura de la piel o del papel sale
  como lluvia de puntos.
"""
from __future__ import annotations

import io
import re
import time
from pathlib import Path
from typing import Optional

from .paths import MEM_DIR

# Las imágenes que manda el usuario y lo que se le devuelve. Fuera de git.
CARPETA = MEM_DIR / "fotos_chat"
# Más grande no aporta nada al imprimir en A4 y multiplica la RAM (límite 85 %).
LADO_MAX = 2000
# Lo que se acepta abrir: una foto de móvil anda por 12-50 Mpx. Un fichero
# pequeño muy comprimido puede declarar mucho más y reventar la RAM al
# descomprimirse (revisión de Codex): se mira el tamaño ANTES de cargarla.
MAX_PIXELES = 60_000_000
# Una foto vieja no es «esa foto»: pasado este rato hay que volver a mandarla.
VIGENCIA_SEG = 45 * 60

# Lo que se pide. Las líneas van primero: «las líneas en blanco y negro» son
# líneas, no una foto en grises.
PIDE_LINEAS_RE = re.compile(
    r"(?i)\b(?:l[ií]neas?|contornos?|silueta|trazos?|calc(?:ar|o)|plantilla|"
    r"stencil|para\s+(?:colorear|pintar(?:lo|la)?|dibujar(?:lo|la)?|calcar)|"
    r"line\s*art|dibujo\s+lineal|"
    # 27 sep 2026: «Pero en una hoja solo quiero el dibujo» (sin el brazo)
    # se contestó preguntando qué diseño quería.
    r"s[oó]lo\s+(?:quiero\s+)?el\s+dibujo|el\s+dibujo\s+s[oó]lo|en\s+una\s+hoja|"
    r"sin\s+(?:el\s+)?(?:brazo|piel|fondo|pierna|mano))\b")
# Tatuajes y dibujos ya traen sus líneas en tinta: ahí se saca la tinta, no
# los bordes (ver `lineas`).
ES_DIBUJO_RE = re.compile(
    r"(?i)\b(?:tattoos?|tatuajes?|dibujos?|diseños?|ilustraci[oó]n(?:es)?|"
    r"c[oó]mics?|mangas?|calcar|calco|stencil|plantilla)\b")
PIDE_BN_RE = re.compile(
    r"(?i)\b(?:blanco\s+y\s+negro|escala\s+de\s+grises|en\s+grises|"
    r"sin\s+color(?:es)?|monocrom[oa]|b\s*/\s*n)\b")
# Tiene que hablar de LA imagen: «me gusta el blanco y negro» o «quiero pelis
# en blanco y negro» no son un encargo, aunque haya una foto reciente en el
# hilo (revisión de Codex). Vale nombrarla o señalarla con el pronombre
# pegado al verbo («pásala», «pintarlo», «dámela»).
HABLA_DE_IMAGEN_RE = re.compile(
    r"(?i)\b(?:fotos?|im[aá]gen(?:es)?|dibujos?|diseños?|tattoos?|tatuajes?|"
    r"(?:p[aá]sa|haz|conviert[ea]|d[aá]me|pon|p[oó]n|d[eé]ja|d[eé]jame)(?:la|lo|mela|melo)|"
    r"(?:imprimir|emprimir|pintar|colorear|calcar|tatuar|tatuarme)(?:la|lo|mela|melo))\b")
# Con la foto adjunta en el mismo mensaje basta con que lo pida.
PIDE_ALGO_RE = re.compile(
    r"(?i)\b(?:quiero|quisiera|d[aá]me|dame|dieses|dar[ií]as|puedes|podr[ií]as|"
    r"hazme|p[aá]same|ponme|convi[eé]rte|necesito|me\s+gustar[ií]a)\b")


# «¿Y qué puedes hacer con esa foto?» — contestaba la lista general de todo lo
# que sabe hacer, como si no hubiera foto (26 sep 2026).
PREGUNTA_QUE_HACER_RE = re.compile(
    r"(?i)\bqu[eé]\s+(?:m[aá]s\s+)?(?:puedes|podr[ií]as|sabes|se\s+puede)\s+hacer\s+"
    r"con\s+(?:esa|esta|la|mi|el|ese|este)\s+(?:foto|imagen|dibujo|diseño)\b")
QUE_HAGO_CON_LA_FOTO = (
    "Con esa foto puedo:\n"
    "  • Pasarla a blanco y negro, con el contraste ajustado para imprimir.\n"
    "  • Dejarla sólo en líneas, negro sobre blanco, para calcarla o pintarla tú.\n"
    "  • Decirte qué veo en ella.\n"
    "  • Hacer una imagen nueva inspirada en ella, si me cuentas cómo la quieres.\n"
    "Dime cuál.")


# Cuánta tinta se queda un calco: de más limpio a más detalle. Cuanto más
# bajo, más oscura tiene que ser la línea para quedarse, y el sombreado (gris)
# se va. 27 sep 2026: con 0,45 Enzo lo vio «muy saturado» para tatuar.
NIVELES_TINTA = (0.25, 0.32, 0.45)
NIVEL_DEFECTO = 1

# «Muy saturado», «más limpio», «más detalle»: rehacer el calco sin volver a
# mandar la foto.
MENOS_DETALLE_RE = re.compile(
    r"(?i)\b(?:(?:muy|demasiado|mu)\s+(?:saturad|cargad|recargad|suci|lleno)\w*|"
    r"m[aá]s\s+limpi\w*|menos\s+(?:detalle|l[ií]neas|trazos|saturad\w*|cargad\w*|sombras?)|"
    r"quita(?:le)?\s+(?:el\s+|los\s+)?(?:sombread\w*|puntitos|manchas)|sin\s+sombra\w*)")
MAS_DETALLE_RE = re.compile(
    r"(?i)\b(?:m[aá]s\s+(?:detalle|l[ií]neas|trazos)|faltan?\s+(?:l[ií]neas|trazos|detalles?|cosas))")

# Lo último que se pidió en cada hilo: si luego llega otra foto («Solo tengo
# esta»), se le hace lo mismo. Se contestó con una orden escrita en crudo.
_ULTIMO_MODO: dict = {}      # hilo → (modo, cuándo, nivel, es_dibujo, es_tatuaje)


def recordar_modo(hilo: Optional[str], modo: str, nivel: int = NIVEL_DEFECTO,
                  es_dibujo: bool = False, es_tatuaje: bool = False) -> None:
    ahora = time.time()
    for clave in [c for c, v in _ULTIMO_MODO.items() if ahora - v[1] > VIGENCIA_SEG]:
        del _ULTIMO_MODO[clave]
    _ULTIMO_MODO[hilo or "principal"] = (modo, ahora, nivel, es_dibujo, es_tatuaje)
    # Y en disco, junto a la foto: tras un reinicio (o si el calco se mandó al
    # buzón sin pasar por el chat) «más limpio» no sabía de qué hablaba.
    try:
        import json
        CARPETA.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        (CARPETA / f"modo_{_nombre_hilo(hilo)}.json").write_text(json.dumps(
            {"modo": modo, "ts": ahora, "nivel": nivel, "es_dibujo": es_dibujo,
             "es_tatuaje": es_tatuaje}))
    except OSError:
        pass


def _reciente(hilo: Optional[str]):
    v = _ULTIMO_MODO.get(hilo or "principal")
    if v is None:
        try:
            import json
            d = json.loads((CARPETA / f"modo_{_nombre_hilo(hilo)}.json").read_text())
            v = (d["modo"], float(d["ts"]), int(d["nivel"]), bool(d["es_dibujo"]),
                 bool(d.get("es_tatuaje")))
        except (OSError, ValueError, KeyError):
            return None
    return v if time.time() - v[1] <= VIGENCIA_SEG else None


def modo_reciente(hilo: Optional[str]) -> Optional[str]:
    v = _reciente(hilo)
    return v[0] if v else None


def ajuste_pedido(texto: str, hilo: Optional[str]):
    """(nivel, es_dibujo, es_tatuaje, mensaje) si pide rehacer el último calco, o None."""
    v = _reciente(hilo)
    if not v or v[0] != "lineas":
        return None
    _, _, nivel, es_dibujo, es_tatuaje = v
    menos, mas = MENOS_DETALLE_RE.search(texto or ""), MAS_DETALLE_RE.search(texto or "")
    if menos and mas:
        return None          # pide las dos cosas: que lo aclare la conversación
    if menos:
        if nivel <= 0:
            return (0, es_dibujo, es_tatuaje, "Esta es la versión más limpia que sale de la foto: "
                    "lo que queda ya es la línea del propio tatuaje. Si quieres, "
                    "te la vuelvo a dar con más detalle.")
        return (nivel - 1, es_dibujo, es_tatuaje, "Aquí la tienes más limpia, con menos sombreado. "
                "Si se ha perdido alguna línea que querías, dime «más detalle».")
    if mas:
        if nivel >= len(NIVELES_TINTA) - 1:
            return (nivel, es_dibujo, es_tatuaje, "Esta es la versión con más detalle que sale de la foto. "
                    "Lo que no esté en ella no lo puedo sacar.")
        return (nivel + 1, es_dibujo, es_tatuaje, "Aquí la tienes con más detalle. Si queda muy "
                "cargada, dime «más limpio».")
    return None


# Prometer que va a redibujar, retocar o generar la imagen sin tener con qué.
# 27 sep 2026, buzón: «Puedo hacerlo… la redibujo», «Voy a generar la imagen
# ahora» y al final «dime qué quieres ver». Tres promesas, ninguna imagen.
PROMESA_IMAGEN_RE = re.compile(
    r"(?i)\b(?:(?:la|lo|te\s+la|te\s+lo)\s+(?:redibujo|dibujo|retoco|edito|genero|recreo|arreglo)|"
    r"voy\s+a\s+(?:generar|crear|dibujar|redibujar|recrear|retocar|editar)|"
    r"(?:generar[eé]|crear[eé]|dibujar[eé]|redibujar[eé]|recrear[eé]|retocar[eé])\b"
    r"[^.\n]{0,40}\b(?:imagen|dibujo|cola|versi[oó]n|foto|tatuaje|calco|dise[ñn]o)|"
    r"te\s+(?:enviar[eé]|mandar[eé])\s+la\s+(?:versi[oó]n|imagen)|"
    r"la\s+dibujar[eé]|puedo\s+(?:recrearla|redibujarla|retocarla|redibujar|recrear))")
SIN_EDITOR = (
    "Te tengo que ser sincera: no tengo forma de redibujar ni retocar partes de "
    "una foto (como rehacer la cola). Para eso hace falta un modelo de IA que "
    "edite imágenes, y los que tengo a mano no lo permiten gratis.\n\n"
    "Lo que sí puedo hacer con esta foto: dejarla sólo en líneas, aplanar el "
    "tatuaje si está en un brazo, y ajustarla más limpia o con más detalle.")


def que_pide(texto: str, con_imagen: bool = False) -> Optional[str]:
    """«lineas», «bn» o None si el mensaje no pide transformar una imagen.

    `con_imagen`: la foto viene en este mismo mensaje, así que no hace falta
    que la nombre («quiero sólo las líneas» con la foto adjunta).
    """
    t = texto or ""
    if not (HABLA_DE_IMAGEN_RE.search(t) or (con_imagen and PIDE_ALGO_RE.search(t))):
        return None
    if PIDE_LINEAS_RE.search(t):
        return "lineas"
    if PIDE_BN_RE.search(t):
        return "bn"
    return None


def _nombre_hilo(hilo: Optional[str]) -> str:
    """Nombre de fichero del hilo. Lleva un resumen del identificador ENTERO:
    limpiar a secas hacía que «a/b» y «a?b» fueran el mismo (revisión de Codex)."""
    import hashlib
    crudo = hilo or "principal"
    return (re.sub(r"[^\w-]", "_", crudo)[:60] + "-"
            + hashlib.sha1(crudo.encode("utf-8")).hexdigest()[:10])


def _limpiar_caducadas() -> None:
    """Borra las fotos de hilo que ya no valen: si no, crecen sin fin."""
    ahora = time.time()
    try:
        for f in [*CARPETA.glob("ultima_*.img"), *CARPETA.glob("modo_*.json")]:
            try:
                if ahora - f.stat().st_mtime > VIGENCIA_SEG:
                    f.unlink()
            except OSError:
                pass
    except OSError:
        pass


def guardar_foto(datos: bytes, hilo: Optional[str]) -> Optional[Path]:
    """Guarda la última foto del hilo para poder trabajar con ella después."""
    try:
        CARPETA.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass          # PRoot: `mkdir` a veces falla con la carpeta ya creada
    _limpiar_caducadas()
    ruta = CARPETA / f"ultima_{_nombre_hilo(hilo)}.img"
    try:
        tmp = ruta.with_suffix(".parcial")
        tmp.write_bytes(datos)
        tmp.replace(ruta)
        return ruta
    except OSError:
        return None


def ultima_foto(hilo: Optional[str]) -> Optional[bytes]:
    """La última foto que mandó en este hilo, si es reciente."""
    ruta = CARPETA / f"ultima_{_nombre_hilo(hilo)}.img"
    try:
        if time.time() - ruta.stat().st_mtime > VIGENCIA_SEG:
            return None
        return ruta.read_bytes()
    except OSError:
        return None


def _abrir(datos: bytes, en_color: bool = False):
    from PIL import Image, ImageOps
    img = Image.open(io.BytesIO(datos))        # perezoso: aún no hay píxeles
    ancho, alto = img.size
    if ancho * alto > MAX_PIXELES:
        raise ValueError(f"imagen demasiado grande ({ancho}×{alto})")
    # Un JPEG se puede descomprimir ya reducido: gasta una fracción de RAM.
    if img.format == "JPEG":
        img.draft("RGB", (LADO_MAX, LADO_MAX))
    img = ImageOps.exif_transpose(img)          # la foto del móvil, derecha
    if img.mode in ("RGBA", "LA", "P"):
        # Lo transparente, blanco: en negro se comería el dibujo.
        fondo = Image.new("RGB", img.size, (255, 255, 255))
        img = img.convert("RGBA")
        fondo.paste(img, mask=img.split()[-1])
        img = fondo
    img = img.convert("RGB" if en_color else "L")
    if max(img.size) > LADO_MAX:
        img.thumbnail((LADO_MAX, LADO_MAX))
    return img


def blanco_y_negro(datos: bytes):
    """Escala de grises con el contraste estirado (lo más oscuro, negro)."""
    from PIL import ImageOps
    return ImageOps.autocontrast(_abrir(datos), cutoff=1)


LADO_DESENROLLO = 1200
# Palabras que dicen que la foto es de un tatuaje (en la piel): sólo entonces se
# busca un brazo que desenrollar. Un dibujo sobre una mesa de madera también es
# «rojizo» y no hay que deformarlo (revisión de Codex).
ES_TATUAJE_RE = re.compile(r"(?i)\b(?:tattoos?|tatuajes?|tatuad[oa]s?|tatuar(?:me|te|se)?)\b")


def desenrollar_brazo(img):
    """El tatuaje de un brazo, aplanado. None si en la foto no hay un brazo.

    27 sep 2026: «sigue teniendo el volumen del brazo… la cola se ve muy fina
    por una parte por culpa del brazo». Un antebrazo es casi un cilindro: lo
    que queda hacia sus lados se ve de canto, aplastado. Se busca la silueta
    (piel frente a fondo), se pone derecha por su eje y cada fila se
    desenrolla: el punto a distancia x del centro está en el ángulo
    asin(x/R), y en plano ocupa R·ángulo.
    """
    import numpy as np
    from PIL import Image
    from scipy import ndimage

    # Para un calco sobra, y el desenrollado es lo que más RAM pide: a 2000 px
    # el pico medido eran 341 MB (revisión de Codex); a 1200, una fracción.
    img = img.convert("RGB")
    if max(img.size) > LADO_DESENROLLO:
        img = img.copy()
        img.thumbnail((LADO_DESENROLLO, LADO_DESENROLLO))
    a = np.asarray(img).astype(np.int16)
    piel = ((a[..., 0] - a[..., 2]) > 18) & (a.mean(axis=2) > 70)
    piel = ndimage.binary_closing(piel, iterations=6)
    et, n = ndimage.label(piel)
    if not n:
        return None
    tam = np.bincount(et.ravel())
    tam[0] = 0
    brazo = ndimage.binary_fill_holes(et == tam.argmax())
    del et, piel
    # Un brazo deja fondo a los lados: ni casi nada ni la foto entera.
    fraccion = float(brazo.mean())
    if not 0.25 < fraccion < 0.9:
        return None
    # Un brazo entra y sale del encuadre: toca al menos dos bordes de la foto.
    # Una taza o una cara en medio no toca ninguno. Con margen: el cierre de
    # arriba se come los píxeles pegados al borde de la imagen.
    k = 8
    bordes_tocados = sum(bool(b.any()) for b in
                         (brazo[:k, :], brazo[-k:, :], brazo[:, :k], brazo[:, -k:]))
    if bordes_tocados < 2:
        return None
    ys, xs = np.nonzero(brazo)
    _, vec = np.linalg.eigh(np.cov(np.vstack([xs, ys])))
    ang = float(np.degrees(np.arctan2(vec[1, 1], vec[0, 1])))
    mascara = Image.fromarray((brazo * 255).astype(np.uint8))
    # El giro que deja el brazo vertical: el de anchos más parejos por fila.
    mejor = None
    # Sólo giros de menos de 90°: vertical se queda de dos maneras, y la otra
    # deja el dibujo boca abajo (pasó al bajar la foto a 1200 px).
    def _corto(g):
        return (g + 90) % 180 - 90
    for giro in (_corto(ang - 90), _corto(90 - ang)):
        m = np.asarray(mascara.rotate(giro, expand=True)) > 127
        anchos = m.sum(axis=1)
        anchos = anchos[anchos > 0]
        nota = anchos.std() / max(1.0, anchos.mean()) + (m.sum(axis=0) > 0).mean()
        if mejor is None or nota < mejor[0]:
            mejor = (nota, giro)
    giro = mejor[1]
    color_piel = tuple(int(v) for v in np.median(a[brazo], axis=0))
    derecha = img.convert("RGB").rotate(giro, expand=True, resample=Image.BICUBIC,
                                        fillcolor=color_piel)
    m = np.asarray(mascara.rotate(giro, expand=True)) > 127
    alto, ancho = m.shape
    izq = np.full(alto, np.nan)
    der = np.full(alto, np.nan)
    for y in range(alto):
        x = np.flatnonzero(m[y])
        if len(x) > ancho * 0.2:
            izq[y], der[y] = x[0], x[-1]
    hay = ~np.isnan(izq)
    # Forma de brazo: lo recorre a lo largo, deja fondo a los lados y su
    # ancho cambia poco. Una mancha rojiza cualquiera no cumple las tres.
    if hay.sum() < alto * 0.5:
        return None
    de_lado_a_lado = (izq[hay] <= 1) & (der[hay] >= ancho - 2)
    anchos = der[hay] - izq[hay]
    if de_lado_a_lado.mean() > 0.2 or anchos.std() / max(1.0, anchos.mean()) > 0.35:
        return None
    filas = np.arange(alto)
    izq = ndimage.gaussian_filter1d(np.interp(filas, filas[hay], izq[hay]), 25)
    der = ndimage.gaussian_filter1d(np.interp(filas, filas[hay], der[hay]), 25)
    centro, radio = (izq + der) / 2, (der - izq) / 2
    # Hasta ±72°: más allá la piel se ve tan de canto que estirarla es
    # inventar píxeles.
    limite = float(np.arcsin(0.95))
    ancho_plano = int(2 * radio.max() * limite)
    angulos = (np.arange(ancho_plano) - ancho_plano / 2) / (ancho_plano / 2) * limite
    y0, y1 = np.flatnonzero(hay)[[0, -1]]
    filas = filas[y0:y1 + 1]
    xx = (centro[filas, None] + radio[filas, None] * np.sin(angulos)[None, :]).astype(np.float32)
    yy = np.broadcast_to(filas[:, None].astype(np.float32), xx.shape)
    fuente = np.asarray(derecha)
    del derecha, m, a, brazo
    # Canal a canal y directo a 8 bits: nunca hay tres planos en coma flotante.
    plano = np.empty(xx.shape + (3,), np.uint8)
    for k in range(3):
        canal = ndimage.map_coordinates(fuente[..., k].astype(np.float32), [yy, xx],
                                        order=1, cval=color_piel[k])
        plano[..., k] = canal.clip(0, 255)
        del canal
    return Image.fromarray(plano)


def tinta(datos, umbral: float = NIVELES_TINTA[NIVEL_DEFECTO],
          quitar_bordes: bool = True):
    """Las líneas de un tatuaje o un dibujo: su tinta negra, sobre blanco.

    27 sep 2026: con los bordes, el tatuaje de Enzo salió «muy feo» y «no se
    entiende para nada»: cada línea negra tiene DOS bordes, así que salía
    doble, y el ojo o la cara se perdían. Un dibujo ya trae sus líneas: basta
    con quedarse con lo oscuro. Oscuro respecto a lo que lo rodea, no un número
    fijo: la piel de un brazo tiene luz por un lado y sombra por el otro.
    """
    import numpy as np
    from PIL import Image
    from scipy import ndimage

    # Pico medido: ~100 MB extra con 2000 px. Se suelta el color en cuanto se
    # han sacado el gris y la saturación (revisión de Codex).
    color = np.asarray(_abrir(datos, en_color=True) if isinstance(datos, bytes)
                       else datos.convert("RGB"))
    saturacion = color.max(axis=2) - color.min(axis=2)          # uint8
    gris = color.mean(axis=2, dtype=np.float32)
    del color
    alto, ancho = gris.shape
    lado = max(alto, ancho)
    # Lo que tendría la piel (o el papel) sin tinta: el máximo del entorno.
    fondo = ndimage.gaussian_filter(
        ndimage.maximum_filter(ndimage.gaussian_filter(gris, 3), size=41), 15)
    # Tinta: bastante más oscura que su fondo y sin color (el rojo o el verde
    # del relleno no son línea).
    m = (gris / np.maximum(fondo, 1.0) < umbral) & (saturacion < 60)
    m = ndimage.median_filter(m.astype(np.uint8), size=3).astype(bool)
    del saturacion, fondo, gris
    et, n = ndimage.label(m)
    if not n:
        return Image.new("L", (ancho, alto), 255)
    tam = np.bincount(et.ravel(), minlength=n + 1)
    # Manchas, no líneas: lo que sobrevive a una apertura con un disco es
    # grueso. Una mancha casi toda gruesa (el «1/4» de Instagram, una sombra)
    # fuera; el dibujo tiene zonas rellenas, pero sobre todo línea.
    r = max(3, lado // 110)
    yy, xx = np.ogrid[-r:r + 1, -r:r + 1]
    grueso = ndimage.binary_opening(m, structure=(xx * xx + yy * yy) <= r * r)
    tam_grueso = np.bincount(et[grueso].ravel(), minlength=n + 1)
    quedan = (tam >= max(20, lado // 50)) & (tam_grueso < 0.5 * tam)
    # Lo que toca el borde de la foto y no es el dibujo principal: el borde
    # del brazo, la esquina de la mesa.
    principal = int(np.argmax(np.where(np.arange(n + 1) == 0, 0, tam)))
    if quitar_bordes:
        bordes = np.unique(np.concatenate([et[0, :], et[-1, :], et[:, 0], et[:, -1]]))
        quedan[bordes[bordes != principal]] = False
    quedan[0] = False
    return Image.fromarray(np.where(quedan[et], 0, 255).astype(np.uint8), mode="L")


def lineas(datos: bytes):
    """Sólo los contornos, negro sobre blanco. Para fotos (no dibujos)."""
    import numpy as np
    from PIL import Image
    from scipy import ndimage

    gris = np.asarray(_abrir(datos), dtype=np.float32)
    lado = max(gris.shape)
    # La escala del detalle va con el tamaño: en una foto de 2000 px, un
    # sigma de 1 ve el grano del papel; en una de 400 px, ya ve el dibujo.
    s = max(1.0, lado / 700)
    # La mediana borra la textura (tela, pelo, grano) y respeta los bordes:
    # sin ella, una camiseta jaspeada salía como lluvia de rayitas.
    gris = ndimage.median_filter(gris, size=int(2 * s) * 2 + 1)
    dog = ndimage.gaussian_filter(gris, s) - ndimage.gaussian_filter(gris, s * 1.6)
    # Un borde es donde la versión fina queda más oscura que la suave. El
    # umbral sale de la propia imagen, no de un número fijo: una foto con
    # poco contraste tendría si no el papel en blanco.
    umbral = -max(2.0, float(np.percentile(np.abs(dog), 92)) * 0.8)
    trazo = dog < umbral
    # Las motas: grupos de pocos píxeles sueltos (grano, poros, JPEG).
    etiquetas, n = ndimage.label(trazo)
    if n:
        tamanos = np.bincount(etiquetas.ravel(), minlength=n + 1)[1:]
        minimo = max(10, int(lado / 25))
        grandes = np.zeros(n + 1, dtype=bool)
        grandes[1:] = tamanos >= minimo
        trazo = grandes[etiquetas]
    # En imágenes grandes el trazo de un píxel no se ve al imprimir.
    if lado > 1200:
        trazo = ndimage.binary_dilation(trazo, iterations=1)
    return Image.fromarray(np.where(trazo, 0, 255).astype(np.uint8), mode="L")


def transformar(datos: bytes, modo: str, es_dibujo: bool = False,
                nivel: int = NIVEL_DEFECTO, es_tatuaje: bool = False) -> bytes:
    """Aplica el modo y devuelve el PNG. No se guarda: viaja en la respuesta.

    `es_dibujo`: un tatuaje o una ilustración, que ya trae sus líneas en tinta.
    """
    if modo == "lineas":
        if es_dibujo:
            # Un tatuaje en un brazo: primero se aplana. Lo que toca el borde
            # entonces es el propio tatuaje (la cola llegaba al canto), no el
            # borde del brazo, que ya no está.
            plano = desenrollar_brazo(_abrir(datos, en_color=True)) if es_tatuaje else None
            img = (tinta(plano, NIVELES_TINTA[nivel], quitar_bordes=False)
                   if plano is not None else tinta(datos, NIVELES_TINTA[nivel]))
        else:
            img = lineas(datos)
    else:
        img = blanco_y_negro(datos)
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


MENSAJE = {
    "lineas": ("Aquí la tienes sólo en líneas, negro sobre blanco, lista para "
               "imprimir y pintar. Si salen demasiados trazos o faltan, dímelo y "
               "la repaso."),
    "bn": ("Aquí la tienes en blanco y negro, con el contraste ajustado para "
           "imprimir. Si lo que quieres es sólo el contorno para pintarla tú, "
           "pídemela en líneas."),
}
