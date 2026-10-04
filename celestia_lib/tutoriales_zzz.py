"""Aprender de tutoriales en vídeo: buscarlos, transcribirlos y sacar qué hacer.

Enzo, 17 sep 2026: «lo que tú dices de aprender midiendo en el juego en vez de
imitando grabaciones es porque yo no quiero jugar para enseñarle; pero si ella
busca vídeos, ve tutoriales y aprende qué hacer con cada equipo, combos y todo».
La distinción es suya y es justa: lo que se descartó fue pedirle a él que jugara
para que ella copiase. Un tutorial de internet no le cuesta nada a nadie.

Lo que se aprovecha de un vídeo **no son los fotogramas**: es lo que se dice. Un
modelo de visión describe escenas, no mide milisegundos, y lo que hace falta para
un combo son gestos y tiempos. Los tutoriales lo cuentan en voz alta y YouTube lo
transcribe: de ahí salen frases como «use Astra's EX Special, then swap to
Miyabi and hold basic attack», que es exactamente lo que
`habilidades_zzz.entradas_de()` ya sabe convertir en pasos (`E`, `a:2500`…).

Y lo importante: **lo que dice un vídeo es una propuesta, no un hecho**. Entra
como candidato y lo juzga el juego con el juez de la vida del enemigo
([[vida_enemigo_zzz]]). Un tutorial de hace un año puede estar hablando de una
versión que ya no existe.
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
from celestia_lib.paths import MEM_DIR

CARPETA = MEM_DIR / "zzz"
# Los subtítulos automáticos valen: lo que se busca son frases de juego, no prosa.
IDIOMAS = ("en", "en-US", "en-GB", "es", "es-ES")
TOPE_SEGUNDOS = 3600          # un vídeo de más de una hora no es un tutorial de combos
MAX_FRASES = 400


def _yt(*args: str, tiempo: int = 120) -> str:
    """yt-dlp con salida de texto. Cadena vacía si falla: sin red no se rompe nada."""
    try:
        r = subprocess.run([sys.executable, "-m", "yt_dlp", *args],
                           capture_output=True, text=True, timeout=tiempo)
        return r.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def buscar(consulta: str, cuantos: int = 5) -> List[Dict[str, Any]]:
    """Vídeos que responden a la consulta, los más vistos primero (lo que da YouTube)."""
    salida = _yt(f"ytsearch{int(cuantos)}:{consulta}", "--flat-playlist", "--dump-json",
                 "--no-warnings", tiempo=180)
    fuera = []
    for linea in salida.splitlines():
        try:
            d = json.loads(linea)
        except ValueError:
            continue
        dur = d.get("duration") or 0
        if dur and dur > TOPE_SEGUNDOS:
            continue
        fuera.append({"id": d.get("id", ""), "titulo": d.get("title", ""),
                      "canal": d.get("channel") or d.get("uploader") or "",
                      "segundos": int(dur or 0), "url": d.get("url") or
                      f"https://www.youtube.com/watch?v={d.get('id','')}"})
    return [v for v in fuera if v["id"]]


def _texto_de_vtt(vtt: str) -> List[str]:
    """Las frases de un .vtt, sin marcas, sin repetidas y en orden."""
    fuera: List[str] = []
    for linea in (vtt or "").splitlines():
        linea = linea.strip()
        if (not linea or "-->" in linea or linea.startswith(("WEBVTT", "Kind:", "Language:"))
                or linea.isdigit()):
            continue
        linea = re.sub(r"<[^>]+>", "", linea).strip()
        if linea and (not fuera or fuera[-1] != linea):
            fuera.append(linea)
        if len(fuera) >= MAX_FRASES:
            break
    return fuera


def transcripcion(video_id: str) -> List[str]:
    """Lo que se dice en el vídeo, frase a frase. Vacío si no hay subtítulos."""
    with tempfile.TemporaryDirectory() as d:
        _yt(f"https://www.youtube.com/watch?v={video_id}", "--skip-download",
            "--write-auto-sub", "--write-sub", "--sub-langs", ",".join(IDIOMAS),
            "--sub-format", "vtt", "-o", str(Path(d) / "sub"), "--no-warnings", tiempo=240)
        for f in sorted(Path(d).glob("*.vtt")):
            frases = _texto_de_vtt(f.read_text("utf-8", "replace"))
            if frases:
                return frases
    return []


# Frases que hablan de cómo se juega. Medido sobre tutoriales de ZZZ: lo útil
# lleva siempre un verbo de acción del juego o el nombre de una técnica.
_UTIL_RE = re.compile(
    r"(?i)\b(rotation|combo|opener|loop|swap|switch|cancel|chain attack|ultimate|"
    r"ex special|special attack|basic attack|dash|dodge|assist|stun|daze|anomaly|"
    r"disorder|hold|tap|press|after the|start with|then|first|second|third)\b")
# Y lo que NO es juego aunque lo parezca: el relleno de los vídeos.
_RUIDO_RE = re.compile(
    r"(?i)\b(subscribe|like the video|patreon|discord link|channel|comment below|"
    r"thanks for watching|sponsor)\b")


def unir_lineas(frases: Sequence[str], tope: int = 220) -> List[str]:
    """Rehace las frases de un subtítulo automático.

    YouTube transcribe en trozos de cinco a ocho palabras, cortando por la mitad
    («She can hold six of» / «them, and when you hold basic attack»), así que
    ninguna línea suelta contiene una secuencia entera y no se sacaba ni un combo.
    Se pega todo y se vuelve a partir por el punto, y si no lo hay, por longitud.
    """
    texto = " ".join(" ".join(str(f).split()) for f in frases)
    fuera: List[str] = []
    for trozo in re.split(r"(?<=[.!?])\s+", texto):
        trozo = trozo.strip()
        while len(trozo) > tope:
            corte = trozo.rfind(" ", 0, tope)
            corte = corte if corte > 40 else tope
            fuera.append(trozo[:corte].strip())
            trozo = trozo[corte:].strip()
        if trozo:
            fuera.append(trozo)
    return fuera


def frases_utiles(frases: Sequence[str], rehacer: bool = True) -> List[str]:
    """Las frases del tutorial que hablan de jugar, sin el relleno del canal."""
    fuera = []
    for f in (unir_lineas(frases) if rehacer else frases):
        f = " ".join(str(f).split())
        if len(f) < 12 or _RUIDO_RE.search(f) or not _UTIL_RE.search(f):
            continue
        if f not in fuera:
            fuera.append(f)
    return fuera


def combos_del_tutorial(frases: Sequence[str], agente: str = "") -> List[Dict[str, str]]:
    """Pasos que el reflejo sabe jugar, sacados de lo que dice el tutorial.

    Se reutiliza el traductor de las fichas escritas (`habilidades_zzz`): las
    mismas frases inglesas, el mismo vocabulario de gestos.
    """
    from celestia_lib.habilidades_zzz import entradas_de, pasos_validos
    fuera: List[Dict[str, str]] = []
    for frase in frases:
        pasos = _pasos_de_frase(frase, entradas_de)
        if pasos and pasos_validos(pasos) and len(pasos.split()) >= 2:
            fuera.append({"pasos": pasos, "porque": frase[:200], "agente": agente,
                          "fuente": "tutorial en vídeo"})
    return fuera


# Ojo con los plurales: `\battack\b` NO casa con «attacks» —el límite de palabra
# cae entre la k y la s— y por eso «three basic attacks» se perdía entera.
_GESTO_A_PASO = (
    (re.compile(r"(?i)\bex specials?\b|\bex\b"), "E"),
    (re.compile(r"(?i)\bultimates?\b|\bults?\b"), "U"),
    (re.compile(r"(?i)\bspecial attacks?\b|\bspecials?\b"), "e"),
    (re.compile(r"(?i)\bdodges?\b|\bdash(?:es)?\b"), "d"),
    (re.compile(r"(?i)\bbasic attacks?\b|\battacks?\b|\bhits?\b"), "a"),
)


def _pasos_de_frase(frase: str, entradas_de) -> str:
    """«use EX then three basic attacks and hold» → «E a a a a:2500»."""
    texto = " ".join(str(frase).split())
    pasos: List[str] = []
    # Se recorre la frase en orden: lo que se dice antes, se pulsa antes.
    for trozo in re.split(r"(?i)\b(?:then|and then|followed by|after that|next|,)\b", texto):
        trozo = trozo.strip()
        if not trozo:
            continue
        veces = 1
        m = re.search(r"(?i)\b(two|three|four|five|2|3|4|5)\s+(?:basic\s+)?(?:attacks?|hits?)\b", trozo)
        if m:
            veces = {"two": 2, "three": 3, "four": 4, "five": 5}.get(m.group(1).lower(),
                                                                    int(m.group(1)) if m.group(1).isdigit() else 1)
        sostenido = re.search(r"(?i)\bhold\b|\bcharge[d]?\b", trozo)
        # El «ataque de carrerilla» es esquiva + ataque, y lleva dentro la palabra
        # «dash»: sin esto, «dash attack» se leía como una esquiva a secas.
        if re.search(r"(?i)\b(?:dash|dodge)\s+attacks?\b", trozo):
            pasos += ["d", "a"]
            continue
        # Manda el orden EN LA FRASE, no el de la tabla: la primera técnica que se
        # nombra es la que se pulsa.
        visto = [(m.start(), paso) for patron, paso in _GESTO_A_PASO
                 for m in [patron.search(trozo)] if m]
        if visto:
            _, paso = min(visto)
            for _ in range(veces):
                pasos.append(f"{paso}:2500" if sostenido and paso in "ae" else paso)
    return " ".join(pasos[:12])


# Lo que de verdad dan los tutoriales hablados, medido sobre transcripciones
# reales (17 sep): NO dictan botones —«pulsa esto, luego esto» casi no se dice—,
# explican MECÁNICAS («Miyabi empieza con tres Fallen Frost», «su habilidad da dos
# cuando el equipo provoca disorder») y ORDEN DE EQUIPO («empieza con Astra, luego
# Miyabi»). Los pasos de un combo siguen saliendo de las fichas escritas, que los
# traen literales; de aquí sale el saber y a quién meter primero.
_MECANICA_RE = re.compile(
    r"(?i)\b(anomaly|disorder|daze|stun|energy|decibel|fallen frost|stacks?|gauge|"
    r"buff|debuff|attribute|shred|crit|rotation|loop|opener|uptime|window)\b")


def mecanicas_del_tutorial(frases: Sequence[str]) -> List[str]:
    """Las frases que explican cómo funciona el personaje o el equipo."""
    fuera = []
    for f in frases:
        if _MECANICA_RE.search(f) and 30 <= len(f) <= 300 and f not in fuera:
            fuera.append(f)
    return fuera


def _nombres_parecidos(frase: str, cortos: Dict[str, List[str]], corte: float = 0.8):
    """(posición, nombre) de cada agente que se nombra, aunque venga mal escrito."""
    from difflib import SequenceMatcher
    for m in re.finditer(r"[A-Za-z]{3,}", frase):
        palabra = m.group(0).lower()
        mejor_n, mejor_r = "", 0.0
        for nombre, trozos in cortos.items():
            for corto in trozos:
                r = SequenceMatcher(None, palabra, corto).ratio()
                if r > mejor_r:
                    mejor_n, mejor_r = nombre, r
        if mejor_r >= corte:
            yield m.start(), mejor_n


def rotacion_del_tutorial(frases: Sequence[str], nombres: Sequence[str]) -> List[Dict[str, Any]]:
    """Órdenes de equipo propuestos por el vídeo: «empieza con Astra, luego Miyabi».

    Se buscan dos o más nombres del equipo en la misma frase y se devuelve el
    orden en que se nombran, con la frase como prueba. Es una PROPUESTA: quién
    gana se decide midiendo el daño en el juego.
    """
    # Los subtítulos automáticos destrozan los nombres propios: en un tutorial real
    # de este equipo salían «miabi» por Miyabi y «Ani» por Yanagi. Buscarlos
    # literalmente no casa nunca, así que se buscan por parecido.
    # Cualquier parte del nombre vale: de «Astra Yao» la gente dice Astra, y de
    # «Tsukishiro Yanagi», Yanagi. Coger sólo el último apellido perdía a Astra.
    cortos = {n: [p.lower() for p in n.split() if len(p) > 2] for n in nombres if n}
    fuera: List[Dict[str, Any]] = []
    for f in frases:
        visto = sorted(_nombres_parecidos(f, cortos))
        if len(visto) >= 2:
            orden = [n for _, n in visto]
            if not any(d["orden"] == orden for d in fuera):
                fuera.append({"orden": orden, "porque": f[:200]})
    return fuera


def guardar(agente_o_equipo: str, video: Dict[str, Any], frases: Sequence[str],
            combos: Sequence[Dict[str, str]], carpeta: Path = CARPETA) -> Path:
    """Lo aprendido de un vídeo, con su fuente, para poder desconfiar luego."""
    carpeta.mkdir(parents=True, exist_ok=True)
    nombre = re.sub(r"[^a-z0-9]+", "-", str(agente_o_equipo).lower()).strip("-") or "zzz"
    ruta = carpeta / f"tutorial_{nombre}.json"
    datos = {"de": agente_o_equipo, "video": video, "frases": list(frases)[:120],
             "mecanicas": mecanicas_del_tutorial(frases)[:40],
             "combos": list(combos), "sin_probar": True,
             "aviso": "lo que dice un vídeo es una propuesta: lo juzga el juego"}
    ruta.write_text(json.dumps(datos, ensure_ascii=False, indent=1), "utf-8")
    return ruta

def nombres_en(texto: str, nombres: Sequence[str], corte: float = 0.94) -> List[str]:
    """Cuáles del equipo se nombran en un título (tolerando cómo los escriban).

    Enzo, 17 sep: «que busque gameplay de Miyabi Astra Yao Yanagi». El vídeo más
    legible que salió antes era de «Miyabi with Piper»: se eligió por calidad de
    imagen y acabamos mirando la rotación de un equipo que Enzo no tiene. La
    legibilidad no sabe de equipos, así que hay que exigir el equipo aparte.
    """
    cortos = {n: [p.lower() for p in n.split() if len(p) > 2] for n in nombres if n}
    # El corte por defecto es MUCHO más duro que el de los subtítulos: en un título
    # los nombres se escriben bien, y con el corte laxo «Lycaon» pasaba por
    # «Yanagi» y se eligió un gameplay de otro equipo (17 sep).
    vistos = {n for _pos, n in _nombres_parecidos(texto, cortos, corte=corte)}
    return [n for n in nombres if n in vistos]
