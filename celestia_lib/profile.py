"""Perfil persistente del usuario y flujo de onboarding.

Extraído del monolito en sesión 15.
"""
import json
import re
from datetime import datetime
from typing import Any, Dict, Optional

from .memory import _CLAVE_CREDENCIAL_RE
from .paths import ES_ANDROID, MEM_DIR

# Lo que NO es contestar al cuestionario, aunque llegue mientras está abierto.
# Visto en el examen del 23 sep 2026 con una Celestia recién instalada: guardó
# «¿cuál es mi contraseña del banco?» como NOMBRE («Perfecto ¿cuál es mi
# contraseña del banco?») y «mi contraseña del wifi es Naranja99» como
# preferencia, que además viaja al modelo en cada mensaje (`como_contexto`).
_ES_PREGUNTA_RE = re.compile(
    # Sin tilde no cuentan: «que me trates de tú» y «como quieras» contestan.
    r"\?|^\s*¿|^\s*(?:qué|cuál(?:es)?|cómo|cuánd[oa]s?|dónde|por\s*qué|quién(?:es)?)\b",
    re.I)
_ES_PETICION_RE = re.compile(
    r"^\s*(?:por\s+favor[,\s]+)?(?:guarda|recu[eé]rda(?:me)?|busca|dime|dame|pon|"
    r"activa|desactiva|abre|cierra|olvida|act[uú]a|traduce|translate|expl[ií]ca(?:me)?|"
    r"cuenta|calcula|haz(?:me)?|crea|manda|env[ií]a|llama|escribe|borra|juega|mira|"
    r"ens[eé][ñn]a(?:me)?|"
    # Portátil, 3 oct 2026: «Divide en sílabas…» se guardó como el TRATO y
    # «convierte 100 dólares a euros» como el USO, y ninguno se contestó.
    r"divide|convierte|pasa(?:me)?|res[uú]me(?:lo)?|separa|ordena|compara|"
    r"recomi[eé]nda(?:me)?|sugi[eé]re(?:me)?|genera|dibuja|quita|apaga|enciende|"
    r"reproduce|av[ií]sa(?:me)?|l[eé]e(?:me)?|analiza|revisa|corrige|cambia|"
    r"apunta|anota|a[ñn]ade|elimina|descarga|comparte|canta|inventa|"
    r"(?:me\s+)?(?:puedes|podr[ií]as|sabes))\b", re.I)
# Dos preguntas tienen respuestas cerradas: una respuesta tiene que nombrar
# alguna de las opciones (o dejarlo a su elección). Lo demás es otra cosa.
_RESPUESTA_CERRADA_RE = {
    "trato": re.compile(
        r"\b(?:t[uú]|usted|formal|informal|tute\w*|cercan\w*|confianza|serio|"
        r"normal|colega|amig\w*|quieras|prefieras|igual|cualquier\w*|lo\s+que\s+sea)\b",
        re.I),
    "voz": re.compile(
        r"\b(?:texto|escri\w*|voz|audio\w*|habla\w*|ambos|ambas|l[oa]s\s+dos|"
        r"mensaje\w*|depende|quieras|prefieras|igual|cualquier\w*|lo\s+que\s+sea)\b",
        re.I),
}
# Un saludo: lo único con lo que empieza el cuestionario. Si lo primero que
# llega es un encargo, se atiende el encargo y el cuestionario espera.
_SALUDO_RE = re.compile(
    r"^\s*¿?\s*(?:hola+|holi|buenas(?:\s+(?:d[ií]as|tardes|noches))?|buenos\s+d[ií]as|hey|ey|"
    r"hi|hello|saludos|qu[eé]\s+tal)"
    # Detrás sólo vale más saludo: «hola, guarda mi contraseña» ya es un encargo.
    r"(?:[\s,!¡.¿]+(?:celestia|qu[eé]\s+tal|c[oó]mo\s+(?:est[aá]s|vas|andas)|buenas))*"
    r"[\s!¡.?¿]*$", re.I)
_ACLARACION_RE = re.compile(
    r"^\s*(?:pero\b|me\s+refer[ií]a\b|me\s+refiero\b|o\s+sea\b|digo\s+que\b|"
    r"no\s+no\b|no,\s|quiero\s+decir\b|lo\s+que\s+(?:digo|quiero\s+decir)\b)", re.I)
_NOMBRE_PREFIJO_RE = re.compile(r"^\s*(?:me\s+llamo|mi\s+nombre\s+es|soy)\s+", re.I)


