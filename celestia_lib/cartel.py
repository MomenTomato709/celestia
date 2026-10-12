"""Una imagen con un texto concreto: un post, una historia, un cartel.

Examen de nivel (7 oct 2026): «Hazme una imagen para un post de Instagram de mi
peluquería… que ponga bien grande «Viernes de mechas -20 %»». Celestia le pasó
la frase al generador de imágenes y salió una chica genérica, sin una letra, con
la marca de agua de Pollinations y a 686×858. Los generadores de imágenes no
saben escribir: el texto lo tiene que poner un programa.

Así lo hace un diseñador, y así lo hace esto:
  `textos_de(peticion)`  — lo que tiene que poner, entre comillas, tal cual;
  `formato_de(peticion)` — el tamaño de la red social (post 4:5, historia 9:16…);
  `colores_de(peticion)` — los colores que se piden («dorados y negro»);
  `prompt_fondo(peticion, textos)` — lo que se le pide al generador: sólo el fondo,
                            sin letras;
  `componer(fondo, peticion, salida)` — el fondo recortado al formato (fuera la
                            marca de agua), un velo para que se lea, y los textos
                            con su jerarquía y una tipografía de verdad.
"""
from __future__ import annotations

import functools
import re
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

FUENTES = Path(__file__).resolve().parent / "fuentes"
TITULAR = FUENTES / "PlayfairDisplay.ttf"
TEXTO = FUENTES / "Montserrat.ttf"

FORMATOS = {"post": (1080, 1350), "cuadrado": (1080, 1080), "vertical": (1080, 1920),
            "horizontal": (1920, 1080)}
# Lo que se le pide al generador de fondos: más pequeño (tarda menos) y se escala.
FONDO_MAX = 1024
# La marca de agua de Pollinations va abajo a la derecha: ese trozo se recorta.
RECORTE_ABAJO = 0.07

_COMILLAS_RE = re.compile(r"«([^»\n]{1,140})»|\"([^\"\n]{1,140})\"|“([^”\n]{1,140})”")
_COLORES = (
    (r"dorad|\boro\b|golden", (212, 175, 55)), (r"negr", (12, 12, 14)),
    (r"blanc", (250, 250, 248)), (r"rosa", (236, 150, 180)), (r"roj", (200, 35, 50)),
    (r"azul", (45, 95, 200)), (r"verde", (40, 150, 90)), (r"morad|lila|violet", (130, 75, 170)),
    (r"platead|plata\b", (200, 200, 210)), (r"amarill", (245, 205, 50)),
    (r"naranja", (240, 140, 45)), (r"beige|crema\b|nude", (232, 216, 190)),
    (r"granate|burdeos", (120, 25, 40)), (r"turquesa", (40, 190, 190)))
_PEQUENO_RE = re.compile(r"(?:m[aá]s\s+)?(?:peque[ñn]\w*|debajo|abajo|menor|discret\w*|"
                         r"secundari\w*)\W{0,4}$", re.I)
_GRANDE_RE = re.compile(r"(?:bien\s+|muy\s+)?(?:grande|enorme|titular|destacad\w*)\W{0,4}$", re.I)


# Desde dónde empiezan los textos: en «mi peluquería, «Tijeras de Oro», en
# Valladolid… que ponga «Viernes de mechas -20 %»», el primero es el nombre del
# negocio, no algo que poner.
_PONGA_RE = re.compile(r"\b(?:que\s+(?:ponga|diga|lleve|salga|aparezca)|con\s+(?:el|los)\s+"
                       r"textos?|(?:el|los)\s+textos?\b|mostrando|escrito|rotulad\w*|"
                       r"t[ií]tulo|eslogan|lema|frase|r[oó]tulo)", re.I)


