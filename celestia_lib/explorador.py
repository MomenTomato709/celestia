"""Recorrer el juego a propósito, en vez de esperar a tropezarse con él.

Enzo, 9 sep 2026: «Celestia ahora mismo es tonta, no sabe jugar, no sabe cómo
funciona nada del juego, y si va simplemente buscando algo cuando es nuevo pues
va a aprender a jugar dentro de años».

Tiene razón, y el motivo es que hay **dos saberes distintos** y sólo uno crecía
solo:

- La escuela sabe *contar* el juego: qué es un Chain Attack. Eso se baja de la
  wiki y ya son cientos de fichas.
- El libro de jugadas sabe *jugarlo*: en ESTA pantalla se toca AQUÍ. Y eso sólo
  crecía jugando, a razón de las pantallas que la partida se cruzara por
  casualidad.

Esto llena el segundo a propósito: entra en cada sitio, prueba cada botón, mira
a dónde lleva y **vuelve atrás** para seguir por el siguiente. No intenta jugar
bien; intenta *conocer el juego*. Una hora de esto cataloga más pantallas que
una tarde de partidas.

## Lo que NO hace, y es lo importante

No toca nada que cueste dinero ni comprometa la cuenta. El freno es el mismo
del jugador (`_cuesta_dinero`), y aquí importa el doble: una partida toca donde
cree que hay que tocar, y esto toca **todo**. Por eso cada pantalla nueva se
lee entera con OCR antes de tocar nada, y las cajas que caigan sobre palabras
de comprar, gastar, aceptar o vincular se saltan sin probarlas.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from celestia_lib.jugador import (Accion, LibroDeJugadas, Limites, Marcador,
                                  Ojo, Palabra, Punto, Retina, ZONAS_HUELLA,
                                  Zona)

logger = logging.getLogger("celestia_v1")

# Cuánto se espera a que la pantalla reaccione a un toque. Menos de un segundo
# y se fotografía la animación a medias, que cuenta como pantalla nueva y
# ensucia el mapa con fantasmas.
ESPERA_TRAS_TOCAR = 1.2

# Cuántas veces se vuelve a intentar el «atrás» antes de dar la vuelta por
# perdida y seguir explorando desde donde se haya quedado.
INTENTOS_DE_VOLVER = 2


@dataclass
class Pantalla:
    """Una pantalla del juego y lo que se ha probado en ella."""

    clave: str
    nombre: str = ""
    # {índice de caja: "cambia" | "nada"}, para no repetir lo ya probado.
    probadas: Dict[str, str] = field(default_factory=dict)
    salidas: Dict[str, str] = field(default_factory=dict)   # caja → pantalla
    vistas: int = 0
    # Qué botón de ESTA pantalla devuelve a la anterior. Se descubre solo: si
    # tocar algo lleva justo al sitio de donde se venía, eso es la flecha de
    # volver del juego. Hace falta porque el atrás de Android, en un juego a
    # pantalla completa, no retrocede — cierra.
    vuelve: str = ""
    # Lo que pone en la pantalla, tal cual se leyó. Es lo que permite pedirle
    # «vete al campo de pruebas» sin que nadie haya nombrado nada a mano: una
    # huella no se puede buscar, un texto sí.
    texto: str = ""

    def como_dict(self) -> Dict[str, Any]:
        return {"nombre": self.nombre, "probadas": self.probadas,
                "salidas": self.salidas, "vistas": self.vistas,
                "vuelve": self.vuelve, "texto": self.texto}


class MapaDelJuego:
    """El plano: qué pantallas hay y qué botón lleva de una a otra."""

    def __init__(self, ruta: Optional[Path] = None):
        self.ruta = ruta
        self.pantallas: Dict[str, Pantalla] = {}
        self.cargar()

    def cargar(self) -> None:
        if self.ruta is None or not self.ruta.exists():
            return
        try:
            crudo = json.loads(self.ruta.read_text("utf-8"))
        except (OSError, ValueError) as e:
            logger.warning("no pude leer el mapa del juego (%s)", e)
            return
        for clave, d in (crudo.get("pantallas") or {}).items():
            self.pantallas[clave] = Pantalla(
                clave=clave, nombre=d.get("nombre", ""),
                probadas=d.get("probadas", {}), salidas=d.get("salidas", {}),
                vistas=int(d.get("vistas", 0)), vuelve=d.get("vuelve", ""),
                texto=d.get("texto", ""))

    def guardar(self) -> None:
        if self.ruta is None:
            return
        try:
            self.ruta.parent.mkdir(parents=True, exist_ok=True)
            self.ruta.write_text(json.dumps(
                {"pantallas": {k: p.como_dict() for k, p in self.pantallas.items()}},
                ensure_ascii=False), "utf-8")
        except OSError as e:
            logger.warning("no pude guardar el mapa del juego (%s)", e)

    def de(self, huella: Sequence) -> Pantalla:
        clave = _clave_de(huella)
        p = self.pantallas.get(clave)
        if p is None:
            p = Pantalla(clave=clave)
            self.pantallas[clave] = p
        return p

    def buscar(self, texto: str) -> List[str]:
        """Las pantallas donde pone esto. Sin acentos ni mayúsculas."""
        aguja = _sin_tildes(texto)
        if not aguja:
            return []
        return [c for c, p in self.pantallas.items()
                if aguja in _sin_tildes(p.texto) or aguja in _sin_tildes(p.nombre)]

    def como_llegar(self, desde: str, hasta: str,
                    tope: int = 12) -> List[Tuple[str, int]]:
        """Cómo ir de una pantalla a otra: [(pantalla, botón)]. Vacía si no sabe.

        Se llama así y no `ruta` porque `self.ruta` es el fichero del mapa: un
        atributo de instancia tapa al método del mismo nombre, y `mapa.ruta(...)`
        reventaba con «NoneType no se puede llamar».

        Es una anchura primero sobre las salidas que descubrió el explorador.
        Nada de modelo ni de OCR: son toques encadenados sobre lo ya conocido,
        así que llegar a un sitio conocido cuesta milisegundos y cero cuota.
        """
        if desde == hasta:
            return []
        vistos = {desde}
        cola: List[Tuple[str, List[Tuple[str, int]]]] = [(desde, [])]
        while cola:
            donde, camino = cola.pop(0)
            if len(camino) >= tope:
                continue
            pantalla = self.pantallas.get(donde)
            if pantalla is None:
                continue
            for caja, destino in pantalla.salidas.items():
                try:
                    i = int(caja)
                except ValueError:
                    continue
                if destino in vistos:
                    continue
                paso = camino + [(donde, i)]
                if destino == hasta:
                    return paso
                vistos.add(destino)
                cola.append((destino, paso))
        return []

    @property
    def sin_terminar(self) -> int:
        """Pantallas donde queda algún botón por probar."""
        return sum(1 for p in self.pantallas.values()
                   if len(p.probadas) < 4)      # 4 = pocas cajas probadas aún


def _sin_tildes(texto: str) -> str:
    """Minúsculas y sin acentos, para que «Campo de Pruebas» case con «pruebas»."""
    import unicodedata
    plano = unicodedata.normalize("NFKD", (texto or "").lower())
    return "".join(c for c in plano if not unicodedata.combining(c))


def _clave_de(huella: Sequence) -> str:
    """La huella como texto, para poder usarla de llave en un JSON."""
    return "|".join(f"{c[0]},{c[1]},{c[2]}" for c in huella)


class Navegante:
    """Ir a donde ya se sabe ir, sin preguntarle a nadie.

    Enzo, 10 sep 2026: «que use el mapa para ir a donde quiere». Es la otra
    mitad de explorar: catalogar sirve de poco si al jugar se vuelve a
    descubrir el camino cada vez. Aquí una ruta conocida son toques
    encadenados —milisegundos y cero cuota— en vez de una mirada por pantalla.

    Devuelve siempre si llegó o no; nunca «creo que sí». Si en algún paso la
    pantalla no es la que el mapa esperaba, se para: seguir tocando la
    secuencia a ciegas es cómo se acaba pulsando cualquier cosa.
    """

    def __init__(self, jugador, mapa: "MapaDelJuego"):
        self.j = jugador
        self.mapa = mapa
        self.bitacora: List[str] = []

    def donde_estoy(self) -> Optional[str]:
        cap = self.j.mando.ver()
        if cap is None or not cap.crudo:
            return None
        clave = _clave_de(Retina.firma(cap, ZONAS_HUELLA))
        return clave if clave in self.mapa.pantallas else None

    def ir_a(self, texto: str) -> bool:
        """Llegar a la pantalla donde pone `texto`. Devuelve si se llegó."""
        aqui = self.donde_estoy()
        if aqui is None:
            self.bitacora.append("no reconozco dónde estoy: no puedo guiarme")
            return False
        destinos = self.mapa.buscar(texto)
        if not destinos:
            self.bitacora.append(f"no tengo ninguna pantalla con «{texto}»")
            return False
        if aqui in destinos:
            self.bitacora.append(f"ya estoy en «{texto}»")
            return True
        for hasta in destinos:
            camino = self.mapa.como_llegar(aqui, hasta)
            if camino:
                return self._andar(camino, texto)
        self.bitacora.append(f"sé dónde está «{texto}» pero no cómo llegar desde aquí")
        return False

    def _andar(self, camino: List[Tuple[str, int]], texto: str) -> bool:
        self.bitacora.append(f"sé llegar a «{texto}»: {len(camino)} toques")
        for esperado, i in camino:
            cap = self.j.mando.ver()
            if cap is None or not cap.crudo:
                return False
            if _clave_de(Retina.firma(cap, ZONAS_HUELLA)) != esperado:
                self.bitacora.append(
                    "el camino no va como lo tenía apuntado: paro aquí")
                return False
            cajas = Marcador.candidatos(cap.png) if cap.png else []
            if i >= len(cajas):
                self.bitacora.append(
                    "esta pantalla ya no tiene el botón que yo recordaba")
                return False
            self.j.mando.tocar(cajas[i].centro())
            time.sleep(ESPERA_TRAS_TOCAR)
        self.bitacora.append(f"llegué a «{texto}»")
        return True


class Explorador:
    """Recorre el juego tocando lo que no ha probado, y apunta a dónde lleva.

    Se le pasa el mismo `Jugador` que juega —no una copia— porque todo lo que
    hace falta ya está ahí: el mando, el ojo, el libro, el freno del dinero y
    la comprobación de que el juego sigue delante. Aquí sólo cambia el
    propósito: en vez de perseguir un objetivo, se persigue el mapa.
    """

    # Cuántas pantallas seguidas sin nada tocable se aguantan. Pasa de verdad:
    # dentro de un combate no hay botones con fondo liso que reconocer.
    EN_BLANCO_PARA_RENDIRSE = 6

    # Cuántos sitios se prueban como mucho en una pantalla. Con la rejilla de
    # respaldo son 24, y probarlos todos son cuatro minutos por pantalla.
    MAX_SITIOS = 24

    # Por debajo de esto, una pantalla no da para explorar: se completa con la
    # rejilla. La primera tanda real encontró UN solo sitio por pantalla.
    MIN_SITIOS = 6

    # Cuántos pasos de paseo seguidos sin descubrir nada se aguantan antes de
    # dar el juego por recorrido. Sin este tope, el explorador se queda
    # navegando en círculos por pantallas que ya conoce.
    PASEO_SIN_NOVEDAD = 25

    # Donde está la flecha de volver en un móvil: arriba a la izquierda. No es
    # una regla de ningún juego, es la convención de Android — y en ZZZ se
    # comprobó a mano en la sesión 67: (0.085, 0.083). Se prueba ahí cuando
    # hay que retroceder y todavía no se sabe cuál es el botón de esta
    # pantalla. Enzo, 10 sep 2026: «si necesita ir hacia atrás, que le dé al
    # botón de atrás si hay».
    ESQUINA_DE_VOLVER = (0.085, 0.083)

    # Cuántas veces se intenta volver al juego antes de parar del todo. Pocas
    # a propósito: si no se consigue estar dentro, lo que hay debajo del dedo
    # es la pantalla de otro.
    FUERA_PARA_RENDIRSE = 3

    def __init__(self, jugador, mapa: Optional[MapaDelJuego] = None):
        self.j = jugador
        self.mapa = mapa if mapa is not None else MapaDelJuego()
        self.bitacora: List[str] = []
        # ¿Sirve el atrás de Android en este juego? Se descubre probándolo.
        self.back_sirve = True
        self._objetivo = ""
        self._lim: Optional[Limites] = None
        # Veces que se ha tirado por cada puerta, para repartir el paseo.
        self._usos: Dict[str, int] = {}

    # ── lo que se ve ────────────────────────────────────────────────────
    def _mirar(self) -> Tuple[Optional[Any], Optional[Sequence], List[Zona]]:
        cap = self.j.mando.ver()
        if cap is None or not cap.crudo:
            return None, None, []
        huella = Retina.firma(cap, ZONAS_HUELLA)
        cajas = Marcador.candidatos(cap.png) if cap.png else []
        return cap, huella, cajas

    def _donde_tocar(self, cap, cajas: List[Zona],
                     palabras: Sequence[Palabra]) -> List[Zona]:
        """Los sitios que merece la pena probar en esta pantalla.

        Tres vías, y en este orden, porque cada una falla donde la siguiente
        acierta:

        1. **Los botones que encuentra el marcador.** Van primero porque son
           los buenos: caja exacta y sin OCR. Sobre arte de juego devuelve
           **cero**, y eso ya se sabía (sesión 61: «los botones de un juego son
           manchas de color uniforme, no lo que no es fondo»).
        2. **Las palabras que ha leído.** Un menú es texto, y cada palabra
           tiene su caja: si pone «Ajustes», ahí se puede tocar.
        3. **Una rejilla a ciegas**, sólo si hay texto en la pantalla. Es la
           red de seguridad para menús que el marcador no ve y cuyas palabras
           el OCR lee a medias.

        ⚠️ Y si no hay NI botones NI texto, se devuelve vacío a propósito: eso
        no es un menú, es una pelea, un vídeo o una carga. Tocar veinticuatro
        puntos a ciegas ahí no cataloga nada y sí puede liarla.
        """
        con_texto = [p for p in palabras if (p.texto or "").strip()]
        sitios: List[Zona] = list(cajas)
        sitios += [p.zona for p in con_texto if p.zona is not None]
        # 🔴 La rejilla toca A CIEGAS, y el freno del dinero mira el texto que
        # hay a la altura del dedo (±5%). Una celda puede caer justo encima de
        # «Comprar» sin que ninguna palabra esté a su altura, y entonces el
        # freno no la ve: lo cazó un test el 10 sep 2026, tocando cinco celdas
        # sobre un botón de comprar. Así que en una pantalla donde haya algo
        # que cueste dinero o comprometa la cuenta, **no se toca a ciegas**:
        # se prueban sólo los sitios concretos, que sí pasan por el freno.
        hay_peligro = any(self._lim.texto_peligroso(p.texto or "")
                          for p in con_texto) if self._lim else False
        if hay_peligro:
            self.bitacora.append(
                "en esta pantalla hay algo que cuesta dinero: no toco a ciegas")
        # Con muy pocos sitios no hay recorrido: la primera tanda encontró
        # «1 de 1» y se quedó sin nada que probar en cuanto lo probó. Si esto
        # es un menú —hay texto— se completa con la rejilla; si no hay ni
        # texto ni botones, es una pelea o un vídeo y no se toca a ciegas.
        if len(sitios) < self.MIN_SITIOS and con_texto and not hay_peligro:
            sitios += self._rejilla()
        if not con_texto and not cajas:
            return []
        # Sin repetir sitios casi iguales, que serían el mismo toque dos veces.
        limpios: List[Zona] = []
        for z in sitios:
            c = z.centro()
            if any(abs(c.x - o.centro().x) < 0.04 and abs(c.y - o.centro().y) < 0.04
                   for o in limpios):
                continue
            limpios.append(z)
        return limpios[:self.MAX_SITIOS]

    def _rejilla(self, columnas: int = 6, filas: int = 4) -> List[Zona]:
        """Una malla de puntos, para menús donde no se reconoce ningún botón."""
        ancho, alto = 1.0 / columnas, 1.0 / filas
        return [Zona(c * ancho, f * alto, ancho, alto)
                for f in range(filas) for c in range(columnas)]

    def _leer(self, cap) -> List[Palabra]:
        """El OCR de la pantalla entera. Caro, y por eso una vez por pantalla."""
        try:
            with self.j.crono.medir("leer"):
                return self.j.ojo.leer(cap)
        except Exception as e:
            logger.warning("no pude leer la pantalla al explorar (%s)", e)
            return []

    # ── el recorrido ────────────────────────────────────────────────────
    def explorar(self, objetivo: str, limites: Optional[Limites] = None) -> Dict[str, Any]:
        lim = limites or Limites(max_jugadas=300, max_segundos=1800)
        self._lim = lim
        t0 = time.time()
        toques = nuevas = 0
        # Vueltas seguidas sin encontrar nada que tocar. Sin este tope, una
        # pantalla sin botones reconocibles —un vídeo, una carga, el combate
        # mismo— deja el explorador dando «atrás» para siempre, y la bitácora
        # con quinientas líneas iguales. Visto en la primera prueba.
        en_blanco = 0
        # Veces seguidas que se ha encontrado fuera del juego.
        fuera = 0
        # Por dónde se ha ido entrando, para reconocer la flecha de volver.
        camino: List[str] = []
        # Pasos de navegación seguidos que no han descubierto nada.
        sin_novedad = 0
        leidas: Dict[str, List[Palabra]] = {}

        self._objetivo = objetivo
        puedo, apunte = self.j._poner_el_juego_delante(objetivo)
        if not puedo:
            return {"error": apunte, "pantallas": len(self.mapa.pantallas)}
        if apunte:
            self.bitacora.append(apunte)

        while toques < lim.max_jugadas:
            if time.time() - t0 > lim.max_segundos:
                self.bitacora.append("se acabó el tiempo de explorar")
                break
            if lim.debo_parar():
                self.bitacora.append("me dijiste que parara")
                break
            falta = self.j._sin_bateria(lim)
            if falta:
                self.bitacora.append(falta)
                break

            # 🔴 ¿SIGO DENTRO DEL JUEGO? Esto no es un adorno: es el freno que
            # faltaba la primera noche. El explorador da «atrás» después de
            # cada botón, y unos cuantos «atrás» seguidos **sacan del juego**.
            # Enzo lo vio en su móvil el 9 sep 2026: «estuvo en el terminal
            # tocando sin parar». Catalogó diez pantallas de Termux y metió
            # diez jugadas falsas en el libro. Una partida toca donde cree que
            # hay algo; esto toca TODO, así que salirse es mucho peor aquí.
            if not self.j._sigue_siendo_el_juego():
                fuera += 1
                if fuera == 1:
                    self.bitacora.append(
                        "me he salido del juego: lo traigo y sigo, sin tocar nada")
                puedo, _apunte = self.j._poner_el_juego_delante(objetivo)
                if not puedo or fuera >= self.FUERA_PARA_RENDIRSE:
                    self.bitacora.append(
                        "no consigo quedarme dentro del juego: paro antes de "
                        "ponerme a tocar cualquier otra cosa")
                    break
                time.sleep(1.0)
                continue
            fuera = 0

            cap, huella, cajas = self._mirar()
            if huella is None:
                time.sleep(1.0)
                continue
            pantalla = self.mapa.de(huella)
            pantalla.vistas += 1
            # La lectura hace falta ANTES de decidir dónde tocar: es la que
            # dice si esto es un menú (hay texto) o una pelea (no lo hay).
            if pantalla.clave not in leidas:
                leidas[pantalla.clave] = self._leer(cap)
                if len(leidas) > 40:
                    leidas.pop(next(iter(leidas)))
            palabras = leidas[pantalla.clave]
            if palabras and not pantalla.texto:
                pantalla.texto = " ".join(
                    (p.texto or "").strip() for p in palabras if (p.texto or "").strip()
                )[:300]
            cajas = self._donde_tocar(cap, cajas, palabras)
            if not cajas:
                # Sin nada que probar aquí: se sale y se sigue por otro lado.
                en_blanco += 1
                if en_blanco >= self.EN_BLANCO_PARA_RENDIRSE:
                    self.bitacora.append(
                        f"llevo {en_blanco} pantallas seguidas sin encontrar "
                        f"nada que tocar: aquí no exploro, lo dejo")
                    break
                if en_blanco == 1:
                    self.bitacora.append("aquí no encuentro nada que tocar: salgo")
                self._atras()
                continue
            en_blanco = 0

            i = self._siguiente_sin_probar(pantalla, cajas)
            if i is None:
                pantalla.probadas["completa"] = "si"
                self.mapa.guardar()
                if self._atras():
                    continue
                # Sin vuelta atrás no se acaba la exploración: se sigue hacia
                # delante. Este era el fallo del 10 sep: con el atrás de
                # Android desactivado —porque cerraba el juego— el explorador
                # se rendía a los 34 s en cuanto encontraba una pantalla ya
                # conocida, en vez de tirar por una puerta que ya conocía.
                if self._seguir_hacia_delante(pantalla, cajas):
                    # Navegar también gasta toques: sin esto, el tope no lo
                    # frena y se queda dando el mismo toque para siempre —371
                    # veces en la primera prueba, y en el móvil de Enzo eso son
                    # 371 toques de verdad.
                    toques += 1
                    sin_novedad += 1
                    if sin_novedad >= self.PASEO_SIN_NOVEDAD:
                        self.bitacora.append(
                            f"llevo {sin_novedad} pasos sin encontrar nada "
                            f"nuevo: ya me he recorrido esto, lo dejo")
                        break
                    continue
                self.bitacora.append(
                    "esta pantalla la conozco entera y no sé salir de aquí: "
                    "lo dejo")
                break

            caja = cajas[i]
            punto = caja.centro()
            accion = Accion(tipo="tocar", punto=punto, porque="explorando")
            peligro = self.j._cuesta_dinero(accion, palabras, lim)
            if peligro:
                pantalla.probadas[str(i)] = "no lo toco"
                self.bitacora.append(f"no toco «{peligro}»: eso no lo decido yo")
                continue

            antes = huella
            self.j.mando.tocar(punto)
            toques += 1
            time.sleep(ESPERA_TRAS_TOCAR)
            _cap2, despues, _c2 = self._mirar()
            if despues is None:
                continue
            if Retina.firmas_parecidas(antes, despues):
                pantalla.probadas[str(i)] = "nada"
                continue

            # Llevó a algún sitio: eso es una jugada que funciona, y el libro
            # la quiere tanto como el mapa.
            pantalla.probadas[str(i)] = "cambia"
            destino = self.mapa.de(despues)
            pantalla.salidas[str(i)] = destino.clave
            # ¿Este botón me ha devuelto justo de donde venía? Entonces es la
            # flecha de volver del juego, y es lo que sustituye al atrás de
            # Android cuando éste cierra la aplicación en vez de retroceder.
            if camino and destino.clave == camino[-1] and not pantalla.vuelve:
                pantalla.vuelve = str(i)
                self.bitacora.append(
                    f"el botón {i + 1} es la flecha de volver de esta pantalla")
            # 🔴 Y NO se apunta en el libro de jugadas. Parecía gratis —«si
            # cambió la pantalla, funciona»— y es justo lo contrario: el libro
            # dice «en esta pantalla, para AVANZAR, toca aquí», y el explorador
            # sólo sabe que algo se movió. Entre esos toques hay flechas de
            # volver y celdas de la rejilla. Enzo, 10 sep 2026, viéndola jugar
            # después de explorar: «no sabe cómo jugar, y te diría que ahora
            # sabe menos». Tenía razón: 28 jugadas de exploración metidas en el
            # libro son 28 pantallas donde, al jugar, retrocedería.
            #
            # El mapa (dónde lleva cada botón) y el libro (qué hacer para
            # avanzar) son dos cosas distintas. Esto llena el mapa; el libro se
            # gana jugando, donde una jugada vale si acercó al objetivo.
            if destino.vistas == 0:
                nuevas += 1
                sin_novedad = 0
                self.bitacora.append(
                    f"pantalla nueva por el botón {i + 1} de {len(cajas)}")
            if camino and destino.clave == camino[-1]:
                camino.pop()
            else:
                camino.append(pantalla.clave)
                del camino[:-30]
            self.mapa.guardar()
            # Y se vuelve para seguir probando la pantalla de origen, que es
            # lo que hace que esto sea un recorrido y no un paseo.
            self._atras()

        self.mapa.guardar()
        return {"toques": toques, "pantallas": len(self.mapa.pantallas),
                "nuevas": nuevas, "segundos": round(time.time() - t0),
                "bitacora": self.bitacora[-12:]}

    def _seguir_hacia_delante(self, pantalla: Pantalla,
                              cajas: Sequence[Zona]) -> bool:
        """Tirar por una puerta conocida, prefiriendo donde quede trabajo.

        Es lo que sustituye a «volver atrás» cuando el juego no deja volver:
        en vez de deshacer el camino, se avanza por una salida ya catalogada,
        y se elige la que lleve a una pantalla con botones sin probar.
        """
        pendientes, cualesquiera = [], []
        for caja, destino in pantalla.salidas.items():
            try:
                i = int(caja)
            except ValueError:
                continue
            if i >= len(cajas):
                continue
            cualesquiera.append(i)
            otra = self.mapa.pantallas.get(destino)
            if otra is None or "completa" not in otra.probadas:
                pendientes.append(i)
        opciones = pendientes or cualesquiera
        if not opciones:
            return False
        # La menos usada, para no ir y venir por la misma puerta: con
        # `opciones[0]` fijo, el recorrido se volvía un tic.
        i = min(opciones, key=lambda k: self._usos.get(f"{pantalla.clave}:{k}", 0))
        self._usos[f"{pantalla.clave}:{i}"] = \
            self._usos.get(f"{pantalla.clave}:{i}", 0) + 1
        self.bitacora.append(
            f"aquí ya lo he visto todo: tiro por el botón {i + 1} para seguir")
        self.j.mando.tocar(cajas[i].centro())
        time.sleep(ESPERA_TRAS_TOCAR)
        return True

    def _volver_con_la_flecha(self) -> bool:
        """Retroceder tocando la flecha del propio juego, si se conoce.

        Es lo que queda cuando el atrás de Android cierra la aplicación en vez
        de retroceder. La flecha no se busca por su dibujo —cada juego la pinta
        distinta— sino por lo que hace: el botón que ya devolvió una vez.
        """
        cap, huella, cajas = self._mirar()
        if huella is None or not cajas:
            return False
        pantalla = self.mapa.de(huella)
        destino = None
        if pantalla.vuelve:
            try:
                i = int(pantalla.vuelve)
                if i < len(cajas):
                    destino = cajas[i].centro()
            except ValueError:
                destino = None          # "esquina": se resuelve más abajo
        if destino is None:
            destino = Punto(*self.ESQUINA_DE_VOLVER)
        antes = huella
        self.j.mando.tocar(destino)
        time.sleep(ESPERA_TRAS_TOCAR)
        # Lo primero, otra vez: ¿sigo dentro? Una flecha mal elegida en la
        # esquina puede ser «salir del juego».
        if not self.j._sigue_siendo_el_juego():
            self.bitacora.append("la flecha de volver me ha sacado del juego")
            self.j._poner_el_juego_delante(self._objetivo)
            return False
        ahora = self._mirar()[1]
        if ahora is None or Retina.firmas_parecidas(antes, ahora):
            return False
        if not pantalla.vuelve:
            self.bitacora.append(
                "la flecha de arriba a la izquierda sirve para volver aquí: "
                "me lo apunto")
            pantalla.vuelve = "esquina"
            self.mapa.guardar()
        return True

    def _siguiente_sin_probar(self, pantalla: Pantalla,
                              cajas: Sequence[Zona]) -> Optional[int]:
        for i in range(len(cajas)):
            if str(i) not in pantalla.probadas:
                return i
        return None

    def _atras(self) -> bool:      # noqa: D401
        """Volver a la pantalla anterior. Devuelve si se pudo.

        🔴 `KEYCODE_BACK` parecía la vía universal —cada juego pone su flecha
        donde quiere, pero el botón atrás de Android es el mismo en todos— y en
        un juego a pantalla completa **no retrocede: cierra**. Enzo, 10 sep
        2026, mirando el móvil: «no para de entrar y salir». El registro lo
        confirmaba: tras cada «atrás», delante había `com.nothing.launcher` o
        `com.termux`, y el explorador volvía a abrir el juego para repetirlo.

        Así que se prueba UNA vez y se juzga por el resultado: si el atrás nos
        deja fuera del juego, se apunta que en este juego no sirve y no se
        vuelve a usar. A partir de ahí se explora hacia delante, desde donde se
        haya quedado — menos ordenado, pero sin salir de la aplicación.
        """
        if not self.back_sirve:
            return self._volver_con_la_flecha()
        sh = getattr(self.j.mando, "_sh", None)
        if not callable(sh):
            return False
        antes = self._mirar()[1]
        try:
            sh("input keyevent KEYCODE_BACK", timeout=15)
        except Exception as e:
            logger.debug("no pude dar atrás: %s", e)
            return False
        time.sleep(ESPERA_TRAS_TOCAR)

        # Lo primero no es «¿cambió la pantalla?», es «¿sigo en el juego?».
        if not self.j._sigue_siendo_el_juego():
            self.back_sirve = False
            self.bitacora.append(
                "el botón atrás de Android me saca del juego: dejo de usarlo "
                "y sigo explorando hacia delante")
            logger.info("KEYCODE_BACK saca del juego: desactivado")
            self.j._poner_el_juego_delante(self._objetivo)
            return False
        ahora = self._mirar()[1]
        if antes is None or ahora is None:
            return False
        return not Retina.firmas_parecidas(antes, ahora)
