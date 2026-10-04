"""Ver un tutorial de ZZZ: sacar la rotación de lo que se VE, no de lo que se dice.

Enzo, 17 sep 2026: «yo quiero que vea el vídeo, no que transcriba, porque en el
vídeo mostraría cada cosa y se vería la rotación; no se verían las pulsaciones de
botones pero ya se podría hacer una idea de cómo funciona».

Y se puede, porque no hace falta que un modelo «entienda» el vídeo: el HUD de ZZZ
ya se sabe leer (barra del enemigo, aturdimiento, anillos, retratos del equipo).
Aplicando esos detectores fotograma a fotograma sale la rotación **con tiempos**:
cuándo entra cada agente, cuánto dura su turno y qué le pasa a la vida del
enemigo mientras.

Lo medido el 17 sep sobre un tutorial real, para no prometer de más:
  · Los tutoriales están EDITADOS: zoom, texto quemado, cortes y cinemáticas de
    definitiva sin HUD. En muchos fotogramas no hay nada que leer, y eso no es un
    fallo: es que ahí no se ve el juego. Se descartan y se sigue.
  · Vienen con bandas negras (letterbox) y grabados en PC, así que las fracciones
    de la pantalla del móvil no valen: primero se recorta el marco útil.
  · Los retratos del equipo, arriba a la izquierda, son lo más estable de todo el
    HUD: ahí se ve quién está dentro y cuándo cambia, que es la rotación.
"""
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

# Zona de los retratos del equipo dentro del marco útil (medido en el tutorial
# real: los tres retratos ocupan la esquina superior izquierda).
# SÓLO el retrato del agente activo, no la franja entera: con la franja entran las
# barras de vida y energía, que cambian a cada golpe, y salían 45 «relevos» en 113
# segundos con turnos de 2 s. El retrato es lo único que cambia cuando entra otro.
# Medido mirando los recortes de verdad: a la izquierda del retrato está el BOTÓN
# DE PAUSA, y con x desde 0,02 se recortaba ese icono en vez de la cara.
EQUIPO_X = (0.078, 0.125)
EQUIPO_Y = (0.020, 0.090)
# Un fotograma sin HUD (cinemática, zoom, pantalla de texto) es casi todo oscuro
# o casi todo plano en esa zona: no se lee.
MIN_VARIEDAD = 8.0
# Cambio de firma a partir del cual se considera que ha entrado otro agente.
CAMBIO_MIN = 0.22
# Dos cambios seguidos más rápidos que esto son el mismo (efectos, parpadeos).
MIN_ENTRE_CAMBIOS_S = 1.5


def marco_util(F: np.ndarray, umbral: int = 24) -> Tuple[int, int, int, int]:
    """Quita las bandas negras: devuelve (y0, y1, x0, x1) de lo que es imagen."""
    lum = F.astype(np.int16).sum(axis=2) // 3
    filas = np.nonzero((lum > umbral).mean(axis=1) > 0.02)[0]
    cols = np.nonzero((lum > umbral).mean(axis=0) > 0.02)[0]
    if len(filas) < 8 or len(cols) < 8:
        return 0, F.shape[0], 0, F.shape[1]
    return int(filas[0]), int(filas[-1]) + 1, int(cols[0]), int(cols[-1]) + 1


def marco_del_video(fotogramas: Sequence[np.ndarray]) -> Tuple[int, int, int, int]:
    """El marco útil del vídeo entero: la mediana de varios fotogramas.

    Por fotograma no vale: los subtítulos quemados en la banda inferior y las
    escenas oscuras mueven el borde, el recorte se desplaza y la firma cambia sin
    que haya habido relevo (43 «relevos» en 113 s, todos falsos).
    """
    marcos = np.array([marco_util(F) for F in fotogramas if F is not None])
    if not len(marcos):
        return 0, 0, 0, 0
    m = np.median(marcos, axis=0).astype(int)
    return int(m[0]), int(m[1]), int(m[2]), int(m[3])


def zona_equipo(F: np.ndarray, marco: Optional[Tuple[int, int, int, int]] = None
                ) -> Optional[np.ndarray]:
    """El recorte del retrato del agente activo, ya sin bandas negras."""
    y0, y1, x0, x1 = marco if marco else marco_util(F)
    al, an = y1 - y0, x1 - x0
    if al < 40 or an < 80:
        return None
    return F[y0 + int(al * EQUIPO_Y[0]): y0 + int(al * EQUIPO_Y[1]),
             x0 + int(an * EQUIPO_X[0]): x0 + int(an * EQUIPO_X[1])]