def textos_de(peticion: str) -> List[str]:
    """Los textos que tiene que llevar, tal cual, en orden y sin repetir (uno que
    ya va dentro de otro no se repite)."""
    peticion = peticion or ""
    m = _PONGA_RE.search(peticion)
    if not m:
        return []          # «una imagen estilo "Ghibli"»: las comillas no son un texto
    desde = m.start()
    vistos: List[str] = []
    for c in _COMILLAS_RE.finditer(peticion, desde):
        texto = next(g for g in c.groups() if g).strip()
        if texto and texto not in vistos:
            vistos.append(texto)
    return [x for x in vistos if not any(x != y and x in y for y in vistos)]


def jerarquia(peticion: str, textos: Sequence[str]) -> List[Tuple[str, bool]]:
    """(texto, ¿es el titular?): lo que se pide «bien grande» es el titular y lo de
    «más pequeño», no. Si no se dice, el primero es el titular."""
    salida = []
    for i, texto in enumerate(textos):
        pos = (peticion or "").find(texto)
        antes = (peticion or "")[max(0, pos - 40):max(0, pos - 1)] if pos >= 0 else ""
        if _PEQUENO_RE.search(antes):
            grande = False
        elif _GRANDE_RE.search(antes):
            grande = True
        else:
            grande = i == 0
        salida.append((texto, grande))
    if not any(g for _, g in salida) and salida:
        salida[0] = (salida[0][0], True)
    return salida


def formato_de(peticion: str) -> Tuple[int, int]:
    t = (peticion or "").lower()
    if re.search(r"9\s*[:x/]\s*16|vertical|historias?\b|stories|reels?\b|tiktok|estado\s+de\s+"
                 r"whatsapp|fondo\s+de\s+pantalla", t):
        return FORMATOS["vertical"]
    if re.search(r"16\s*[:x/]\s*9|horizontal|banner|miniatura|portada\s+de\s+youtube|youtube", t):
        return FORMATOS["horizontal"]
    if re.search(r"cuadrad|1\s*[:x/]\s*1\b", t):
        return FORMATOS["cuadrado"]
    if re.search(r"4\s*[:x/]\s*5|\bpost\b|publicaci[oó]n|instagram|facebook|cartel|flyer|"
                 r"p[oó]ster|anuncio", t):
        return FORMATOS["post"]
    return FORMATOS["cuadrado"]


def colores_de(peticion: str) -> List[Tuple[int, int, int]]:
    t = (peticion or "").lower()
    hallados = []
    for patron, rgb in _COLORES:
        m = re.search(patron, t)
        if m:
            hallados.append((m.start(), rgb))
    return [rgb for _, rgb in sorted(hallados)]


def _luz(rgb) -> float:
    r, g, b = rgb
    return 0.299 * r + 0.587 * g + 0.114 * b


def paleta(peticion: str) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
    """(color del texto, color del velo): el claro de los pedidos para las letras
    y el oscuro para el velo; si no se piden, blanco sobre negro."""
    colores = colores_de(peticion)
    claros = [c for c in colores if _luz(c) > 110]
    oscuros = [c for c in colores if _luz(c) <= 110]
    texto = claros[0] if claros else (250, 250, 248)
    velo = oscuros[0] if oscuros else (10, 10, 12)
    if _luz(texto) - _luz(velo) < 90:              # dos claros: el velo, oscuro
        velo = (10, 10, 12)
    return texto, velo