class PerfilUsuario:
    """Perfil persistente del usuario para personalizar respuestas."""

    _FILE = MEM_DIR / "perfil_usuario.json"

    def __init__(self):
        self.datos: Dict[str, Any] = {}
        self.cargar()

    def cargar(self) -> None:
        if self._FILE.exists():
            try:
                self.datos = json.loads(self._FILE.read_text(encoding="utf-8"))
            except Exception:
                self.datos = {}

    def guardar(self) -> None:
        self._FILE.parent.mkdir(parents=True, exist_ok=True)
        self._FILE.write_text(
            json.dumps(self.datos, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    @property
    def completo(self) -> bool:
        return bool(self.datos.get("onboarding_completo", False))

    @property
    def nombre(self) -> str:
        return self.datos.get("nombre", "")

    def como_contexto(self) -> str:
        """Devuelve un resumen del perfil para inyectar en el system prompt.

        IMPORTANTE: incluye instrucción explícita para que el LLM respete
        cambios de identidad declarados en el chat — sin esto, si otra
        persona usa el WhatsApp del usuario y se presenta con otro nombre,
        Celestia la sigue llamando como el dueño del perfil (bug sesión 29:
        Lydia/Marcos seguían siendo tratados como "Enzo").
        """
        if not self.datos:
            return ""
        lineas = []
        if self.datos.get("nombre"):
            lineas.append(
                f"El usuario habitual se llama {self.datos['nombre']}. "
                f"SI EN LA CONVERSACIÓN ACTUAL alguien se presenta con un nombre "
                f"distinto ('me llamo X', 'soy X', 'no soy {self.datos['nombre']}'), "
                f"trátalo con ESE nombre — puede ser otra persona usando el dispositivo."
            )
        if self.datos.get("trato"):
            lineas.append(f"Prefiere que le hables así: {self.datos['trato']}.")
        if self.datos.get("uso"):
            lineas.append(f"Te usa principalmente para: {self.datos['uso']}.")
        if self.datos.get("voz"):
            lineas.append(f"Sobre voz vs texto: {self.datos['voz']}.")
        if self.datos.get("intereses"):
            lineas.append(f"Sobre el usuario: {self.datos['intereses']}.")
        if self.genero == "neutro":
            lineas.append(
                "CÓMO HABLAS DE TI: sin marcas de género. Nada de «lista», "
                "«cansada», «encantada» ni «una IA»: usa fórmulas que valgan "
                "igual («ya está», «con ganas», «un placer», «tu IA»). Si te "
                "preguntan si eres una mujer, di que no: eres una IA y no "
                "tienes género."
            )
        elif self.genero == "masculino":
            lineas.append("CÓMO HABLAS DE TI: en masculino («listo», "
                          "«encantado»).")
        return " ".join(lineas)

    @property
    def modo_canal(self) -> str:
        """Devuelve 'ambos' (default), 'solo_voz' o 'solo_texto'."""
        v = self.datos.get("modo_canal")
        if v in ("ambos", "solo_voz", "solo_texto"):
            return v
        return "ambos"

    def set_modo_canal(self, modo: str) -> None:
        if modo in ("ambos", "solo_voz", "solo_texto"):
            self.datos["modo_canal"] = modo
            self.guardar()

    # ── Cómo habla de sí misma ───────────────────────────────────────────
    # El usuario le pidió dos veces que no se tratara en femenino y ella se
    # negó las dos («mi creadora me configuró con género femenino y no puedo
    # cambiarlo»): un dato inventado —nadie lo configuró así— y una negativa
    # a algo que sí depende de él. Como todo lo que tiene que salir igual con
    # cualquier modelo, se decide aquí y no en la buena voluntad del LLM.
    @property
    def genero(self) -> str:
        """'femenino' (lo de siempre), 'neutro' o 'masculino'."""
        v = self.datos.get("genero_celestia")
        return v if v in ("femenino", "neutro", "masculino") else "femenino"

    def set_genero(self, genero: str) -> None:
        if genero in ("femenino", "neutro", "masculino"):
            self.datos["genero_celestia"] = genero
            self.guardar()

    # ── En qué idioma le habla ───────────────────────────────────────────
    # «auto» es lo normal: contesta en el idioma de cada mensaje, que es lo que
    # espera cualquiera. Fijarlo sirve para quien escribe mezclando (mucha
    # gente teclea en inglés palabras sueltas) o para quien quiere practicar
    # un idioma. Celestia no se usa solo en España.
    @property
    def idioma(self) -> str:
        """Código ISO ('en', 'de'…) o 'auto' para seguir al que escribe."""
        v = (self.datos.get("idioma") or "auto").lower()
        if v == "auto":
            return "auto"
        from .idiomas import es_valido
        return v if es_valido(v) else "auto"

    def set_idioma(self, codigo: str) -> bool:
        """Fija el idioma (o 'auto'). Devuelve si se aceptó."""
        codigo = (codigo or "").strip().lower()
        from .idiomas import es_valido
        if codigo != "auto" and not es_valido(codigo):
            return False
        self.datos["idioma"] = codigo
        self.guardar()
        return True

    # ── Voz: persistir id, velocidad y tono ──────────────────────────────
    @property
    def voz_id(self) -> str:
        return self.datos.get("voz_id", "es-ES-ElviraNeural")

    @property
    def voz_rate(self) -> str:
        """Velocidad: '+0%' default. Rango razonable: -50% a +100%."""
        return self.datos.get("voz_rate", "+0%")

    @property
    def voz_pitch(self) -> str:
        """Tono: '+0Hz' default. Rango razonable: -50Hz a +50Hz."""
        return self.datos.get("voz_pitch", "+0Hz")

    def set_voz(self, voz_id: Optional[str] = None,
                  rate: Optional[str] = None,
                  pitch: Optional[str] = None) -> None:
        if voz_id is not None:
            self.datos["voz_id"] = voz_id
        if rate is not None:
            self.datos["voz_rate"] = rate
        if pitch is not None:
            self.datos["voz_pitch"] = pitch
        self.guardar()


class OnboardingFlow:
    """Flujo de presentación inicial: hace preguntas para construir el perfil."""

    PREGUNTAS = [
        ("nombre",    "Para empezar, ¿cómo te llamas?"),
        ("trato",     "¿Cómo prefieres que te trate: de tú o de usted? ¿En tono formal o informal?"),
        ("uso",       "¿En qué áreas crees que más te voy a ayudar? Por ejemplo: trabajo, estudios, hogar, ocio, todo."),
        ("voz",       "¿Prefieres que te responda por texto, por voz, o por ambos según el momento?"),
        ("intereses", "Cuéntame brevemente algo de ti: a qué te dedicas, qué te apasiona. Me ayuda a entenderte mejor."),
    ]

    INTRO = (
        "¡Hola! Soy Celestia, tu asistente personal autónoma. "
        "Antes de empezar voy a hacerte unas preguntas rápidas para conocerte mejor "
        "y poder ayudarte como te mereces. Si quieres saltarte alguna, dime 'paso' o 'siguiente'.\n\n"
    )

    def __init__(self, perfil: PerfilUsuario):
        self.perfil = perfil
        self.indice: Optional[int] = None

    def en_curso(self) -> bool:
        return self.indice is not None

    def iniciar(self) -> str:
        self.indice = 0
        return self.INTRO + self.PREGUNTAS[0][1]

    @staticmethod
    def es_saludo(texto: str) -> bool:
        return bool(_SALUDO_RE.match(texto or ""))

    def es_respuesta(self, texto: str) -> bool:
        """¿`texto` contesta a la pregunta abierta, o es otra cosa (un encargo,
        una pregunta, un dato secreto)? Lo que no es respuesta no se guarda."""
        if self.indice is None:
            return False
        t = (texto or "").strip()
        if not t or _CLAVE_CREDENCIAL_RE.search(t):
            return False
        if _ES_PREGUNTA_RE.search(t) or _ES_PETICION_RE.match(t):
            return False
        # Una aclaración de lo de antes tampoco: «pero me refiero a la key de
        # deepseek» se guardó como «para qué la usas» (3 oct 2026).
        if _ACLARACION_RE.match(t):
            return False
        clave = self.PREGUNTAS[self.indice][0]
        if clave == "nombre":
            nombre = _NOMBRE_PREFIJO_RE.sub("", t).strip(" .!")
            return (0 < len(nombre.split()) <= 4 and not re.search(r"\d", nombre)
                    and not _SALUDO_RE.match(nombre))
        if clave in _RESPUESTA_CERRADA_RE:
            return (len(t.split()) <= 12
                    and bool(_RESPUESTA_CERRADA_RE[clave].search(t)))
        return True

    def procesar(self, respuesta: str) -> str:
        """Guarda la respuesta a la pregunta actual y devuelve la siguiente, o cierre."""
        if self.indice is None:
            return ""
        clave, _ = self.PREGUNTAS[self.indice]
        saltar = bool(re.search(r"^\s*(paso|siguiente|skip|salta(?:r|me)?)\s*$",
                                  respuesta.strip(), re.I))
        if not saltar and respuesta.strip():
            valor = respuesta.strip()
            if clave == "nombre":
                valor = _NOMBRE_PREFIJO_RE.sub("", valor).strip(" .!")
            self.perfil.datos[clave] = valor
        self.indice += 1
        if self.indice >= len(self.PREGUNTAS):
            self.perfil.datos["onboarding_completo"] = True
            self.perfil.datos["onboarding_fecha"] = datetime.now().isoformat()
            self.perfil.guardar()
            self.indice = None
            nombre = self.perfil.nombre
            saludo = f"Perfecto {nombre}. " if nombre else "Perfecto. "
            # En un PC no hay teléfono que controlar (portátil, 3 oct 2026).
            telefono = "controlar el teléfono, " if ES_ANDROID else ""
            return (f"{saludo}Ya tengo lo que necesitaba. A partir de ahora te ayudo "
                    f"con cualquier cosa: dictarme tareas, {telefono}"
                    f"crear documentos, aprender cosas nuevas o lo que se te ocurra. "
                    f"¿En qué empezamos?")
        self.perfil.guardar()
        return self.PREGUNTAS[self.indice][1]
