"""En qué idioma habla quien escribe, y en cuál debe contestar Celestia.

Hasta aquí el idioma se decidía con tres reglas dentro del turno y un `else`
que daba por hecho el español. Para una casa en España funciona; para alguien
que escriba en alemán, en japonés o en árabe, significa recibir una respuesta
en un idioma que no entiende.

Todo esto es **determinista** —escritura Unicode y palabras funcionales, sin
modelo y sin dependencias—, que es la única forma de que se comporte igual con
cualquier proveedor detrás y de que funcione sin internet.

Dos piezas:

* `detectar(texto)` — el idioma de un mensaje, o ``None`` si no está claro.
  Preferimos no saberlo a inventarlo: quien no sabe en qué idioma le hablan
  hace menos daño callándose que forzando el equivocado.
* `IDIOMAS` — los que se ofrecen para elegir a mano, con su nombre en su propia
  lengua (un menú que pone «Deutsch» lo encuentra quien busca alemán; uno que
  pone «Alemán», no).
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

# ── Los idiomas que se pueden elegir ─────────────────────────────────────
# `codigo: (nombre en su propia lengua, nombre en español)`. El orden es el que
# se ve en el menú: primero los de casa, luego por número de hablantes.
IDIOMAS: Dict[str, tuple] = {
    "es": ("Español", "español"),
    "en": ("English", "inglés"),
    "pt": ("Português", "portugués"),
    "fr": ("Français", "francés"),
    "de": ("Deutsch", "alemán"),
    "it": ("Italiano", "italiano"),
    "ca": ("Català", "catalán"),
    "gl": ("Galego", "gallego"),
    "eu": ("Euskara", "euskera"),
    "nl": ("Nederlands", "neerlandés"),
    "pl": ("Polski", "polaco"),
    "tr": ("Türkçe", "turco"),
    "ru": ("Русский", "ruso"),
    "uk": ("Українська", "ucraniano"),
    "ar": ("العربية", "árabe"),
    "he": ("עברית", "hebreo"),
    "hi": ("हिन्दी", "hindi"),
    "zh": ("中文", "chino"),
    "ja": ("日本語", "japonés"),
    "ko": ("한국어", "coreano"),
    "el": ("Ελληνικά", "griego"),
}

# El nombre en español, para escribirlo en la instrucción que ve el modelo.
NOMBRE_ES = {c: v[1] for c, v in IDIOMAS.items()}


def nombre_nativo(codigo: str) -> str:
    """«Deutsch» para `de`. El código crudo si no lo conocemos."""
    par = IDIOMAS.get((codigo or "").lower())
    return par[0] if par else (codigo or "")


def nombre_en_espanol(codigo: str) -> str:
    return NOMBRE_ES.get((codigo or "").lower(), codigo or "")


def es_valido(codigo: str) -> bool:
    return (codigo or "").lower() in IDIOMAS


# ── 1. Por la escritura ───────────────────────────────────────────────────
# Cuando el texto no está en alfabeto latino, el idioma se sabe casi con solo
# mirarlo, y eso no falla nunca: nadie escribe español en cirílico.
_ESCRITURAS = (
    ("ja", r"[぀-ゟ゠-ヿ]"),        # kana: japonés seguro
    ("ko", r"[가-힯ᄀ-ᇿ]"),        # hangul
    ("zh", r"[一-鿿]"),                     # han (tras descartar kana)
    ("ru", r"[Ѐ-ӿ]"),                     # cirílico
    ("ar", r"[؀-ۿݐ-ݿ]"),
    ("he", r"[֐-׿]"),
    ("hi", r"[ऀ-ॿ]"),                     # devanagari
    ("el", r"[Ͱ-Ͽἀ-῿]"),
    ("th", r"[฀-๿]"),
)
_ESCRITURAS_RE = [(c, re.compile(p)) for c, p in _ESCRITURAS]

# El ucraniano comparte alfabeto con el ruso: se distingue por letras propias.
_UCRANIANO_RE = re.compile(r"[ґєіїҐЄІЇ]")


# ── 2. Por las palabras que sostienen la frase ───────────────────────────
# Artículos, preposiciones, pronombres y auxiliares: las que aparecen sí o sí
# en cualquier frase real. No se usan palabras de contenido, que viajan de un
# idioma a otro (hotel, taxi, internet).
#
# Cada palabra vale según lo que DISTINGUE: si «un» está en cinco idiomas, ver
# «un» no dice casi nada; si «cuéntame» solo está en español, verla lo dice
# todo. El peso se calcula solo al cargar el módulo (1 / nº de idiomas que la
# tienen), así que añadir un idioma nuevo reajusta el resto sin tocar nada.
_PALABRAS = {
    "es": """el la los las un una de del que y es está estoy soy para con por
        pero como más muy qué cómo dónde cuándo porque también hola gracias
        quiero puedes tengo hacer bien cuéntame dime hazme aquí ahora mismo
        esto eso vale entonces oye venga chiste corto
        no me mi al ya hay has mas crees creo tienes tiene eres fue son todo
        mucho poco mejor después despues luego hoy mañana manana ayer siempre
        sin hasta donde cuando cual quien cuanto pues bueno otro otra ahí ahi
        allí alli yo él ella ellos usted quieres hablas voy sea ni buen
        cualquier puedo puede a""",
    "en": """the and is are you your for with that this have has what how where
        when because please thanks hello want can could would about there they
        from been being tell me joke short something anything""",
    "pt": """o os as um uma de do da dos das que e é está estou sou para com por
        mas como mais muito não também olá obrigado obrigada quero você então
        coisa agora aqui isso conta-me piada""",
    "fr": """le la les un une des de du que et est suis être pour avec par mais
        comme plus très qu'est comment où quand parce aussi bonjour merci je
        vous nous c'est s'il raconte blague maintenant ici""",
    "de": """der die das ein eine und ist sind nicht ich du sie wir für mit von
        auf aber wie was wo wann weil auch hallo danke bitte kann haben sein
        werden noch schon erzähl witz jetzt hier""",
    "it": """il lo la gli le un una di del che e è sono per con ma come più
        molto non anche ciao grazie voglio puoi perché quando dove adesso qui
        raccontami barzelletta mi al""",
    "ca": """el els la les un una de del que i és està sóc per amb però com més
        molt no també hola gràcies vull pots perquè quan on aquest aquesta ara
        això explica'm acudit curt fer tinc estic al""",
    # «todo» y «creo» también son gallego: si solo estuvieran en la lista
    # española, cualquier frase gallega que las usara sumaría al español.
    "gl": """o os a as un unha de do da que e é está son para con pero como máis
        moi non tamén ola grazas quero podes porque cando onde agora isto
        cóntame chiste todo creo""",
    "eu": """eta da dira dut duzu zure nire bat batzuk hau hori zer non noiz
        zergatik kaixo eskerrik mesedez nahi behar orain hemen""",
    "nl": """de het een en is zijn niet ik je jij we voor met van op maar hoe
        wat waar wanneer omdat ook hallo bedankt kan hebben nu hier vertel mop""",
    "pl": """i w na nie to jest są że się do dla jak co gdzie kiedy dlaczego
        cześć dziękuję proszę chcę mogę jestem teraz tutaj opowiedz żart""",
    "tr": """bir ve bu için ile ama nasıl ne nerede çünkü merhaba teşekkür
        lütfen istiyorum var yok değil çok şimdi burada anlat şaka kısa""",
}