# El fondo se pide en inglés y como bodegón: con «peluquería» y «mechas» en español
# salía una modelo en primer plano y con letras inventadas por la IA (7 oct 2026).
_ESCENAS = (
    (r"peluquer|barber|sal[oó]n\s+de\s+belleza|est[eé]tica|mechas|tinte",
     "luxury hair salon still life: golden scissors and comb on marble"),
    (r"tatu|tattoo", "tattoo studio still life: ink bottles and a tattoo machine"),
    (r"restaurante|tapas|\bbar\b|cafeter|men[uú]\b|cocina",
     "restaurant still life: elegant plated dish and glasses on a table"),
    (r"pasteler|panader|reposter|tarta|cupcake", "bakery still life: pastries and cakes"),
    (r"gimnasio|fitness|entrenador|crossfit|yoga", "fitness still life: dumbbells and towel"),
    (r"florister|flores|ramo", "florist still life: fresh flowers and petals"),
    (r"inmobiliari|\bpiso\b|alquiler|vivienda", "bright modern home interior details"),
    (r"ropa|moda|boutique|zapater", "fashion boutique still life: fabrics and accessories"),
    (r"cl[ií]nica|dentista|fisio|m[eé]dic", "clean modern clinic details"),
    (r"cumplea[ñn]os|fiesta|boda|evento", "festive celebration still life: confetti and balloons"),
    (r"concierto|m[uú]sica|dj\b", "music stage lights and instruments"),
)
_COLORES_EN = ((r"dorad|\boro\b", "gold"), (r"negr", "black"), (r"blanc", "white"),
               (r"rosa", "pink"), (r"roj", "red"), (r"azul", "blue"), (r"verde", "green"),
               (r"morad|lila|violet", "purple"), (r"platead|plata\b", "silver"),
               (r"amarill", "yellow"), (r"naranja", "orange"), (r"beige|crema\b", "beige"),
               (r"granate|burdeos", "burgundy"), (r"turquesa", "turquoise"))


def prompt_fondo(peticion: str, textos: Sequence[str]) -> str:
    """Lo que se le pide al generador: el ambiente del negocio en inglés, con los
    colores pedidos, sin personas ni letras. Si no es un negocio conocido, lo que
    se describe (sin los textos), que puede ser el motivo («un gato que ponga…»)."""
    p = peticion or ""
    for texto in textos:
        p = p.replace(f"«{texto}»", "").replace(f"\"{texto}\"", "").replace(f"“{texto}”", "")
    t = p.lower()
    escena = next((e for patron, e in _ESCENAS if re.search(patron, t)), None)
    colores = [en for patron, en in _COLORES_EN if re.search(patron, t)]
    paleta_en = f", {' and '.join(colores[:3])} palette" if colores else ""
    if escena:
        return (f"{escena}{paleta_en}, soft bokeh, dramatic light, empty center, no people, "
                "no faces, no text, no letters, no logo, no watermark, high quality photo")
    p = re.sub(r"\b(?:que\s+(?:ponga|diga|lleve)|con\s+el\s+texto)\b[^.]*", "", p, flags=re.I)
    p = re.sub(r"\s+", " ", p).strip(" .,")
    return (f"{p}{paleta_en}. Background for a social media poster, empty center, "
            "no text, no letters, no words, no typography, no watermark, high quality")[:480]


_NUMERO_Y_UNIDAD_RE = re.compile(r"(\d)[ \t]+(%|€|\$|h\b|min\b|km\b|kg\b|años\b|euros\b)")


@functools.lru_cache(maxsize=1)
def _rasgos() -> dict:
    """Cifras de altura normal: Playfair dibuja por defecto las «antiguas», y en
    «-20 %» el 0 parecía una o (examen, 7 oct 2026). Pedirlas necesita libraqm;
    sin ella (puede faltar en el móvil) salen las de siempre."""
    try:
        from PIL import features
        return {"features": ["lnum"]} if features.check("raqm") else {}
    except Exception:
        return {}


def _fuente(ruta: Path, tam: int):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(str(ruta), tam)
    except OSError:
        return ImageFont.load_default()


def _partir(draw, texto: str, fuente, ancho_max: int) -> List[str]:
    # Un número no se separa de su unidad: «-20» en una línea y «%» solo en la
    # siguiente. Se une con un espacio duro, que no cuenta como corte.
    texto = _NUMERO_Y_UNIDAD_RE.sub("\\1\u00a0\\2", texto)
    palabras, lineas, actual = re.split(r"[ \t\n]+", texto.strip()), [], ""
    for p in palabras:
        prueba = f"{actual} {p}".strip()
        if actual and draw.textlength(prueba, font=fuente, **_rasgos()) > ancho_max:
            lineas.append(actual)
            actual = p
        else:
            actual = prueba
    if actual:
        lineas.append(actual)
    return lineas


