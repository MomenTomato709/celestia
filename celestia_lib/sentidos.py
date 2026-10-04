"""Lo que Celestia puede hacer en ESTE aparato, sentido por sentido.

En el móvil de Enzo lo tenía todo porque, con los meses, le fue metiendo
claves gratis (Groq, Gemini, OpenRouter…) y programas (Whisper, SearXNG). Una
Celestia recién instalada en un PC o en la app sólo tiene la clave que le
pegaron, y nadie sabía qué le faltaba ni cómo dárselo (Enzo, 4 oct 2026:
«no podemos estar atrasados otra vez»). Esto lo cuenta en un sitio: el
banner al arrancar, la sección «Sentidos» de Ajustes y /sentidos.

Cada sentido: {id, nombre, ok, como, falta, claves: [{var, nombre, enlace}]}.
`claves` son las claves GRATIS que lo encenderían; se pegan en el chat.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
from typing import Any, Dict, List

from . import oido

# Dónde se saca cada clave gratis (las que reconoce primer_arranque al pegarlas).
ENLACES = {
    "GEMINI_API_KEY": ("Gemini", "https://aistudio.google.com/apikey"),
    "GROQ_API_KEY": ("Groq", "https://console.groq.com/keys"),
    "OPENROUTER_API_KEY": ("OpenRouter", "https://openrouter.ai/settings/keys"),
    "CEREBRAS_API_KEY": ("Cerebras", "https://cloud.cerebras.ai/"),
}

# Nombre que se enseña de cada cerebro.
_CEREBROS = (("DEEPSEEK_API_KEY", "DeepSeek"), ("GROQ_API_KEY", "Groq"),
             ("GEMINI_API_KEY", "Gemini"), ("CEREBRAS_API_KEY", "Cerebras"),
             ("OPENROUTER_API_KEY", "OpenRouter"), ("MISTRAL_API_KEY", "Mistral"),
             ("SAMBANOVA_API_KEY", "SambaNova"), ("GITHUB_MODELS_TOKEN", "GitHub"),
             ("NVIDIA_API_KEY", "NVIDIA"))


def _hay(var: str) -> bool:
    return bool(os.environ.get(var, "").strip())


def _claves(*vars_: str) -> List[Dict[str, str]]:
    """Las que faltan, de entre las que lo arreglarían."""
    return [{"var": v, "nombre": ENLACES[v][0], "enlace": ENLACES[v][1]}
            for v in vars_ if v in ENLACES and not _hay(v)]


def _pensar() -> Dict[str, Any]:
    tiene = [nombre for var, nombre in _CEREBROS if _hay(var)]
    if not tiene:
        return {"ok": False, "como": "", "falta": "sin ninguna clave no puedo pensar",
                "claves": _claves("GEMINI_API_KEY", "GROQ_API_KEY")}
    if len(tiene) == 1:
        # Funciona, pero si ese proveedor falla o se acaba el cupo del día, se
        # queda en blanco: se cuenta como «a medias».
        return {"ok": True, "medio": True, "como": tiene[0],
                "falta": "sin cerebro de reserva si ese falla",
                "claves": _claves("GROQ_API_KEY", "GEMINI_API_KEY", "OPENROUTER_API_KEY")}
    return {"ok": True, "como": " + ".join(tiene[:4]) + ("…" if len(tiene) > 4 else ""),
            "falta": "", "claves": []}


def _ver(api=None) -> Dict[str, Any]:
    como = []
    if _hay("GEMINI_API_KEY"):
        como.append("Gemini")
    if _hay("OPENROUTER_API_KEY"):
        como.append("OpenRouter")
    if _hay("GROQ_API_KEY") and os.environ.get("GROQ_VISION_MODEL", "").strip():
        como.append("Groq")
    try:
        if api is not None and api._vision_disponible():
            como.append("en este aparato")
    except Exception:
        pass
    if como:
        return {"ok": True, "como": " + ".join(como), "falta": "", "claves": []}
    return {"ok": False, "como": "", "falta": "no veo fotos ni capturas",
            "claves": _claves("GEMINI_API_KEY", "OPENROUTER_API_KEY")}


def _oir() -> Dict[str, Any]:
    e = oido.estado()
    if e["ok"]:
        return {"ok": True, "como": e["como"], "falta": "", "claves": []}
    if e.get("instalando"):
        falta = "me lo estoy instalando ahora mismo"
    elif e.get("se_instala_solo"):
        falta = "me lo instalo sola con tu primera nota de voz (o con una clave de Groq, al momento)"
    else:
        falta = "no oigo notas de voz"
    return {"ok": False, "como": "", "falta": falta, "claves": _claves("GROQ_API_KEY")}


def _hablar() -> Dict[str, Any]:
    if importlib.util.find_spec("edge_tts") is not None:
        return {"ok": True, "como": "voz de Microsoft (gratis)", "falta": "", "claves": []}
    for programa in ("piper", "espeak-ng", "espeak"):
        if shutil.which(programa):
            return {"ok": True, "como": programa, "falta": "", "claves": []}
    return {"ok": False, "como": "", "falta": "no tengo voz en este aparato", "claves": []}


def _buscar() -> Dict[str, Any]:
    try:                                   # el fijado o el de Termux en 127.0.0.1:8888
        from .tools import AgentTools
        searxng = AgentTools._url_searxng()
    except Exception:
        searxng = os.environ.get("CELESTIA_SEARXNG_URL", "").strip()
    if searxng:
        como = "mi buscador propio (SearXNG)"
    elif _hay_metabuscador():
        como = "varios buscadores a la vez"
    elif os.environ.get("CELESTIA_APP_ANDROID") == "1":
        como = "Wikipedia y fuentes directas (enciende tu SearXNG para buscar de todo)"
    else:
        como = "DuckDuckGo y Wikipedia"
    if _hay("TAVILY_API_KEY"):
        como += " + Tavily"
    return {"ok": True, "como": como, "falta": "", "claves": []}


def _hay_metabuscador() -> bool:
    """ddgs y su motor (primp). En la app de Android primp es una rueda
    fabricada aparte (android/fabricar_ruedas.sh): que esté no basta, tiene
    que cargar."""
    if importlib.util.find_spec("ddgs") is None:
        return False
    try:
        import primp  # noqa: F401
        return True
    except Exception:
        return False


def probar(api) -> Dict[str, Any]:
    """Hablar y buscar de verdad (con red). Para la prueba de la APK."""
    import os as _os
    import time as _time
    pruebas: Dict[str, Any] = {}
    inicio = _time.time()
    try:
        ruta = api._sintetizar("Hola, soy Celestia.")
        pruebas["hablar"] = bool(ruta and _os.path.getsize(ruta) > 1000)
        if ruta:
            _os.unlink(ruta)
    except Exception as e:
        pruebas["hablar"] = f"fallo: {e}"
    pruebas["hablar_s"] = round(_time.time() - inicio, 1)
    inicio = _time.time()
    try:
        from .tools import AgentTools
        texto = AgentTools(None)._ddgs("Celestia inteligencia artificial")
        pruebas["buscar"] = len([l for l in texto.splitlines() if l.strip()])
    except Exception as e:
        pruebas["buscar"] = f"fallo: {e}"
    pruebas["buscar_s"] = round(_time.time() - inicio, 1)
    return pruebas


def _dibujar() -> Dict[str, Any]:
    return {"ok": True, "medio": True, "como": "Pollinations (gratis, calidad básica)",
            "falta": "", "claves": []}


def estado(api=None) -> List[Dict[str, Any]]:
    sentidos = [("pensar", "Pensar", _pensar()), ("ver", "Ver", _ver(api)),
                ("oir", "Oír", _oir()), ("hablar", "Hablar", _hablar()),
                ("buscar", "Buscar", _buscar()), ("dibujar", "Dibujar", _dibujar())]
    return [{"id": i, "nombre": n, **d} for i, n, d in sentidos]


def tras_clave(var: str) -> str:
    """Qué gana con la clave que acaba de pegar, y qué otra gratis le
    vendría bien — para la respuesta de «¡Listo! Clave guardada»."""
    gana = {
        "GROQ_API_KEY": "pensar muy rápido y oír tus notas de voz",
        "GEMINI_API_KEY": "pensar, ver tus fotos y capturas, y oír notas de voz",
        "OPENROUTER_API_KEY": "ver tus fotos y tener cerebros de reserva",
        "CEREBRAS_API_KEY": "un cerebro de reserva muy rápido",
        "DEEPSEEK_API_KEY": "pensar a fondo (es de pago: gasta de tu saldo)",
    }.get(var, "")
    frase = f"Con ella puedo {gana}." if gana else ""
    pendientes = [d for d in estado() if not d["ok"] or d.get("medio")]
    sugerida = next((c for d in pendientes for c in d["claves"]), None)
    if sugerida:
        sentido = next(d["id"] for d in pendientes if sugerida in d["claves"])
        para = {"pensar": "tener un cerebro de reserva", "ver": "ver tus fotos",
                "oir": "oír tus notas de voz"}.get(sentido, sentido)
        frase += (f" Si quieres que además pueda {para}, pégame también una clave gratis "
                  f"de {sugerida['nombre']} ({sugerida['enlace'].split('://', 1)[1]}).")
    return frase.strip()
