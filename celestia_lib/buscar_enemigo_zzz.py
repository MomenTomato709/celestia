"""Ir a por el enemigo cuando no se le ve — el joystick DENTRO del reflejo.

Sesión 75, 16 sep 2026. Enzo, tras mirar la partida de la noche: «la vi pegar a
la pared todo el rato y el enemigo estaba en otro sitio», «los dos primeros
enemigos los mató, ya después empezó a pegar a la pared». El diario le da la
razón: en los tramos 3, 4 y 5 la barra del enemigo se vio el 0 % del tiempo y el
reflejo siguió atacando los 50 segundos enteros.

El joystick estaba hecho desde la sesión 71, pero en el OTRO carril
(`jugador.py.andar`, con `motionevent` sostenido). En una pelea manda el reflejo
en C —Python no puede mirar la pantalla sin robarle el canal—, y el reflejo sólo
sabía de botones: atacar, especial, esquivar, relevo, definitiva, cadena. Sin
joystick, un enemigo que reaparece a diez metros es un enemigo al que no se
llega nunca.

Esto es la decisión, aparte y sin tocar pantalla para poder probarla: cuándo se
da por perdido al enemigo y hacia dónde ir a buscarlo. Es el espejo de
`buscar_enemigo()` en `agente_movil/reflejo_zzz.c`, y los tests exigen a los dos
las mismas órdenes con los mismos números.

La señal de que hay enemigo delante es su barra de vida flotante
([[vida_enemigo_zzz]]): mientras se la ve, se pelea; cuando lleva
`PERDIDO_MS` sin aparecer, se deja de atacar y se va a por él. Medido en la
partida de anoche: peleando de verdad se veía en 28-30 fotogramas de cada
segundo, y pegando a la pared en 0 o 1.
"""
from typing import NamedTuple, Optional, Sequence

# Sin un solo fotograma con barra durante este rato: no hay enemigo delante.
# A 29 fps son unos 35 fotogramas seguidos, así que un fallo suelto del
# detector no saca a nadie de la pelea.
PERDIDO_MS = 1200

# Lo que se sostiene el joystick por paso. El joystick no se toca: se empuja y
# se aguanta, y el rato que se aguanta es lo que se anda (sesión 71).
PASO_MS = 800

# Tanto rato sin verle que ya no vale fiarse de por dónde andaba.
SIN_RASTRO_MS = 4000

# Con la cámara detrás, un enemigo a este lado de la pantalla está a ese lado
# del mundo: se va en diagonal hacia él.
CENTRO_IZQ, CENTRO_DER = 0.42, 0.58
DIAGONAL = 0.7

# Andando a ciegas, cada tantos pasos se gira la cámara para barrer el sitio.
GIRO_CADA = 2
GIRO_MS = 150

# Parte del TRAMO que como mucho se puede gastar en buscar y perseguir.
#
# 🔴 18 sep, 824 tramos reales: se fue el 42,6 % del tiempo en buscar —con el
# tope ya puesto en 25— y el daño cayó de 0,465 a 0,303 barras por minuto. El
# tope se comparaba contra lo CORRIDO del tramo, y así en el primer segundo el
# presupuesto son 250 ms (no deja ir a por él justo cuando hay que encontrarlo,
# y se pega al aire) y al final es de segundos enteros (ya nada frena el paseo).
# Contra la duración del tramo es el mismo de principio a fin.
TOPE_BUSCANDO_PCT = 25

# Y en dos bolsas, porque andar y girar no cuestan lo mismo. De los 62 tramos
# medidos el 18 sep, los que mejor salieron hacían 11 giros y 13 andares en 50 s;
# los peores, 32 y 41. Girar cuesta 150 ms y es LO ÚNICO que vuelve a poner al
# enemigo delante; andar cuesta 800, y a ciegas aleja. Con una sola bolsa, cuatro
# andares se la comen entera y el resto del tramo es pegar al aire con el enemigo
# a la espalda; con dos, se deja de pasear mucho antes de dejar de mirar.
#
# Medido en seco con el propio binario, pantalla sin enemigo (el peor caso), a
# igualdad de fotogramas: la búsqueda baja del 25 % al 8 % del tramo y los
# ataques suben un 13 % (186 → 211 en 50 s, 56 → 64 en 15 s).
TOPE_ANDANDO_PCT = 20
TOPE_GIRANDO_PCT = 8


class Orden(NamedTuple):
    """Qué hacer en este instante. `clase`: pelear, andar o camara."""
    clase: str
    dx: float = 0.0
    dy: float = 0.0
    ms: int = 0


PELEAR = Orden("pelear")