def firma(recorte: Optional[np.ndarray]) -> Optional[np.ndarray]:
    """Huella de color del recorte, para comparar fotogramas entre sí.

    Un histograma por canal: no dice QUIÉN es, dice si es el mismo de antes. Para
    la rotación basta con eso, y aguanta el zoom y el ruido de la compresión.
    """
    if recorte is None or recorte.size < 300:
        return None
    A = recorte.astype(np.int16)
    if float(A.std()) < MIN_VARIEDAD:
        return None                      # zona plana: no hay HUD que leer
    h = []
    for c in range(3):
        hist, _ = np.histogram(A[..., c], bins=12, range=(0, 256))
        h.append(hist / max(1, hist.sum()))
    return np.concatenate(h)


def distancia(a: Optional[np.ndarray], b: Optional[np.ndarray]) -> Optional[float]:
    if a is None or b is None:
        return None
    return float(np.abs(a - b).sum() / 2.0)


def rotacion_vista(firmas: Sequence[Tuple[float, Optional[np.ndarray]]],
                   cambio_min: float = CAMBIO_MIN) -> List[Dict[str, Any]]:
    """Los relevos que se ven: (segundo, cuánto cambió) y lo que duró cada turno.

    `firmas` es (segundo, firma) por fotograma, con None donde no había HUD.
    """
    fuera: List[Dict[str, Any]] = []
    ultima: Optional[np.ndarray] = None
    t_entrada = None
    for t, f in firmas:
        if f is None:
            continue
        if ultima is None:
            ultima, t_entrada = f, t
            continue
        d = distancia(ultima, f) or 0.0
        if d >= cambio_min:
            if fuera and t - fuera[-1]["segundo"] < MIN_ENTRE_CAMBIOS_S:
                ultima = f
                continue
            fuera.append({"segundo": round(t, 1), "cambio": round(d, 3),
                          "turno_s": round(t - (t_entrada if t_entrada is not None else t), 1)})
            t_entrada = t
        ultima = f
    return fuera