_LISTAS = {c: set(t.split()) for c, t in _PALABRAS.items()}
# Cuántos idiomas comparten cada palabra → cuánto vale verla.
_CUANTOS = {}
for _c, _ps in _LISTAS.items():
    for _w in _ps:
        _CUANTOS[_w] = _CUANTOS.get(_w, 0) + 1
_PESOS = {c: {w: 1.0 / _CUANTOS[w] for w in ps} for c, ps in _LISTAS.items()}

# Letras que casi zanjan la cuestión entre parientes cercanos. Sólo las que el
# español NO usa: con la «ú» en la lista catalana, «busca quién ganó el último
# mundial» salía catalán, y la respuesta —bien escrita en español— se regeneró
# en catalán (26 sep 2026). «último», «según», «algún» la llevan a diario.
_PISTAS_LETRAS = {
    "es": "¿¡ñ", "pt": "ãõç", "fr": "çœàèùêâ", "de": "äöüß",
    "ca": "·ïçàèò", "pl": "ąćęłńóśźż", "tr": "ğışçöü",
}

# Cuánto peso hace falta para atreverse, y cuánta ventaja sobre el segundo.
# Con menos, se prefiere no saberlo: forzar el idioma equivocado es peor.
_MINIMO = 1.2
_VENTAJA = 1.4
# Lenguas tan parientes que dos palabras compartidas no bastan para saltar de
# una a otra a mitad de conversación, y lo que hace falta para hacerlo.
_PARIENTES = frozenset({"es", "pt", "gl", "ca", "it"})
_MINIMO_CAMBIO = 1.8

