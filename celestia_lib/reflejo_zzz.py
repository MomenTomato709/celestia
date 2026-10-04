"""El reflejo nativo de ZZZ: un binario dentro del móvil que mira el vídeo y pulsa.

Sesión 72. Enzo vio pelear a Celestia y lo dijo claro: «solo ataca y deja que
le golpeen sin parar… no hace timings con el cambio ofensivo para contraatacar
ni nada». El vigía de shell leía unos pocos píxeles con `od` a 4-6 vueltas por
segundo, y un destello enemigo dura 3-6 fotogramas y no cae en un sitio fijo.
Así que la reacción se mudó a C (`agente_movil/reflejo_zzz.c`): lee
`screenrecord --output-format=raw-frames` a 192x86 y escribe los toques
directos en el táctil.

Lo que sabe salió de una pelea que grabó Enzo con sus toques, y de cómo lo
explicó él:
  · destello ROJO   → esquivar («si llegas a cambiar en el destello rojo
                      recibes daño»)
  · destello DORADO → relevo, el cambio perfecto
  · anillo en el botón tras defender → salió perfecta → contraatacar ya

Aquí vive lo de fuera: compilarlo, subirlo, lanzarlo con sus topes, leer lo que
hizo y aprender cuánto esperar desde el destello hasta pulsar.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from celestia_lib.habilidades_zzz import pasos_validos
from celestia_lib.paths import MOVIL_DIR

logger = logging.getLogger(__name__)

RAIZ = Path(__file__).resolve().parent.parent
FUENTE = RAIZ / "agente_movil" / "reflejo_zzz.c"
# Se compila aquí (aarch64 estático, igual que el móvil) y se ejecuta allí: la
# carpeta del proyecto no deja ejecutar, pero sí guardar.
BINARIO = RAIZ / "agente_movil" / "bin" / "reflejo_zzz"

EN_SDCARD = f"{MOVIL_DIR}/reflejo_zzz"
# /sdcard es FUSE: no ejecuta, y escribir allí la nota línea a línea metería
# milisegundos en cada reacción. El binario y su nota van a ext4.
EN_MOVIL = "/data/local/tmp/.celestia_reflejo_zzz"
NOTA_MOVIL = "/data/local/tmp/.celestia_reflejo_zzz.nota"
NOTA = f"{MOVIL_DIR}/reflejo_zzz.nota"
VIVO = f"{MOVIL_DIR}/reflejo_zzz.vivo"
# Los fotogramas que vio, cuando se pide revisar una pelea (192x86, 3 bytes).
VOLCADO = f"{MOVIL_DIR}/reflejo_zzz.frames"
# El tamaño de vídeo con el que se midieron todas las firmas del binario.
ANCHO, ALTO = 192, 86


def compilar(fuente: Path = FUENTE, binario: Path = BINARIO, gcc: str = "gcc") -> Optional[Path]:
    """El binario al día con su fuente. `None` si no hay compilador o falla.

    🔴 11 sep (S73): «al día» se decidía por la FECHA, y el fuente reescrito con
    los combos conservó la de las 03:45 — más vieja que la del binario —, así que
    se habría subido al móvil el binario sin `--combos` y cada tramo habría
    acabado en «opción desconocida». Ahora manda el CONTENIDO: al lado del
    binario queda la huella del fuente con que se compiló.
    """
    try:
        huella = hashlib.sha256(fuente.read_bytes()).hexdigest()
        marca = binario.with_name(binario.name + ".fuente")
        if binario.exists() and marca.exists() and marca.read_text().strip() == huella:
            return binario
        binario.parent.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([gcc, "-static", "-O2", "-o", str(binario), str(fuente), "-lm"],
                           capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as e:
        logger.warning("reflejo: no pude compilar (%s)", e)
        return None
    if r.returncode != 0:
        logger.warning("reflejo: el compilador se quejó: %s", r.stderr[-500:])
        return None
    marca.write_text(huella)
    return binario


@dataclass
class Ajustes:
    """Lo que se le pasa al binario en un tramo."""
    espera_rojo: int = 350
    espera_dorado: int = 280
    dorado_con_relevo: bool = True
    ritmo: int = 140
    especial: int = 4
    definitiva: int = 0
    # MB de fotogramas que guardar para revisar la pelea después (0 = nada).
    volcar_mb: int = 0
    # Qué retrato se elige en el ataque en cadena: derecha, izquierda o alternar.
    cadena: str = "derecha"
    # Los combos del agente que está dentro (pasos de `habilidades_zzz`), por turno.
    # Con combos, `especial` y `definitiva` no se usan: la especial va en sus pasos
    # y la definitiva se suelta en cuanto está cargada si `auto_definitiva`.
    combos: List[str] = field(default_factory=list)
    auto_definitiva: bool = False
    # La rotación entera dentro del móvil: milisegundos de cada turno, en el
    # orden del relevo (turno i → combo i). Sin esto, la rotación la lleva
    # Python y cada cambio cuesta 2,27 s de mirar y arrancar (22 sep).
    turnos: List[int] = field(default_factory=list)
    # Ir a por el enemigo con el joystick cuando no se le ve (sesión 75). En
    # falso, el reflejo ataca donde esté mirando aunque no haya nadie delante:
    # es lo que pasó la noche del 16 de septiembre.
    buscar: bool = True
    # La especial sólo cuando el anillo dice que hay energía (sesión 75): sin
    # esto se pulsa cada `especial` toques haya o no haya, y sale una cada 0,57 s.
    especial_con_energia: bool = False
    # Intentar la EX aunque el aro del botón diga que no hay energía. Enzo, 19
    # sep: «Astra sí tiene energía, solamente se fijan en el botón que está gris
    # pero no en la energía que tiene bajo su barra de vida». Con el veto puesto,
    # Astra lanzó CERO EX en toda una partida siendo su jugada entera; sin él, si
    # de verdad no hay energía el juego ejecuta el especial normal y no se pierde
    # nada. Lo que salga se cuenta aparte (`ex_a_ciegas` en el resumen).
    ex_a_ciegas: bool = False

    def opciones(self) -> List[str]:
        o = ["--espera-rojo", str(int(self.espera_rojo)),
             "--espera-dorado", str(int(self.espera_dorado)),
             "--ritmo", str(int(self.ritmo)), "--especial", str(int(self.especial)),
             "--definitiva", str(int(self.definitiva)), "--cadena", self.cadena]
        if not self.dorado_con_relevo:
            o.append("--dorado-esquivar")
        if not self.buscar:
            o.append("--sin-buscar")
        if self.especial_con_energia:
            o.append("--especial-con-energia")
        if self.ex_a_ciegas:
            o.append("--ex-a-ciegas")
        if self.volcar_mb > 0:
            o += ["--volcar", f"{NOTA_MOVIL}.frames", "--volcar-mb", str(int(self.volcar_mb))]
        # Un combo mal escrito haría que el binario no arrancara (sale con 2) y el
        # tramo se perdería entero: se queda fuera, y los demás se juegan.
        validos = [c for c in self.combos if pasos_validos(c)][:12]
        if validos:
            o += ["--combos", "|".join(validos)]
            if self.auto_definitiva:
                o.append("--auto-definitiva")
            # Los turnos sólo valen con combos: cada turno juega el suyo. Y sólo
            # tantos como combos haya, que el binario empareja turno i ↔ combo i.
            turnos = [int(t) for t in self.turnos if int(t) >= 200][:len(validos)]
            if len(turnos) == len(validos) and turnos:
                o += ["--turnos", ",".join(str(t) for t in turnos)]
        return o


def guion(segundos: int, ajustes: Ajustes) -> str:
    """Lo que corre en el móvil: vídeo por una tubería de shell, binario, espera y limpieza.

    Sin `mkfifo`: SELinux se lo niega al shell en /data/local/tmp («Permission
    denied», medido el 11 sep), y sin tubería el binario ni arrancaba. Con
    `screenrecord … - | binario` el vídeo muere solo cuando el binario acaba
    (escribe en una tubería rota); si la pantalla no cambia no escribe, así que
    al final se le busca por sus argumentos con `ps|awk` por campos —`pkill -f`
    se encontraría a sí mismo—. Se sale del bucle cuando acaba el binario (sus
    topes), cuando se borra el `.vivo` (así se corta desde fuera) o por un tope
    de vueltas si todo lo demás falla.
    """
    # Entrecomillado: un combo lleva espacios y «|» («E a a a a:2500|d a»).
    opts = " ".join(shlex.quote(o) for o in ajustes.opciones())
    vueltas = (int(segundos) + 15) * 10 // 3
    return (
        f"B={EN_MOVIL}; N={NOTA_MOVIL}; V={VIVO}; "
        f"cmp -s {EN_SDCARD} $B 2>/dev/null || {{ cp {EN_SDCARD} $B && chmod 755 $B; }}; "
        f"rm -f $N $N.video {NOTA}; : > $V; "
        f"screenrecord --output-format=raw-frames --size {ANCHO}x{ALTO} - 2>$N.video "
        f"| $B {ANCHO} {ALTO} --segundos {int(segundos)} {opts} > $N 2>&1 & b=$!; "
        f"i=0; while kill -0 $b 2>/dev/null && [ -f $V ] && [ $i -lt {vueltas} ]; "
        f"do sleep 0.3; i=$((i+1)); done; "
        # `wait` a secas no: el agente del puente ejecuta con `.` en su propio
        # shell y esperaría también a sus otros hijos (se colgó 48 s el 11 sep).
        f"kill $b 2>/dev/null; wait $b 2>/dev/null; "
        f"for p in $(ps -A -o PID,ARGS | awk '$2==\"screenrecord\" && $NF==\"-\" "
        f"&& /--size {ANCHO}x{ALTO}/ {{print $1}}'); do kill $p 2>/dev/null; done; "
        f"sed 's/^/video: /' $N.video >> $N 2>/dev/null; "
        + (f"rm -f {VOLCADO}; cp $N.frames {VOLCADO} 2>/dev/null; rm -f $N.frames; "
           if ajustes.volcar_mb > 0 else "")
        + f"cp $N {NOTA} 2>/dev/null; rm -f $V $N.video"
    )


_DEFENSA_RE = re.compile(r"^(perfecta|fallida) \d+ (esquiva|asistencia) color=(\w+) retraso=(\d+)", re.M)
# Los contadores que van uno por combo, en el orden en que se mandaron.
_LISTAS = ("combos_hechos", "combos_cortados", "cadenas_combo")


def leer_nota(texto: str) -> Dict[str, Any]:
    """La nota del binario → contadores del resumen y cada defensa con su resultado."""
    texto = texto or ""
    fuera: Dict[str, Any] = {"resumen": {}, "defensas": [], "motivo": "",
                             "error_tactil": bool(re.search(r"^error_tactil", texto, re.M)),
                             # (ms, vida 0-1, fotogramas con barra) por segundo: el juez de los combos
                             "vida_enemigo": [(int(ms), float(v), int(n)) for ms, v, n in
                                              re.findall(r"^enemigo (\d+) vida=([\d.]+) vistos=(\d+)$", texto, re.M)]}
    fuera["turnos"] = [(int(ms), int(i)) for ms, i in
                       re.findall(r"^turno (\d+) (\d+)$", texto, re.M)]
    m = re.search(r"^resumen (.*)$", texto, re.M)
    if m:
        # «-1.00» es «no se vio»: sin el signo se leía «1.00», vida llena.
        for k, v in re.findall(r"(\w+)=(-?[\w.,]+)", m.group(1)):
            if k in _LISTAS:
                fuera["resumen"][k] = [int(x) for x in v.split(",") if x.isdigit()]
                continue
            try:
                fuera["resumen"][k] = int(v)
            except ValueError:
                try:
                    fuera["resumen"][k] = float(v)
                except ValueError:
                    fuera["resumen"][k] = v
        fuera["motivo"] = str(fuera["resumen"].get("motivo", ""))
    for resultado, tipo, color, retraso in _DEFENSA_RE.findall(texto):
        fuera["defensas"].append({"tipo": tipo, "color": color, "retraso": int(retraso),
                                  "perfecta": resultado == "perfecta"})
    return fuera


class EsperasAprendidas:
    """Cuánto esperar del destello a pulsar, por color, según lo que sale perfecto.

    Enzo acertó entre 211 y 632 ms desde que se ve la cruz (mediana 368), así
    que se prueban tres esperas en ese rango, empezando por la del medio. Cada
    una necesita unas cuantas defensas antes de compararla: con dos intentos,
    un 100 % es suerte. Con todas medidas se queda la que más perfectas da, y
    de vez en cuando vuelve a mirar la menos probada, por si el enemigo cambia.
    """

    CANDIDATAS = {"rojo": (250, 350, 450), "dorado": (180, 280, 380)}
    MIN_INTENTOS = 6
    CADA_CUANTO_EXPLORA = 5
    # Sesion 75, 18 sep: con las tres fijas medidas, el mejor quedaba en el BORDE
    # de la rejilla —rojo 250 ms 67 %, dorado 280 ms 46 %— asi que el optimo podia
    # estar en un valor que nunca se probaria. Cuando las tres estan medidas, se
    # abren vecinas a +-50 ms del mejor: es la misma idea que buscar rotaciones,
    # afinar alrededor de lo que ya funciona.
    PASO_FINO = 50
    LIMITES = {"rojo": (150, 550), "dorado": (120, 460)}

    def candidatas(self, color: str) -> tuple:
        """Las esperas a probar: las fijas y, si ya estan medidas, las vecinas del mejor."""
        fijas = self.CANDIDATAS[color]
        medidas = {e for e in fijas if self._cuenta(color, e)[1] >= self.MIN_INTENTOS}
        vistas = {int(e) for e in (self.cuentas.get(color) or {})}
        todas = set(fijas) | vistas
        if len(medidas) == len(fijas):
            mejor = max(todas, key=lambda e: (self.acierto(color, e), -abs(e - fijas[len(fijas) // 2])))
            bajo, alto = self.LIMITES.get(color, (100, 600))
            for vecina in (mejor - self.PASO_FINO, mejor + self.PASO_FINO):
                if bajo <= vecina <= alto:
                    todas.add(vecina)
        return tuple(sorted(todas))

    def __init__(self, ruta: Optional[Path] = None):
        self.ruta = ruta
        # color → espera (texto, por el JSON) → [perfectas, intentos]
        self.cuentas: Dict[str, Dict[str, List[int]]] = {}
        self._elecciones = 0
        if ruta is not None and ruta.exists():
            try:
                self.cuentas = json.loads(ruta.read_text("utf-8")).get("cuentas", {})
            except (OSError, ValueError) as e:
                logger.warning("reflejo: no pude leer las esperas aprendidas (%s)", e)

    def _cuenta(self, color: str, espera: int) -> List[int]:
        return self.cuentas.setdefault(color, {}).setdefault(str(int(espera)), [0, 0])

    def elegir(self, color: str) -> int:
        cands = self.candidatas(color)
        fijas = self.CANDIDATAS[color]
        medio = fijas[len(fijas) // 2]
        intentos = {e: self._cuenta(color, e)[1] for e in cands}
        # Las tres FIJAS se miden antes de nada: son el mapa grueso. Las vecinas
        # finas (±50 ms del mejor) se prueban en el turno de exploración, para no
        # dejar de aprovechar la que ya se sabe buena mientras se afina.
        pocas = [e for e in fijas if intentos.get(e, 0) < self.MIN_INTENTOS]
        if pocas:
            return min(pocas, key=lambda e: (intentos.get(e, 0), abs(e - medio)))
        self._elecciones += 1
        mejor = max(cands, key=lambda e: (self.acierto(color, e), -abs(e - medio)))
        if self._elecciones % self.CADA_CUANTO_EXPLORA == 0:
            otras = [e for e in cands if e != mejor]
            if otras:
                return min(otras, key=lambda e: (intentos.get(e, 0), abs(e - mejor)))
        return mejor

    def acierto(self, color: str, espera: int) -> float:
        p, n = self._cuenta(color, espera)
        return p / n if n else 0.0

    def apuntar(self, nota: Dict[str, Any], esperas: Dict[str, int]) -> None:
        """Cada defensa de la nota cuenta para la espera con la que se lanzó el tramo."""
        for d in nota.get("defensas", []):
            if d["color"] in esperas and d["color"] in self.CANDIDATAS:
                c = self._cuenta(d["color"], esperas[d["color"]])
                c[1] += 1
                c[0] += 1 if d["perfecta"] else 0

    def resumen(self) -> str:
        partes = []
        for color in self.CANDIDATAS:
            cands = self.candidatas(color)
            trozos = [f"{e} ms {self._cuenta(color, e)[0]}/{self._cuenta(color, e)[1]}" for e in cands]
            partes.append(f"{color}: " + ", ".join(trozos))
        return " · ".join(partes)

    def guardar(self) -> None:
        if self.ruta is None:
            return
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        self.ruta.write_text(json.dumps({"cuentas": self.cuentas}, ensure_ascii=False, indent=1), "utf-8")


class ReflejoNativo:
    """Lanza el binario en el móvil durante un tramo y devuelve su nota."""

    def __init__(self, mando: Any, compilar: Callable[[], Optional[Path]] = compilar,
                 copiar: Callable[[str, str], Any] = shutil.copyfile,
                 en_sdcard: str = EN_SDCARD, nota: str = NOTA, vivo: str = VIVO):
        self.mando = mando
        self._compilar = compilar
        self._copiar = copiar
        # Rutas vistas desde aquí (el PRoot ve /sdcard); los tests las cambian
        # para no tocar el móvil de verdad.
        self.en_sdcard, self.nota, self.vivo = en_sdcard, nota, vivo

    def preparar(self) -> bool:
        """Compila si hace falta y deja el binario en /sdcard si allí no está igual."""
        binario = self._compilar()
        if binario is None:
            return False
        try:
            nuevo = Path(binario).read_bytes()
            try:
                igual = Path(self.en_sdcard).read_bytes() == nuevo
            except OSError:
                igual = False
            if not igual:
                self._copiar(str(binario), self.en_sdcard)
        except OSError as e:
            logger.warning("reflejo: no pude subir el binario al móvil (%s)", e)
            return False
        return True

    def correr(self, segundos: int, ajustes: Ajustes) -> str:
        if not self.preparar():
            return "error: no hay binario del reflejo"
        try:
            os.unlink(self.nota)
        except OSError:
            pass
        # El tope de aquí va por encima del de allí: manda el del móvil, y este
        # sólo existe para no quedarse esperando a un muerto.
        self.mando._sh(guion(segundos, ajustes), timeout=segundos + 40)
        try:
            return Path(self.nota).read_text("utf-8", "replace")
        except OSError:
            return "no sé qué pasó: el reflejo no dejó nota"

    def parar(self) -> None:
        """Lo corta desde fuera, sin esperar a sus topes."""
        try:
            os.unlink(self.vivo)
        except OSError:
            pass