def _encajar(draw, texto: str, ruta: Path, tam: int, ancho_max: int, max_lineas: int):
    """La fuente más grande con la que el texto cabe en `max_lineas` líneas."""
    while tam > 18:
        fuente = _fuente(ruta, tam)
        lineas = _partir(draw, texto, fuente, ancho_max)
        if len(lineas) <= max_lineas and all(
                draw.textlength(l, font=fuente, **_rasgos()) <= ancho_max for l in lineas):
            return fuente, lineas
        tam = int(tam * 0.92)
    fuente = _fuente(ruta, tam)
    return fuente, _partir(draw, texto, fuente, ancho_max)


def _fondo(fondo: Optional[Path], tam: Tuple[int, int], velo) -> Optional["Image.Image"]:
    from PIL import Image
    w, h = tam
    if fondo and Path(fondo).is_file():
        try:
            im = Image.open(fondo).convert("RGB")
            # Sin la franja de abajo (la marca de agua) y recortado al formato.
            iw, ih = im.size
            im = im.crop((0, 0, iw, int(ih * (1 - RECORTE_ABAJO))))
            iw, ih = im.size
            escala = max(w / iw, h / ih)
            im = im.resize((max(w, round(iw * escala)), max(h, round(ih * escala))),
                           Image.LANCZOS)
            iw, ih = im.size
            x, y = (iw - w) // 2, (ih - h) // 2
            return im.crop((x, y, x + w, y + h))
        except Exception:
            pass
    return None


