"""La obra: Celestia dirige a un programador y sólo acepta trabajo comprobado.

Enzo (24 sep 2026): «que pueda programar sola y bien, más que un senior, es la
punta del iceberg: con esto puede hacer lo demás sin ti».

El programador es Aider con DeepSeek. Aider solo ya hizo la web de una
peluquería entera (7/7 tareas, 66 tests en verde, 0,09 $), pero entregó dos
cosas que ningún test vio: la portada pública enseñaba el teléfono de todos
los clientes, y en git entraron ficheros basura con nombres sacados de los
bloques de código del README («python app.py», «}»). La calidad no sale del
modelo sino del método, así que aquí Celestia hace de jefa de obra:

1. Aider trabaja SIN hacer commits. Lo que entra en git lo decide la obra.
2. Tras cada intento: fuera la basura, y si ha tocado algo protegido, la tarea
   se deshace entera (sin segunda oportunidad: eso no es un despiste).
3. Los tests los pasa la obra, no Aider: en verde y sin bajar de los que
   había (borrar un test que falla no cuenta como arreglarlo).
4. Un revisor lee el diff buscando lo que los tests no ven: datos personales a
   la vista, agujeros de seguridad, partes del encargo sin hacer.
5. Lo que falla vuelve a Aider con el motivo. Si al final no queda bien, la
   tarea se deshace y la obra se para: no se construye encima de algo roto.

El dinero va por el mismo presupuesto que el chat (gasto.py): cada tarea
reserva su tope antes de empezar, el gasto se cuenta EN VIVO con los tokens
que imprime Aider y se le corta si se acerca al tope. Al final se contrasta
con el saldo real de DeepSeek por si Aider hizo llamadas que no imprimió.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import shlex
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from .gasto import PRECIOS_DEEPSEEK, PresupuestoMensual, coste_uso

logger = logging.getLogger("celestia_v1")

AIDER = os.environ.get("CELESTIA_AIDER", str(Path.home() / ".local" / "bin" / "aider"))
URL_DEEPSEEK = "https://api.deepseek.com"

# Lo que se le repite a Aider en cada tarea. Cada regla sale de un fallo visto,
# no de un manual. La de Markdown: Aider manda cada fichero dentro de un bloque
# de ``` y los ``` del propio README le cortan el fichero y convierten cada
# ejemplo en un fichero basura («python app.py», «node --test»; 24 sep).
REGLAS = """Reglas de la obra (valen para toda tarea):
- Cada cambio de comportamiento lleva sus tests. No borres ni debilites tests que ya existen.
- Ninguna página o ruta pública enseña datos personales de otras personas (teléfonos, correos, nombres de clientes, citas ajenas). Eso va sólo detrás del login, y un test lo comprueba.
- Nada de secretos escritos en el código: claves y contraseñas por variables de entorno.
- Crea ficheros sólo con nombres de fichero normales, nunca con comandos o trozos de código como nombre.
- Lo que vea una persona tiene que verse cuidado: HTML semántico y CSS sencillo y agradable que se lea bien en el móvil.
- En ficheros Markdown (README y demás) NO uses bloques de código con tres comillas invertidas: los comandos y ejemplos van con sangría de 4 espacios.
- Si la tarea se contradice o es imposible tal cual, dilo en UNA línea, elige la interpretación más razonable y sigue: no le des vueltas."""

# Tope de salida por respuesta de Aider. DeepSeek razona antes de escribir y el
# razonamiento cuenta como salida: con 8192 (24 sep) se lo gastó entero
# pensando y no llegó a escribir el fichero. 32768 × 1,20 $/M ≈ 0,04 $.
MAX_SALIDA_AIDER = 32768


def margen_respuesta(modelo: str) -> float:
    """Lo más que puede costar UNA respuesta más de Aider: se corta con este
    margen antes del tope porque la línea de tokens llega ya cobrada."""
    from .gasto import PRECIOS_DEEPSEEK
    p = PRECIOS_DEEPSEEK.get(modelo) or max(PRECIOS_DEEPSEEK.values(), key=lambda x: x["salida"])
    # Salida entera + ~60K tokens de entrada sin caché.
    return (MAX_SALIDA_AIDER * p["salida"] + 60_000 * p["entrada"]) / 1e6

IGNORAR_SIEMPRE = (".aider*", "__pycache__/", "*.pyc", ".pytest_cache/")

# Siempre protegido, lo pida quien lo pida.
PROTEGIDOS_SIEMPRE = (".git/*", ".env", ".env.*")

# Un nombre de fichero normal: letras, números, _ . - y nada que parezca un
# comando o un trozo de código (espacios, comillas, llaves, dos puntos…).
_TROZO_SANO = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,99}$")

# «Tokens: 27k sent, 512 cache hit, 6.5k received.» (también «cache write»).
_LINEA_TOKENS = re.compile(r"^Tokens:\s*(.+?)\.?\s*$")
_PAR_TOKENS = re.compile(r"([\d.]+)\s*([kKmM]?)\s+(sent|cache hit|cache write|received)")


# Lo que escribe el modelo se ejecuta (los tests): que no vea ninguna clave.
_SECRETO = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CLAVE|CREDENTIAL|AUTH", re.I)


def entorno_sin_secretos() -> dict:
    return {k: v for k, v in os.environ.items() if not _SECRETO.search(k)}


_LINEA_DE_FALLO = re.compile(r"FALLA|FAIL|not ok|[Ee]rror|assert|Traceback|failed|✖|línea \d+:")


def resumen_salida(salida: str, max_fallos: int = 80, cola: int = 25) -> str:
    """Lo que se le devuelve a Aider cuando los tests no pasan: PRIMERO las
    líneas de fallo, estén donde estén, y luego el final de la salida.

    Antes eran las últimas 60 líneas: con 116 tests en verde, eran casi todas
    «ok» y las de FALLA quedaban fuera. Aider lo dijo él mismo («me falta la
    salida completa») y arregló a ciegas tres veces (24 sep 2026)."""
    lineas = salida.strip().splitlines()
    fallos = [l for l in lineas if _LINEA_DE_FALLO.search(l)]
    if not fallos:
        return "\n".join(lineas[-60:])
    return ("Líneas de fallo:\n" + "\n".join(fallos[:max_fallos])
            + "\n\nFinal de la salida:\n" + "\n".join(lineas[-cola:]))


def contar_tests(salida: str) -> Optional[int]:
    """Tests que pasan según la salida de pytest, unittest o `node --test`;
    None si no se reconoce ninguno."""
    pasan = re.findall(r"(\d+) passed", salida)
    if pasan:
        return int(pasan[-1])
    if "no tests ran" in salida:
        return 0
    nodo = re.findall(r"^[#ℹ] pass (\d+)", salida, re.M)
    if nodo:
        return int(nodo[-1])
    corridos = re.findall(r"^Ran (\d+) tests?", salida, re.M)
    if corridos:
        m = re.search(r"FAILED \((.*?)\)", salida)
        malos = sum(int(x) for x in re.findall(r"(?:failures|errors)=(\d+)", m.group(1))) if m else 0
        return int(corridos[-1]) - malos
    return None


def nombre_sano(ruta: str) -> bool:
    return all(_TROZO_SANO.match(trozo) for trozo in ruta.split("/"))


def _numero(valor: str, sufijo: str) -> float:
    n = float(valor)
    return n * {"k": 1e3, "m": 1e6}.get(sufijo.lower(), 1.0)


def coste_linea_tokens(linea: str, modelo: str) -> float:
    """Dólares de una línea «Tokens: …» de Aider; 0 si no lo es.

    Aider redondea («27k» puede ser 27.499): se cuenta medio escalón de más,
    y la caché se cobra aparte de lo enviado, que la incluye.
    """
    m = _LINEA_TOKENS.match(linea.strip())
    if not m:
        return 0.0
    cuenta = {"sent": 0.0, "cache hit": 0.0, "cache write": 0.0, "received": 0.0}
    for valor, sufijo, clase in _PAR_TOKENS.findall(m.group(1)):
        margen = {"k": 50.0, "m": 50_000.0}.get(sufijo.lower(), 0.0)
        cuenta[clase] += _numero(valor, sufijo) + margen
    uso = {
        "prompt_tokens": int(cuenta["sent"]),
        "prompt_cache_hit_tokens": int(min(cuenta["cache hit"], cuenta["sent"])),
        "completion_tokens": int(cuenta["received"]),
    }
    return coste_uso(uso, modelo)


def _git(carpeta: Path, *args: str, check: bool = True) -> str:
    r = subprocess.run(["git", "-C", str(carpeta), *args], capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def _cambios(carpeta: Path) -> List[Tuple[str, str]]:
    """(estado, ruta) de todo lo que difiere de HEAD, sin agrupar carpetas.
    Los ficheros de Aider (.aider*) no cuentan: son su diario, no la obra."""
    crudo = _git(carpeta, "status", "--porcelain=v1", "-z", "-uall")
    trozos = crudo.split("\0")
    salida: List[Tuple[str, str]] = []
    i = 0
    while i < len(trozos):
        entrada = trozos[i]
        i += 1
        if len(entrada) < 4:
            continue
        estado, ruta = entrada[:2], entrada[3:]
        if "R" in estado or "C" in estado:
            # Renombrado: detrás viene la ruta de origen, que también cuenta.
            if i < len(trozos) and trozos[i]:
                salida.append((estado, trozos[i]))
            i += 1
        if not ruta.split("/")[0].startswith(".aider"):
            salida.append((estado, ruta))
    return salida


@dataclass
class Resultado:
    n: int
    tarea: str
    aceptada: bool = False
    motivo: str = ""
    intentos: int = 0
    tests_antes: Optional[int] = None
    tests_despues: Optional[int] = None
    basura: List[str] = field(default_factory=list)
    graves: List[str] = field(default_factory=list)
    revisada: bool = False
    coste: float = 0.0
    segundos: float = 0.0
    commit: str = ""
    gasto_ciego: bool = False


class Obra:
    """Lleva un proyecto tarea a tarea. `revisor` y `presupuesto` se pueden
    cambiar (tests); por defecto son DeepSeek y el contador de la Celestia real."""

    def __init__(
        self,
        carpeta: Path,
        *,
        test_cmd: str = "python3 -m pytest -q",
        modelo: str = "deepseek-flash",
        presupuesto: Optional[PresupuestoMensual] = None,
        clave: Optional[str] = None,
        tope_tarea: float = 0.20,
        reintentos: int = 2,
        protegidos: Sequence[str] = (),
        revisor: Optional[Callable[[str, str], Optional[List[str]]]] = None,
        archivos: Optional[Sequence[str]] = None,
        leer: Sequence[str] = (),
        adelanto: float = 0.0,
        esfuerzo: str = "none",
        formato: str = "diff",
        escalar: bool = True,
        timeout_aider_s: float = 900.0,
        timeout_tests_s: float = 600.0,
        aviso: Callable[[str], None] = lambda texto: logger.info("Obra: %s", texto),
    ):
        self.carpeta = Path(carpeta).resolve()
        self.test_cmd = test_cmd
        self.modelo = modelo
        self.clave = clave if clave is not None else os.environ.get("DEEPSEEK_API_KEY", "")
        self.presupuesto = presupuesto if presupuesto is not None else _presupuesto_real()
        self.tope_tarea = float(tope_tarea)
        self.reintentos = max(int(reintentos), 0)
        self.protegidos = tuple(PROTEGIDOS_SIEMPRE) + tuple(protegidos)
        self.revisor = revisor or self._revisar_con_deepseek
        self.timeout_aider_s = timeout_aider_s
        self.timeout_tests_s = timeout_tests_s
        self.aviso = aviso
        # Qué tiene Aider delante. None = todo el código del proyecto (vale
        # para uno pequeño); en uno grande como Celestia se le da la lista:
        # cada fichero viaja en cada petición y se paga.
        self.archivos = list(archivos) if archivos is not None else None
        self.leer = list(leer)
        self.adelanto = float(adelanto)
        # Cuánto razona Aider antes de escribir. Por defecto DeepSeek piensa
        # tanto que se comía los 32K de salida sin escribir el fichero (24 sep,
        # tarea del túnel), y con "low" siguió pasando. "none" = sin razonar
        # (medido: 0 tokens de razonamiento). La calidad la vigilan los tests
        # y el revisor, que sí razona a fondo.
        self.esfuerzo = esfuerzo
        # Cómo manda Aider los cambios. Con "whole" reescribía cada fichero
        # entero en cada respuesta y, con un fichero de tests de cientos de
        # líneas, no cabía en los 32K (24 sep, tarea de los hilos de la
        # puerta). "diff" = sólo bloques de buscar y reemplazar.
        self.formato = formato
        # Si tras los reintentos sigue sin salir, un último intento razonando a
        # fondo. Sin razonar, Aider es rápido y barato pero se atasca en lo que
        # pide pensar: la puerta con hilos, o un test de probabilidades que no
        # tenía en cuenta la protección de rachas (24 sep 2026).
        self.escalar = escalar
        self.saldo = saldo_deepseek
        self.cada_saldo_s = 60.0
        # Lo que se ha contado de verdad: aider + revisiones, en dólares.
        self.contado = 0.0

    # ── Preparar el terreno ────────────────────────────────────────────
    def _preparar_repo(self) -> None:
        self.carpeta.mkdir(parents=True, exist_ok=True)
        if not (self.carpeta / ".git").exists():
            _git(self.carpeta, "init", "-q")
        hay_historia = bool(_git(self.carpeta, "rev-parse", "--verify", "-q", "HEAD", check=False).strip())
        if hay_historia and _cambios(self.carpeta):
            # La obra no mezcla su trabajo con cambios a medias de otro.
            raise RuntimeError("hay cambios sin guardar en la carpeta: guárdalos o descártalos antes")
        # El diario de Aider y lo que dejan los tests al pasar no son la obra:
        # sin esto, `git add -A` guardaba los .pyc de pytest.
        ignorar = self.carpeta / ".gitignore"
        actual = ignorar.read_text(encoding="utf-8") if ignorar.exists() else ""
        faltan = [x for x in IGNORAR_SIEMPRE if x not in actual.split("\n")]
        if faltan:
            ignorar.write_text(actual + ("\n" if actual and not actual.endswith("\n") else "")
                               + "\n".join(faltan) + "\n", encoding="utf-8")
        if faltan or not hay_historia:
            _git(self.carpeta, "add", "-A")
            self._commit("Inicio de la obra" if not hay_historia
                         else "La obra ignora el diario de Aider y las cachés", vacio=True)

    def _commit(self, mensaje: str, vacio: bool = False) -> str:
        nombre = _git(self.carpeta, "config", "user.name", check=False).strip() or "Celestia"
        correo = _git(self.carpeta, "config", "user.email", check=False).strip() or "celestia@localhost"
        args = ["-c", f"user.name={nombre}", "-c", f"user.email={correo}", "commit", "-q", "-m", mensaje]
        if vacio:
            args.append("--allow-empty")
        _git(self.carpeta, *args)
        return _git(self.carpeta, "rev-parse", "--short", "HEAD").strip()

    def _deshacer(self, base: str) -> None:
        _git(self.carpeta, "reset", "-q", "--hard", base)
        # -fd sin -x: lo ignorado (.aider*, cachés) se queda.
        _git(self.carpeta, "clean", "-q", "-fd")

    # ── Los tests, pasados por la obra ─────────────────────────────────
    def pasar_tests(self) -> Tuple[bool, Optional[int], str]:
        """(en verde, tests que pasan o None si no se sabe contar, cola de la salida)."""
        try:
            r = subprocess.run(shlex.split(self.test_cmd), cwd=self.carpeta, capture_output=True,
                               text=True, timeout=self.timeout_tests_s, env=entorno_sin_secretos())
        except subprocess.TimeoutExpired:
            return False, None, f"los tests no acabaron en {self.timeout_tests_s:.0f} s"
        except OSError as e:
            return False, None, f"no se pudieron lanzar los tests: {e}"
        salida = (r.stdout or "") + (r.stderr or "")
        cola = resumen_salida(salida)
        n = contar_tests(salida)
        return r.returncode == 0, n, cola

    # ── Aider ──────────────────────────────────────────────────────────
    def _ficheros_para_aider(self) -> List[str]:
        """Lo que Aider tiene delante: el código del proyecto (no binarios ni
        ficheros enormes, que se comen el contexto y el dinero)."""
        if self.archivos is not None:
            # Los que aún no existan también: Aider los crea.
            return [r for r in self.archivos if nombre_sano(r)]
        salida = []
        for ruta in _git(self.carpeta, "ls-files").splitlines():
            p = self.carpeta / ruta
            if (p.suffix in (".py", ".md", ".txt", ".html", ".css", ".js", ".json", ".toml", ".cfg", ".ini")
                    and p.is_file() and p.stat().st_size < 200_000 and nombre_sano(ruta)):
                salida.append(ruta)
        return salida

    def _lanzar_aider(self, mensaje: str, tope: float) -> Tuple[float, str]:
        """Un turno de Aider. Devuelve (coste contado, motivo si se le cortó)."""
        entorno = entorno_sin_secretos()
        entorno.update({"OPENAI_API_KEY": self.clave, "OPENAI_API_BASE": URL_DEEPSEEK})
        # Sin esto Aider no manda `max_tokens` y DeepSeek puede escribir
        # 393.216 tokens en UNA respuesta (≈0,47 $): el corte llegaría tarde.
        ajustes = self.carpeta / ".aider.obra.ajustes.yml"
        # Va en extra_body: litellm tira `reasoning_effort` sin avisar (visto
        # con una API falsa que guardaba lo que llegaba), extra_body pasa tal cual.
        if self.esfuerzo == "none":
            razonar = "    extra_body:\n      thinking:\n        type: disabled\n"
        elif self.esfuerzo:
            razonar = f"    extra_body:\n      reasoning_effort: {self.esfuerzo}\n"
        else:
            razonar = ""
        ajustes.write_text(f"- name: openai/{self.modelo}\n  extra_params:\n"
                           f"    max_tokens: {MAX_SALIDA_AIDER}\n" + razonar, encoding="utf-8")
        orden = [
            AIDER, "--model", f"openai/{self.modelo}", "--model-settings-file", str(ajustes),
            *(["--edit-format", self.formato] if self.formato else []),
            "--no-auto-commits", "--no-dirty-commits",
            # Ni comandos de shell sugeridos por el modelo ni visitas a URLs:
            # con --yes-always se ejecutarían sin que nadie los mire.
            "--no-suggest-shell-commands", "--no-detect-urls",
            "--yes-always", "--no-check-update", "--no-analytics", "--no-show-model-warnings",
            "--no-pretty", "--no-stream", "--no-gitignore",
            # Sin --auto-test: los tests los pasa la obra, y los de Aider se
            # ejecutarían con la clave de DeepSeek en el entorno.
            *[x for ruta in self.leer for x in ("--read", ruta)],
            "--message", mensaje, *self._ficheros_para_aider(),
        ]
        coste = 0.0
        motivo = ""
        diario = self.carpeta / ".aider.obra.log"
        proc = subprocess.Popen(orden, cwd=self.carpeta, env=entorno, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True,
                                errors="replace", start_new_session=True)

        por_tiempo = threading.Event()

        def matar():
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                time.sleep(3)
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        reloj = threading.Timer(self.timeout_aider_s, lambda: (por_tiempo.set(), matar()))
        reloj.daemon = True
        reloj.start()
        # Freno que no depende de lo que imprima Aider: el saldo de verdad.
        # Va en céntimos y con retraso, así que es la red, no el corte fino.
        por_saldo = threading.Event()
        acabado = threading.Event()
        saldo0 = self.saldo(self.clave)

        def vigilar_saldo():
            while saldo0 is not None and not acabado.wait(self.cada_saldo_s):
                ahora = self.saldo(self.clave)
                if ahora is not None and saldo0 - ahora >= tope:
                    por_saldo.set()
                    matar()
                    return

        vigia = threading.Thread(target=vigilar_saldo, daemon=True)
        vigia.start()
        try:
            with open(diario, "a", encoding="utf-8") as log:
                assert proc.stdout is not None
                for linea in proc.stdout:
                    log.write(linea)
                    coste += coste_linea_tokens(linea, self.modelo)
                    # Una respuesta más puede costar unos céntimos: se corta
                    # con margen para no pasarse de lo reservado.
                    if not motivo and coste > tope - margen_respuesta(self.modelo):
                        motivo = f"se acercaba al tope de la tarea ({coste:.3f} $)"
                        matar()
            proc.wait()
        finally:
            reloj.cancel()
            acabado.set()
            if proc.stdout is not None:
                proc.stdout.close()
        if por_saldo.is_set():
            motivo = "el saldo real de DeepSeek bajó más que el tope de la tarea"
            coste = max(coste, tope)
        elif not motivo and por_tiempo.is_set():
            motivo = f"Aider no acabó en {self.timeout_aider_s:.0f} s"
        return coste, motivo

    # ── El revisor ─────────────────────────────────────────────────────
    def _revisar_con_deepseek(self, tarea: str, diff: str) -> Optional[List[str]]:
        """Problemas graves del diff, [] si no hay, None si no se pudo revisar."""
        if not self.clave:
            return None
        sistema = (
            "Eres un revisor de código senior y exigente. Revisas el diff de UNA tarea. "
            "Informa SÓLO de problemas graves: datos personales visibles para quien no debe "
            "verlos (rutas o páginas públicas que enseñan teléfonos, correos, citas o nombres "
            "ajenos), agujeros de seguridad (inyección SQL, XSS, rutas privadas sin proteger, "
            "contraseñas sin hash, secretos en el código) y partes de la tarea que no están "
            "hechas. Sólo ves el DIFF de esta tarea: el resto del proyecto ya existe y funciona, "
            "así que NUNCA digas que algo falta o no existe en un fichero que el diff no muestra "
            "(24 sep: se deshizo una tarea buena por un «falta ClaimCode» que ya estaba). "
            "Nada de estilo ni de gustos. Cada problema en una frase concreta que diga "
            "dónde está. Contesta sólo JSON: {\"graves\": [\"...\"]} (lista vacía si no hay)."
        )
        usuario = f"TAREA:\n{tarea}\n\nDIFF:\n{diff}"
        # Razona antes de contestar y el razonamiento cuenta como salida: con
        # 6000 se los gastó todos pensando y no llegó a contestar (24 sep).
        max_tokens = MAX_SALIDA_AIDER
        payload = json.dumps({
            "model": self.modelo,
            "messages": [{"role": "system", "content": sistema}, {"role": "user", "content": usuario}],
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "response_format": {"type": "json_object"},
        }).encode()
        # No `coste_maximo`: ése acota la salida a 8192 (lo del chat) y aquí
        # se piden más; la reserva tiene que cubrir lo que de verdad se pide.
        precio = PRECIOS_DEEPSEEK.get(self.modelo) or max(PRECIOS_DEEPSEEK.values(),
                                                           key=lambda x: x["salida"])
        maximo = (len(payload) * precio["entrada"] + max_tokens * precio["salida"]) / 1e6
        if not self.presupuesto.reservar(maximo, self.adelanto):
            logger.info("Obra: sin cupo para revisar (%.4f $)", maximo)
            return None
        req = urllib.request.Request(
            f"{URL_DEEPSEEK}/chat/completions", data=payload, method="POST",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.clave}"})
        try:
            with urllib.request.urlopen(req, timeout=240) as resp:
                datos = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            self.presupuesto.liquidar(maximo, 0.0)
            logger.warning("Obra: el revisor contestó %s", e.code)
            return None
        except Exception as e:
            # Corte sin saber qué se cobró: se queda la reserva entera.
            self.contado += maximo
            logger.warning("Obra: revisión cortada (%s)", e)
            return None
        real = coste_uso(datos.get("usage") or {}, self.modelo)
        self.presupuesto.liquidar(maximo, real)
        self.contado += real
        eleccion = (datos.get("choices") or [{}])[0]
        texto = (eleccion.get("message") or {}).get("content") or ""
        if eleccion.get("finish_reason") == "length":
            logger.warning("Obra: el revisor se quedó sin tokens de salida — revisión perdida")
        m = re.search(r"\{.*\}", texto, re.S)
        try:
            graves = json.loads(m.group(0)).get("graves") if m else None
        except ValueError:
            graves = None
        if not isinstance(graves, list):
            return None
        return [str(g).strip() for g in graves if str(g).strip()]

    def _diff_de_la_tarea(self, base: str, limite: int = 60_000) -> str:
        # intent-to-add: que el diff enseñe también los ficheros nuevos.
        _git(self.carpeta, "add", "-A", "-N")
        diff = _git(self.carpeta, "diff", "-U6", base)
        if len(diff) > limite:
            diff = diff[:limite] + "\n[… diff recortado …]"
        return diff

    # ── Una tarea ──────────────────────────────────────────────────────
    def _tocados_protegidos(self, cambios: List[Tuple[str, str]]) -> List[str]:
        """Lo protegido que se ha tocado. Un enlace simbólico cuenta como
        protegido siempre: por él se escribe en otro sitio que git no ve."""
        tocados = set()
        for _, ruta in cambios:
            p = self.carpeta / ruta
            if ruta == ".git" or any(fnmatch.fnmatch(ruta, pat) for pat in self.protegidos):
                tocados.add(ruta)
            elif p.is_symlink() or any(q.is_symlink() for q in p.parents
                                       if q != self.carpeta and self.carpeta in q.parents):
                tocados.add(f"{ruta} (enlace simbólico)")
        return sorted(tocados)

    def _quitar_basura(self, cambios: List[Tuple[str, str]]) -> List[str]:
        basura = []
        for estado, ruta in cambios:
            # Nuevo = sin seguimiento o añadido (el `add -N` del revisor lo
            # pasa de «??» a « A»); lo que ya estaba en git no es basura.
            if (estado == "??" or "A" in estado) and not nombre_sano(ruta):
                p = self.carpeta / ruta
                try:
                    # -f: tras el `add -N` del revisor el fichero puede haber
                    # cambiado, y sin él git se niega (24 sep: «node --test»).
                    _git(self.carpeta, "rm", "-q", "--cached", "-f", "--ignore-unmatch", "--", ruta)
                    if p.is_file() or p.is_symlink():
                        p.unlink()
                        basura.append(ruta)
                except (OSError, RuntimeError) as e:
                    logger.warning("Obra: no pude borrar la basura %r (%s)", ruta, e)
        # Carpetas que se hayan quedado vacías tras borrar su basura.
        for ruta in basura:
            padre = (self.carpeta / ruta).parent
            while padre != self.carpeta:
                try:
                    padre.rmdir()
                except OSError:
                    break
                padre = padre.parent
        return basura

    def hacer_tarea(self, n: int, tarea: str) -> Resultado:
        res = Resultado(n=n, tarea=tarea)
        t0 = time.time()
        base = _git(self.carpeta, "rev-parse", "HEAD").strip()
        _, res.tests_antes, _ = self.pasar_tests()
        if not self.presupuesto.reservar(self.tope_tarea, self.adelanto):
            res.motivo = "no queda cupo de gasto hoy"
            return res
        gastado = 0.0
        contado_antes = self.contado
        mensaje = f"{tarea}\n\n{REGLAS}"
        try:
            total_intentos = 1 + self.reintentos + (1 if self.escalar else 0)
            for intento in range(total_intentos):
                res.intentos = intento + 1
                escalado = self.escalar and intento == total_intentos - 1 and intento > 0
                if escalado:
                    self.aviso(f"tarea {n}: último intento razonando a fondo")
                    mensaje = ("Varios intentos rápidos no lo han resuelto. PIENSA A FONDO la causa "
                               "antes de tocar nada: ¿falla el código, o el test pide algo que "
                               "contradice otra parte de la tarea?\n\n" + mensaje)
                esfuerzo_normal = self.esfuerzo
                if escalado:
                    self.esfuerzo = "high"
                try:
                    coste, cortado = self._lanzar_aider(mensaje, self.tope_tarea - gastado
                                                        - (self.contado - contado_antes))
                finally:
                    self.esfuerzo = esfuerzo_normal
                gastado += coste
                cambios = _cambios(self.carpeta)
                res.basura += self._quitar_basura(cambios)
                protegidos = self._tocados_protegidos(cambios)
                if protegidos:
                    res.motivo = "tocó ficheros protegidos: " + ", ".join(protegidos)
                    break
                if cortado:
                    res.motivo = cortado
                    break
                if coste == 0 and self.clave and cambios:
                    # Cambió ficheros sin que se viera ni un token: el formato
                    # de Aider ha cambiado y el contador está ciego. Sin
                    # contador no se sigue gastando.
                    res.motivo = "no pude medir el gasto de Aider (no imprimió los tokens)"
                    res.gasto_ciego = True
                    # Sin saber qué costó, se apunta lo reservado entero.
                    gastado = max(gastado, self.tope_tarea - (self.contado - contado_antes))
                    break
                verde, pasan, cola = self.pasar_tests()
                res.tests_despues = pasan
                if verde and pasan is not None and res.tests_antes is not None and pasan < res.tests_antes:
                    verde = False
                    cola = (f"Antes pasaban {res.tests_antes} tests y ahora {pasan}: "
                            "no se pueden borrar ni saltar tests.\n" + cola)
                if not verde:
                    res.motivo = "los tests no pasan"
                    mensaje = (f"La tarea no está terminada: los tests no pasan.\n\n{cola}\n\n"
                               f"Arréglalo sin borrar ni debilitar tests.\n\nLa tarea era:\n{tarea}\n\n{REGLAS}")
                    continue
                graves = self.revisor(tarea, self._diff_de_la_tarea(base))
                res.revisada = graves is not None
                res.graves = graves or []
                if res.graves:
                    res.motivo = "la revisión encontró problemas graves"
                    lista = "\n".join(f"- {g}" for g in res.graves)
                    mensaje = (f"La revisión de la tarea encontró problemas graves:\n{lista}\n\n"
                               f"Corrígelos y añade tests que lo comprueben.\n\nLa tarea era:\n{tarea}\n\n{REGLAS}")
                    continue
                res.aceptada = True
                res.motivo = "" if res.revisada else "aceptada sin revisión (no se pudo revisar)"
                break
            if res.aceptada:
                # Lo que hayan dejado los propios tests después de la limpieza.
                cambios = _cambios(self.carpeta)
                res.basura += self._quitar_basura(cambios)
                cambios = _cambios(self.carpeta)
                protegidos = self._tocados_protegidos(cambios)
                restos = [r for e, r in cambios if (e == "??" or "A" in e) and not nombre_sano(r)]
                if protegidos:
                    res.aceptada = False
                    res.motivo = "tocó ficheros protegidos: " + ", ".join(protegidos)
                elif restos:
                    res.aceptada = False
                    res.motivo = "no pude quitar la basura: " + ", ".join(restos)
            if res.aceptada:
                _git(self.carpeta, "add", "-A")
                primera = tarea.strip().splitlines()[0][:70]
                res.commit = self._commit(f"Tarea {n}: {primera}")
            else:
                self._deshacer(base)
        except Exception:
            # Un fallo inesperado no puede dejar el repo a medias.
            try:
                self._deshacer(base)
            except Exception:
                logger.exception("Obra: no pude deshacer la tarea %d tras un fallo", n)
            raise
        finally:
            total = gastado + (self.contado - contado_antes)
            self.contado += gastado
            # Lo de la revisión ya se liquidó aparte, con su propia reserva. Si
            # Aider se pasó (el corte llega después de cobrada la respuesta),
            # el exceso se apunta igual: nunca se deja un gasto sin contar.
            self.presupuesto.liquidar(self.tope_tarea, min(gastado, self.tope_tarea))
            if gastado > self.tope_tarea:
                self.presupuesto.apuntar(gastado - self.tope_tarea)
            res.coste = round(total, 4)
            res.segundos = round(time.time() - t0, 1)
        return res

    # ── La obra entera ─────────────────────────────────────────────────
    def ejecutar(self, tareas: Sequence[str]) -> List[Resultado]:
        self._preparar_repo()
        saldo0 = self.saldo(self.clave)
        resultados: List[Resultado] = []
        for n, tarea in enumerate(tareas, 1):
            self.aviso(f"tarea {n}/{len(tareas)}: {tarea.splitlines()[0][:80]}")
            res = self.hacer_tarea(n, tarea)
            resultados.append(res)
            self.aviso(resumen_tarea(res))
            if not res.aceptada or not res.revisada or res.gasto_ciego:
                # No se construye encima de algo roto ni de algo sin mirar.
                break
        saldo1 = self.saldo(self.clave)
        if saldo0 is not None and saldo1 is not None:
            real = saldo0 - saldo1
            # El saldo va en céntimos y lo comparte con el chat: sólo se
            # apunta lo que pase de lo contado más un céntimo de redondeo.
            if real > self.contado + 0.01:
                logger.warning("Obra: el saldo bajó %.2f $ y conté %.4f $ — apunto la diferencia",
                               real, self.contado)
                self.presupuesto.apuntar(real - self.contado)
                self.contado = real
        return resultados


def resumen_tarea(r: Resultado) -> str:
    estado = "✓" if r.aceptada else "✗"
    tests = f"{r.tests_antes}→{r.tests_despues} tests" if r.tests_despues is not None else "sin tests"
    extra = f" · basura quitada: {len(r.basura)}" if r.basura else ""
    motivo = f" · {r.motivo}" if r.motivo else ""
    return (f"{estado} tarea {r.n} · {r.intentos} intento(s) · {tests} · "
            f"{r.coste:.3f} $ · {r.segundos:.0f} s{extra}{motivo}")


def saldo_deepseek(clave: str) -> Optional[float]:
    if not clave:
        return None
    req = urllib.request.Request(f"{URL_DEEPSEEK}/user/balance",
                                 headers={"Authorization": f"Bearer {clave}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            datos = json.loads(resp.read())
        return float(datos["balance_infos"][0]["total_balance"])
    except Exception:
        return None


def _presupuesto_real() -> PresupuestoMensual:
    from .config import Config
    from .paths import MEM_DIR
    from .tz import ahora_usuario
    en_tests = os.environ.get("CELESTIA_EN_TESTS", "").strip() == "1"
    ruta = None if en_tests else Path(
        os.environ.get("CELESTIA_GASTO_DEEPSEEK", "").strip() or MEM_DIR / "gasto_deepseek.json")
    return PresupuestoMensual(Config.DEEPSEEK_TOPE_MES, ruta, hoy=lambda: ahora_usuario().date())


def leer_tareas(ruta: Path) -> List[str]:
    """Una tarea por bloque, separados por una línea en blanco."""
    texto = Path(ruta).read_text(encoding="utf-8")
    return [b.strip() for b in re.split(r"\n\s*\n", texto) if b.strip()]


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse
    p = argparse.ArgumentParser(description="Celestia dirige una obra de programación tarea a tarea.")
    p.add_argument("carpeta", type=Path)
    p.add_argument("--tareas", type=Path, required=True, help="fichero: una tarea por bloque")
    p.add_argument("--test-cmd", default="python3 -m pytest -q")
    p.add_argument("--tope-tarea", type=float, default=0.20)
    p.add_argument("--reintentos", type=int, default=2)
    p.add_argument("--proteger", action="append", default=[], help="patrón glob relativo")
    p.add_argument("--informe", type=Path, help="dónde guardar el informe JSON")
    p.add_argument("--archivo", action="append", default=None,
                   help="fichero que Aider tiene delante (por defecto, todo el código)")
    p.add_argument("--leer", action="append", default=[], help="fichero de consulta, sólo lectura")
    p.add_argument("--adelanto", type=float, default=0.0,
                   help="$ que hoy se pueden tomar de los días siguientes (nunca del mes)")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    obra = Obra(a.carpeta, test_cmd=a.test_cmd, tope_tarea=a.tope_tarea,
                reintentos=a.reintentos, protegidos=a.proteger, archivos=a.archivo,
                leer=a.leer, adelanto=a.adelanto, aviso=print)
    resultados = obra.ejecutar(leer_tareas(a.tareas))
    hechas = sum(r.aceptada for r in resultados)
    print(f"\n{hechas}/{len(leer_tareas(a.tareas))} tareas aceptadas · {obra.contado:.3f} $ contados")
    if a.informe:
        a.informe.write_text(json.dumps([asdict(r) for r in resultados], ensure_ascii=False, indent=1),
                             encoding="utf-8")
    return 0 if hechas == len(leer_tareas(a.tareas)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