def leer_fotogramas(carpeta: Path, fps: float = 1.0) -> List[Tuple[float, Optional[np.ndarray]]]:
    """Firma de cada fotograma de la carpeta, en orden, con su segundo."""
    from PIL import Image
    rutas = sorted(Path(carpeta).glob("*.png"))
    muestras = []
    for ruta in rutas[:: max(1, len(rutas) // 12)]:
        try:
            muestras.append(np.asarray(Image.open(ruta).convert("RGB")))
        except Exception:
            continue
    marco = marco_del_video(muestras) if muestras else None
    fuera = []
    for i, ruta in enumerate(rutas):
        try:
            F = np.asarray(Image.open(ruta).convert("RGB"))
        except Exception:
            continue
        fuera.append((i / fps, firma(zona_equipo(F, marco))))
    return fuera


def resumen(firmas: Sequence[Tuple[float, Optional[np.ndarray]]]) -> Dict[str, Any]:
    """Cuánto del vídeo se puede leer y qué rotación se ve."""
    con_hud = [t for t, f in firmas if f is not None]
    relevos = rotacion_vista(firmas)
    turnos = [r["turno_s"] for r in relevos if r["turno_s"] > 0]
    return {"fotogramas": len(firmas), "con_hud": len(con_hud),
            "parte_legible": round(len(con_hud) / max(1, len(firmas)), 2),
            "relevos": len(relevos),
            "turno_medio_s": round(float(np.median(turnos)), 1) if turnos else None,
            "cuando": [r["segundo"] for r in relevos[:20]]}

# ── quién está dentro: por la cara, no por el brillo ────────────────────────
# El histograma de color cambia con los efectos y los golpes: daba 42 «relevos»
# en 113 s, todos falsos. Lo que no cambia es la CARA del retrato, así que se
# reduce a 16x16 en gris, se normaliza (fuera brillo y contraste) y se agrupan
# los fotogramas por parecido: los cambios de grupo son los relevos de verdad.
CARA_LADO = 16
MISMA_CARA = 0.55        # distancia coseno por debajo de esto: el mismo agente
MAX_AGENTES = 4          # tres del equipo, y uno de margen para el ruido


def cara(recorte: Optional[np.ndarray]) -> Optional[np.ndarray]:
    """El retrato como vector comparable: 16x16 en gris y normalizado."""
    if recorte is None or recorte.size < 300:
        return None
    from PIL import Image
    g = np.asarray(Image.fromarray(recorte).convert("L").resize((CARA_LADO, CARA_LADO)),
                   dtype=np.float32).ravel()
    if float(g.std()) < 6.0:
        return None                     # recorte plano: no hay retrato
    return (g - g.mean()) / (g.std() + 1e-6)


def _distancia_cara(a: np.ndarray, b: np.ndarray) -> float:
    return float(1.0 - np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-6))


def agrupar_caras(caras: Sequence[Tuple[float, Optional[np.ndarray]]],
                  umbral: float = MISMA_CARA) -> List[Tuple[float, Optional[int]]]:
    """(segundo, nº de agente) por fotograma. El nº es el orden de aparición."""
    centros: List[np.ndarray] = []
    fuera: List[Tuple[float, Optional[int]]] = []
    for t, c in caras:
        if c is None:
            fuera.append((t, None))
            continue
        distancias = [(_distancia_cara(c, k), i) for i, k in enumerate(centros)]
        if distancias and min(distancias)[0] <= umbral:
            i = min(distancias)[1]
            centros[i] = (centros[i] * 0.8 + c * 0.2)      # el centro se afina
        elif len(centros) < MAX_AGENTES:
            centros.append(c)
            i = len(centros) - 1
        else:
            i = min(distancias)[1] if distancias else None
        fuera.append((t, i))
    return fuera


def rotacion_por_caras(etiquetas: Sequence[Tuple[float, Optional[int]]],
                       minimo_s: float = 1.0) -> List[Dict[str, Any]]:
    """Los turnos que se ven: quién estaba dentro, desde cuándo y cuánto duró."""
    turnos: List[Dict[str, Any]] = []
    quien, desde = None, None
    for t, i in etiquetas:
        if i is None:
            continue
        if quien is None:
            quien, desde = i, t
            continue
        if i != quien:
            dur = t - (desde if desde is not None else t)
            if dur >= minimo_s:
                turnos.append({"agente": quien, "desde": round(desde, 1), "dura_s": round(dur, 1)})
                quien, desde = i, t
            # Un cambio más corto que `minimo_s` es un parpadeo: se ignora.
    if quien is not None and desde is not None:
        turnos.append({"agente": quien, "desde": round(desde, 1), "dura_s": None})
    return turnos

# ── elegir el tutorial: el que se puede LEER, no el del mejor título ────────
# Enzo, 17 sep 2026: «pero hay tutoriales mejores». El primero que salió estaba
# muy editado —zoom, texto quemado, cinemáticas— y el 14 % de los fotogramas no
# tenían HUD. En vez de juzgar por el título, se bajan unos segundos de varios
# candidatos y gana el que deja leer más: eso es lo que sirve para aprender.
TROZO = (60, 150)          # el medio del vídeo: ahí ya está peleando
FPS_MUESTRA = 2.0   # a 1 fps no se distingue un relevo real de un parpadeo


def _bajar_trozo(video_id: str, destino: Path, desde: int = TROZO[0], hasta: int = TROZO[1]) -> bool:
    """Baja sólo un trozo del vídeo en baja resolución. Falso si no se pudo."""
    import subprocess
    import sys as _s
    try:
        r = subprocess.run(
            [_s.executable, "-m", "yt_dlp", f"https://www.youtube.com/watch?v={video_id}",
             "-f", "bv*[height<=480]", "--download-sections", f"*{desde}-{hasta}",
             "--no-warnings", "-o", str(destino)],
            capture_output=True, text=True, timeout=420)
        return destino.exists() and destino.stat().st_size > 50_000 and r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _fotogramas(video: Path, carpeta: Path, fps: float = FPS_MUESTRA) -> int:
    import subprocess
    carpeta.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(video),
                        "-vf", f"fps={fps},scale=960:-1", str(carpeta / "f%04d.png"), "-y"],
                       capture_output=True, timeout=420)
    except (OSError, subprocess.SubprocessError):
        return 0
    return len(list(carpeta.glob("*.png")))