_PARTIR_RE = re.compile(r"[^\w'ñçàáâãäèéêëìíîïòóôõöùúûüýÿœ·]+", re.UNICODE)


def detectar(texto: str, previo: Optional[str] = None) -> Optional[str]:
    """El código del idioma del texto, o ``None`` si no está claro.

    Devolver ``None`` es una respuesta válida y frecuente: «vale», «7413» o
    «jajaja» no están en ningún idioma en particular, y quien los reciba debe
    seguir con el idioma que ya traía la conversación.

    ``previo`` es el idioma en que venía la conversación. Entre parientes
    cercanos (español, portugués, gallego, catalán, italiano) cambiar de
    idioma a mitad de charla exige más que un par de palabras compartidas:
    «Gracias creo que has mejorado bastante como IA te noto mas lista mas al
    tanto de todo» salía como portugués por dos «mas» sin tilde, y Celestia
    contestó «Obrigado!» (sesión 74, chat real).
    """
    t = (texto or "").strip()
    if len(t) < 2:
        return None

    # 1) La escritura manda: si hay kana, es japonés y no hay más que hablar.
    for codigo, patron in _ESCRITURAS_RE:
        if patron.search(t):
            if codigo == "ru" and _UCRANIANO_RE.search(t):
                return "uk"
            return codigo

    # 2) Palabras funcionales, cada una según lo que distinga.
    bajo = t.lower()
    palabras = [w for w in _PARTIR_RE.split(bajo) if w]
    puntos = {c: 0.0 for c in _LISTAS}
    for w in palabras:
        for codigo, pesos in _PESOS.items():
            if w in pesos:
                puntos[codigo] += pesos[w]
    # Las letras propias inclinan la balanza entre lenguas parientes.
    for codigo, letras in _PISTAS_LETRAS.items():
        if any(ch in bajo for ch in letras):
            puntos[codigo] = puntos.get(codigo, 0) + 1.0

    orden = sorted(puntos.items(), key=lambda kv: kv[1], reverse=True)
    if not orden or orden[0][1] < _MINIMO:
        return None
    mejor, tantos = orden[0]
    segundo = orden[1][1] if len(orden) > 1 else 0
    if segundo and tantos < segundo * _VENTAJA:
        return None          # dos candidatos empatados: mejor no elegir
    # Pasar a un pariente del idioma que ya traía la conversación pide pruebas
    # de verdad: sus letras propias («ã», «ç»…) o una ventaja clara. Si no, se
    # devuelve None y quien llama sigue con el idioma de antes.
    if (previo and mejor != previo and mejor in _PARIENTES
            and previo in _PARIENTES
            and not any(ch in bajo for ch in _PISTAS_LETRAS.get(mejor, ""))):
        if tantos < max(_MINIMO_CAMBIO, puntos.get(previo, 0.0) * 2.0):
            return None
    return mejor


def instruccion(codigo: str) -> str:
    """La orden que se le pega al mensaje para que conteste en ese idioma.

    Va en español porque el resto del prompt lo está, y nombra el idioma en su
    propia lengua además de en español: con modelos pequeños, «alemán (Deutsch)»
    acierta más veces que solo «alemán».
    """
    codigo = (codigo or "").lower()
    if not es_valido(codigo):
        return ""
    nativo, en_es = IDIOMAS[codigo]
    return (f"\n\n[IDIOMA: la persona escribe en {en_es} ({nativo}). "
            f"Responde TODA tu respuesta en {en_es}, sin mezclar otros idiomas.]")


def para_menu() -> List[Dict[str, str]]:
    """La lista para el selector de la interfaz."""
    return [{"codigo": c, "nombre": n, "es": e} for c, (n, e) in IDIOMAS.items()]