def _fondo_disenado(tam: Tuple[int, int], velo, color) -> "Image.Image":
    """Sin foto (sin red, o Pollinations dice «402 Payment Required», que pasa en su
    nivel gratuito): un fondo hecho a mano. Resplandor cálido del color del texto
    en el centro, una textura suave y un marco doble con rombos en las esquinas."""
    from PIL import Image, ImageDraw, ImageFilter
    import random as _r
    w, h = tam
    base = Image.new("RGB", (w, h), velo)
    brillo = Image.new("RGB", (w, h), tuple(int(v * 0.75 + c * 0.25) for v, c in zip(velo, color)))
    radial = Image.radial_gradient("L").resize((w, h))          # 0 en el centro
    mascara = Image.eval(radial, lambda v: max(0, 210 - v))
    im = Image.composite(brillo, base, mascara)
    ruido = Image.effect_noise((w // 4, h // 4), 18).resize((w, h)).filter(ImageFilter.GaussianBlur(2))
    im = Image.blend(im, Image.merge("RGB", (ruido, ruido, ruido)), 0.06)
    d = ImageDraw.Draw(im)
    trazo = max(2, w // 360)
    for margen in (int(w * 0.045), int(w * 0.06)):
        d.rectangle((margen, margen, w - margen, h - margen), outline=color, width=trazo)
    lado = int(w * 0.022)
    m = int(w * 0.045)
    for cx, cy in ((m, m), (w - m, m), (m, h - m), (w - m, h - m)):
        d.polygon(((cx, cy - lado), (cx + lado, cy), (cx, cy + lado), (cx - lado, cy)), fill=color)
    return im


def _mezcla(c1, c2, f: float) -> Tuple[int, int, int]:
    return tuple(int(round(a + (b - a) * f)) for a, b in zip(c1, c2))


def _brillo(color, cuanto: float) -> Tuple[int, int, int]:
    """Más claro (cuanto > 0) o más oscuro (< 0)."""
    return _mezcla(color, (255, 255, 255) if cuanto > 0 else (0, 0, 0), abs(cuanto))


def _relleno_metalico(alto: int, color) -> "Image.Image":
    """Una columna con el degradado del brillo, de arriba abajo: claro, el color, una
    sombra y un reflejo. Juez del examen (post, 7 oct 2026): «texto plano»; el rival
    llevaba el dorado con brillo metálico."""
    from PIL import Image
    paradas = [(0.0, _brillo(color, 0.55)), (0.35, _brillo(color, 0.15)), (0.55, tuple(color)),
               (0.8, _brillo(color, -0.3)), (1.0, _brillo(color, 0.2))]
    col = Image.new("RGB", (1, max(1, alto)))
    px = col.load()
    for y in range(max(1, alto)):
        f = y / max(1, alto - 1)
        for (f0, c0), (f1, c1) in zip(paradas, paradas[1:]):
            if f0 <= f <= f1:
                px[0, y] = _mezcla(c0, c1, (f - f0) / max(1e-6, f1 - f0))
                break
    return col


def _pintar_linea(im, xy, linea: str, fuente, color, oscuro: bool) -> None:
    """La línea con relleno degradado, sombra suave y, sobre fondo oscuro, un halo."""
    from PIL import Image, ImageDraw, ImageFilter
    mascara = Image.new("L", im.size, 0)
    ImageDraw.Draw(mascara).text(xy, linea, font=fuente, fill=255, **_rasgos())
    caja = mascara.getbbox()
    if not caja:
        return
    radio = max(2, fuente.size // 22)
    desplaza = max(2, fuente.size // 30)
    sombra = Image.new("RGBA", im.size, (0, 0, 0, 0))
    sombra.putalpha(mascara.filter(ImageFilter.GaussianBlur(radio)).point(lambda v: v * 0.6))
    im.alpha_composite(sombra, (desplaza, desplaza))
    if oscuro:
        halo = Image.new("RGBA", im.size, tuple(color) + (0,))
        halo.putalpha(mascara.filter(ImageFilter.GaussianBlur(radio * 3)).point(lambda v: v * 0.35))
        im.alpha_composite(halo)
    relleno = _relleno_metalico(caja[3] - caja[1], color).resize((caja[2] - caja[0],
                                                                    caja[3] - caja[1]))
    capa = Image.new("RGBA", im.size, (0, 0, 0, 0))
    capa.paste(relleno.convert("RGBA"), caja[:2])
    capa.putalpha(mascara)
    im.alpha_composite(capa)


def _filete(draw, w: int, y: int, color, grosor: int) -> None:
    """Dos trazos finos con un rombo en medio."""
    largo, hueco, r = w // 10, max(10, w // 60), max(5, w // 140)
    medio = w // 2
    for x0, x1 in ((medio - hueco - largo, medio - hueco), (medio + hueco, medio + hueco + largo)):
        draw.line((x0, y, x1, y), fill=tuple(color) + (230,), width=grosor)
    draw.polygon([(medio, y - r), (medio + r, y), (medio, y + r), (medio - r, y)],
                 fill=tuple(color) + (240,))


# La cifra de una oferta al final del titular: «Viernes de mechas -20 %», «Rebajas 2x1».
_CIFRA_FINAL_RE = re.compile(
    r"^(.*?)[\s:·,–—-]*\s((?:[-–−]\s?)?\d+(?:[.,]\d+)?\s?(?:%|€)|\d\s?x\s?\d)\s*[!¡]?$",
    re.I)


def componer(fondo: Optional[Path], peticion: str, salida: Path,
             textos: Optional[Sequence[str]] = None) -> Path:
    """La imagen final, en PNG, con los textos exactos encima."""
    from PIL import Image, ImageDraw, ImageFilter
    textos = list(textos) if textos is not None else textos_de(peticion)
    w, h = formato_de(peticion)
    color, velo = paleta(peticion)
    foto = _fondo(fondo, (w, h), velo)
    if foto is None:
        im = _fondo_disenado((w, h), velo, color).convert("RGBA")
    else:
        im = foto.convert("RGBA")
        # El velo: oscurece la foto un poco y más la franja del texto, para que se lea.
        capa = Image.new("RGBA", (w, h), velo + (95,))
        banda = Image.new("L", (w, h), 0)
        ImageDraw.Draw(banda).rectangle((0, int(h * 0.28), w, int(h * 0.78)), fill=120)
        banda = banda.filter(ImageFilter.GaussianBlur(h // 12))
        capa.putalpha(Image.eval(banda, lambda v: min(255, 95 + v)))
        im = Image.alpha_composite(im, capa)

    draw = ImageDraw.Draw(im)
    margen = int(w * 0.09)
    ancho = w - 2 * margen
    # (fuente, líneas, tipo): «titular», «cifra» (la de la oferta, aparte y enorme,
    # como la llevaba el rival del examen) o «texto».
    bloques = []
    for texto, grande in jerarquia(peticion, textos):
        if grande:
            m = _CIFRA_FINAL_RE.match(texto)
            if m and m.group(1).strip():
                fuente, lineas = _encajar(draw, m.group(1).strip(), TITULAR, int(w * 0.13),
                                          ancho, 3)
                bloques.append((fuente, lineas, "titular"))
                fuente, lineas = _encajar(draw, m.group(2).strip(), TITULAR, int(w * 0.24),
                                          ancho, 1)
                bloques.append((fuente, lineas, "cifra"))
                continue
            fuente, lineas = _encajar(draw, texto, TITULAR, int(w * 0.13), ancho, 3)
            bloques.append((fuente, lineas, "titular"))
        else:
            fuente, lineas = _encajar(draw, texto, TEXTO, int(w * 0.045), ancho, 2)
            bloques.append((fuente, lineas, "texto"))

    # Se colocan con la caja real de cada línea (no con el tamaño de la fuente): con
    # Playfair, el filete de debajo rozaba la «s» de «mechas».
    interlinea = [int(f.size * (0.35 if tipo == "texto" else 0.18)) for f, _, tipo in bloques]
    cajas = [[draw.textbbox((0, 0), l, font=f, **_rasgos()) for l in lineas]
             for f, lineas, _ in bloques]
    separacion = int(h * 0.05)
    junto = int(h * 0.02)                      # entre el titular y su cifra
    tipos = [tipo for _, _, tipo in bloques]
    huecos = [junto if (a, b) == ("titular", "cifra") else separacion
              for a, b in zip(tipos, tipos[1:])]
    filete = max(2, w // 400)
    total = sum(sum(c[3] - c[1] for c in cs) + i * (len(cs) - 1)
                for cs, i in zip(cajas, interlinea)) + sum(huecos)
    y = (h - total) // 2
    # Fondo oscuro detrás del texto: el halo del color ayuda; en uno claro, ensucia.
    oscuro = sum(velo) / 3 < 110
    for n, ((fuente, lineas, tipo), cs, entre) in enumerate(zip(bloques, cajas, interlinea)):
        siguiente = tipos[n + 1] if n + 1 < len(tipos) else ""
        if tipo == "titular":                  # filetes finos alrededor del titular
            _filete(draw, w, y - separacion // 2, color, filete)
        for linea, caja in zip(lineas, cs):
            x = (w - (caja[2] - caja[0])) // 2 - caja[0]
            arriba = y - caja[1]                       # la tinta empieza justo en y
            _pintar_linea(im, (x, arriba), linea, fuente, color, oscuro)
            y += (caja[3] - caja[1]) + entre
        y -= entre
        if tipo == "cifra" or (tipo == "titular" and siguiente != "cifra"):
            _filete(draw, w, y + separacion // 2, color, filete)
        y += huecos[n] if n < len(huecos) else 0

    salida = Path(salida)
    salida.parent.mkdir(parents=True, exist_ok=True)
    im.convert("RGB").save(salida, "PNG", optimize=True)
    return salida


def tamano_fondo(peticion: str) -> Tuple[int, int]:
    """El tamaño que se le pide al generador del fondo (misma proporción, más pequeño)."""
    w, h = formato_de(peticion)
    escala = FONDO_MAX / max(w, h)
    return int(w * escala) // 8 * 8, int(h * escala) // 8 * 8