def perdido(ahora_ms: int, visto_ms: int, umbral_ms: int = PERDIDO_MS) -> bool:
    """¿Llevo sin ver la barra del enemigo más de lo tolerable?

    `visto_ms` < 0 es «no le he visto todavía»: al empezar el tramo se le da el
    mismo margen, que la pelea puede arrancar con la cámara girando.
    """
    if visto_ms < 0:
        return ahora_ms > umbral_ms
    return ahora_ms - visto_ms > umbral_ms


def hacia_donde(x_rel: Optional[float]) -> tuple:
    """Dirección del joystick para ir a por él; (0,-1) es hacia delante."""
    if x_rel is None:
        return 0.0, -1.0
    if x_rel < CENTRO_IZQ:
        return -DIAGONAL, -DIAGONAL
    if x_rel > CENTRO_DER:
        return DIAGONAL, -DIAGONAL
    return 0.0, -1.0


def presupuesto_busca(tramo_ms: int, pct: int = TOPE_BUSCANDO_PCT) -> int:
    """Lo que se puede gastar del tramo en buscar, en ms. -1 = sin tope."""
    return tramo_ms * pct // 100 if tramo_ms > 0 else -1


def cabe_en(ms: int, queda: int) -> int:
    """Lo que cabe de un movimiento de `ms` en los `queda` ms de su bolsa.

    Se descuenta ANTES de moverse: si el tope se mira sólo con lo ya andado, el
    último empujón entra entero aunque no quepa, y 800 ms de más en un
    presupuesto de 3.500 son 23 puntos de exceso. Un movimiento que no cabe ni a
    medias no se hace: un giro de 40 ms no mueve la cámara, sólo gasta.
    """
    if queda < 0:
        return ms
    if queda <= 0:
        return 0
    if queda >= ms:
        return ms
    return queda if queda * 2 >= ms else 0


def _queda(gastado: int, tramo_ms: int, pct: int) -> int:
    tope = presupuesto_busca(tramo_ms, pct)
    if tope < 0:
        return -1
    return max(0, tope - gastado)


def cabe_andando(ms: int, ms_andando: int, tramo_ms: int) -> int:
    """Lo que cabe de un paso del joystick. Andar es lo caro: aleja."""
    return cabe_en(ms, _queda(ms_andando, tramo_ms, TOPE_ANDANDO_PCT))


def cabe_girando(ms: int, ms_girando: int, tramo_ms: int) -> int:
    """Lo que cabe de un giro de cámara. Girar es lo que le vuelve a poner delante."""
    return cabe_en(ms, _queda(ms_girando, tramo_ms, TOPE_GIRANDO_PCT))


def que_hacer(ahora_ms: int, visto_ms: int, x_rel: Optional[float], pasos: int,
              umbral_ms: int = PERDIDO_MS, ms_andando: int = 0,
              ms_girando: int = 0, tramo_ms: int = 0) -> Orden:
    """La orden de este instante: pelear, andar hacia él, o girar la cámara.

    Las dos bolsas son el presupuesto del tramo: gastada la suya, ese movimiento
    no se hace y se PELEA donde se esté. Es mejor pegarle a veces que correr
    siempre detrás — y como las bolsas van aparte, cuando ya no se puede pasear
    todavía se puede mirar, que es lo que le devuelve a la pantalla.
    """
    if not perdido(ahora_ms, visto_ms, umbral_ms):
        return PELEAR
    a_ciegas = visto_ms < 0 or ahora_ms - visto_ms > SIN_RASTRO_MS
    if a_ciegas and pasos > 0 and pasos % GIRO_CADA == 0:
        ms = cabe_girando(GIRO_MS, ms_girando, tramo_ms)
        return Orden("camara", 1.0, 0.0, ms) if ms > 0 else PELEAR
    dx, dy = hacia_donde(None if a_ciegas else x_rel)
    ms = cabe_andando(PASO_MS, ms_andando, tramo_ms)
    return Orden("andar", dx, dy, ms) if ms > 0 else PELEAR


def segundos_perdido(vistos_por_segundo: Sequence[int]) -> int:
    """Cuántos segundos de un tramo ya grabado habrían sido de búsqueda.

    Sirve para juzgar los tramos viejos con el criterio nuevo: se le da por
    perdido el segundo siguiente a uno sin barra (el umbral es de 1,2 s).
    """
    fuera, sin_barra = 0, 0
    for vistos in vistos_por_segundo:
        if vistos <= 0:
            if sin_barra:
                fuera += 1
            sin_barra += 1
        else:
            sin_barra = 0
    return fuera