def mirar_video(video_id: str, trabajo: Optional[Path] = None) -> Dict[str, Any]:
    """Qué se puede leer de un vídeo: parte legible, agentes y turnos vistos."""
    import shutil
    import tempfile
    from PIL import Image
    tmp = Path(trabajo or tempfile.mkdtemp(prefix="tutorial_"))
    try:
        mp4 = tmp / "v.mp4"
        if not _bajar_trozo(video_id, mp4):
            return {"id": video_id, "error": "no se pudo bajar"}
        fot = tmp / "fot"
        if not _fotogramas(mp4, fot):
            return {"id": video_id, "error": "no se pudieron sacar fotogramas"}
        rutas = sorted(fot.glob("*.png"))
        muestras = [np.asarray(Image.open(f).convert("RGB")) for f in rutas[:: max(1, len(rutas) // 10)]]
        marco = marco_del_video(muestras)
        caras = []
        for i, f in enumerate(rutas):
            F = np.asarray(Image.open(f).convert("RGB"))
            caras.append((i / FPS_MUESTRA, cara(zona_equipo(F, marco))))
        etiquetas = agrupar_caras(caras)
        turnos = rotacion_por_caras(etiquetas)
        legibles = sum(1 for _t, i in etiquetas if i is not None)
        duraciones = [x["dura_s"] for x in turnos if x.get("dura_s")]
        return {"id": video_id, "fotogramas": len(rutas), "legibles": legibles,
                "parte_legible": round(legibles / max(1, len(rutas)), 2),
                "agentes": len({i for _t, i in etiquetas if i is not None}),
                "turnos": len(turnos),
                "turno_medio_s": round(float(np.median(duraciones)), 1) if duraciones else None,
                "rotacion": turnos[:12]}
    finally:
        if trabajo is None:
            shutil.rmtree(tmp, ignore_errors=True)


def nota_del_tutorial(d: Dict[str, Any]) -> float:
    """Cuánto sirve para aprender: manda lo legible, y que se vea el equipo rotar."""
    if d.get("error"):
        return 0.0
    nota = 60.0 * float(d.get("parte_legible") or 0)
    nota += 12.0 * min(3, int(d.get("agentes") or 0))          # los tres del equipo
    turnos = int(d.get("turnos") or 0)
    nota += min(16.0, turnos * 2.0)                            # que haya rotación
    medio = d.get("turno_medio_s")
    if medio and 2.0 <= float(medio) <= 15.0:                  # turnos de pelea real
        nota += 8.0
    return round(nota, 1)


def mejor_tutorial(candidatos: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Mira cada candidato y los ordena por lo que se puede aprender de ellos."""
    vistos = []
    for v in candidatos:
        d = mirar_video(v.get("id", ""))
        d["titulo"] = v.get("titulo", "")
        d["del_equipo"] = list(v.get("del_equipo") or [])
        d["nota"] = nota_del_tutorial(d)
        vistos.append(d)
    # Primero cuántos del equipo salen y después lo legible: un vídeo nitidísimo de
    # OTRO equipo no sirve para aprender el tuyo, y por ahí se colaron dos.
    vistos.sort(key=lambda d: (-len(d["del_equipo"]), -d["nota"]))
    return vistos

# ── de lo que se ve en el vídeo a una rotación que se puede jugar ───────────
# Enzo, 17 sep: «ella también puede ver las rotaciones desde los tutoriales». Y sí:
# de un gameplay salen los turnos con sus tiempos, y de ahí la forma de la
# rotación. No hace falta saber QUIÉN es cada uno para eso: el que acumula más
# tiempo en pantalla es el principal, y los que entran y salen son los apoyos.
# Los nombres se ponen luego con el catálogo de iconos del juego.


def rotacion_ejecutable(turnos: Sequence[Dict[str, Any]], equipo: Sequence[str] = ()
                        ) -> Dict[str, Any]:
    """La rotación que se ve en el vídeo, lista para jugarla.

    Devuelve el orden de entrada (por papel), los segundos de cada turno y a quién
    le toca ser principal.
    """
    buenos = [t for t in turnos if (t.get("dura_s") or 0) >= MIN_TURNO_S]
    if not buenos:
        return {}
    tiempo: Dict[int, float] = {}
    veces: Dict[int, int] = {}
    for t in buenos:
        tiempo[t["agente"]] = tiempo.get(t["agente"], 0.0) + float(t["dura_s"])
        veces[t["agente"]] = veces.get(t["agente"], 0) + 1
    principal = max(tiempo, key=lambda a: tiempo[a])
    # El orden de entrada: como aparecen la primera vez.
    orden, vistos = [], set()
    for t in buenos:
        if t["agente"] not in vistos:
            vistos.add(t["agente"])
            orden.append(t["agente"])
    import statistics
    turno_de = {a: round(statistics.median([float(t["dura_s"]) for t in buenos
                                            if t["agente"] == a]), 1)
                for a in tiempo}
    papeles = {a: ("principal" if a == principal else "apoyo") for a in tiempo}
    fuera = {"orden": orden, "principal": principal, "papeles": papeles,
             "turno_s": turno_de, "relevos": len(buenos),
             "cada_s": round(sum(float(t["dura_s"]) for t in buenos) / len(buenos), 1)}
    # Con el equipo delante se puede poner nombre al principal: el del equipo que
    # más daño tenga apuntado. Es una propuesta, no una identificación.
    if equipo:
        fuera["equipo"] = list(equipo)
    return fuera


# Un segundo ES un turno: en el gameplay del equipo de Enzo los apoyos entran
# exactamente 1 s —sueltan su habilidad y salen— y el principal aguanta de 2 a 18.
# Con el filtro en 1,5 s se descartaban todos los apoyos y la rotación salía de un
# solo agente, que es justo lo contrario de lo que hace el vídeo.
MIN_TURNO_S = 1.0
