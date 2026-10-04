"""Blueprint core: dashboard, docs, estado, perfil, diagnóstico, introspección,
verificación (Camino 2).

Migrado tal cual desde WhatsAppAPI._registrar_rutas; el receptor pasa a ser `api`.
Las constantes/helpers de módulo de api.py se importan dentro de `crear` (api ya
está cargado cuando se registran los blueprints → sin import circular).
"""
import logging
import os
from datetime import datetime
from typing import Any, Dict
from flask import Blueprint, request as flask_request, jsonify

from .. import idiomas
from .. import primer_arranque
from ..paths import MEM_DIR

logger = logging.getLogger("celestia_v1")


def crear(api) -> Blueprint:
    from .. import __version__
    from ..api import (
        _html_escape, _consultar_estado_shizuku,
        HAS_EDGE_TTS,
    )
    from .. import oido
    from .bp_chat import version_pagina as _version_pagina
    bp = Blueprint("core", __name__)

    @bp.route("/dashboard", methods=["GET"])
    def dashboard():
        """Vista HTML mínima con estado actual, últimas conversaciones, aprendizajes y errores.
        Sin frameworks ni JS — solo HTML + CSS embebido. Auto-refresh cada 30 s.
        """
        mem = api.orch.memory
        try:
            stats24 = mem.estadisticas_periodo(86400)
        except Exception:
            stats24 = {}
        try:
            ultimos_aprend = mem.aprendizajes_recientes(10) or []
        except Exception:
            ultimos_aprend = []
        try:
            ultimos_err = mem.errores_recientes(10) or []
        except Exception:
            ultimos_err = []
        try:
            hechos = mem.hechos_usuario() or []
        except Exception:
            hechos = []
        activos = api._aprendizajes_keys_snapshot()
        backend = api.orch.model._backend if hasattr(api.orch.model, "_backend") else "?"
        vis = "local" if api._vision_disponible() else (
            "groq" if api.orch.config.GROQ_API_KEY else "none")

        def fila(items, cols, get):
            if not items:
                return "<tr><td colspan='99' class='vacio'>sin datos</td></tr>"
            out = []
            for it in items:
                out.append("<tr>" + "".join(f"<td>{_html_escape(str(get(it, c)))}</td>" for c in cols) + "</tr>")
            return "".join(out)

        html = f"""<!DOCTYPE html>
<html lang="es"><head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="refresh" content="30">
  <title>Celestia · Dashboard</title>
  <style>
    body{{font-family:system-ui,sans-serif;background:#0e1116;color:#e6e6e6;margin:0;padding:1rem;max-width:1200px;margin:auto}}
    h1{{color:#9c88ff;margin-bottom:.3rem}}
    .sub{{color:#888;margin-bottom:1.5rem}}
    .grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1rem}}
    .card{{background:#1a1e26;border:1px solid #2a2e36;border-radius:8px;padding:1rem}}
    .card h2{{margin:0 0 .5rem 0;font-size:1rem;color:#9c88ff;border-bottom:1px solid #2a2e36;padding-bottom:.5rem}}
    .kv{{display:flex;justify-content:space-between;padding:.2rem 0}}
    .kv b{{color:#fff}}
    table{{width:100%;border-collapse:collapse;font-size:.85rem}}
    th,td{{padding:.3rem;text-align:left;border-bottom:1px solid #2a2e36;vertical-align:top}}
    th{{color:#888;font-weight:normal;font-size:.75rem;text-transform:uppercase}}
    td.vacio{{color:#555;font-style:italic;text-align:center}}
    .ok{{color:#7ee787}} .warn{{color:#f59e0b}} .err{{color:#f87171}}
    .pill{{display:inline-block;padding:.1rem .5rem;border-radius:10px;background:#2a2e36;font-size:.75rem;margin:.1rem}}
  </style>
</head><body>
  <h1>Celestia · Dashboard</h1>
  <p class="sub">Actualizado {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} · auto-refresh 30 s</p>
  <div class="grid">

    <div class="card">
      <h2>Estado</h2>
      <div class="kv"><span>Backend LLM</span><b>{backend}</b></div>
      <div class="kv"><span>Modelo Groq</span><b>{api.orch.config.GROQ_MODEL}</b></div>
      <div class="kv"><span>Fallback Groq</span><b>{api.orch.config.GROQ_FALLBACK_MODEL}</b></div>
      <div class="kv"><span>Visión</span><b>{vis}</b></div>
      <div class="kv"><span>Oído</span><b>{'✓' if oido.disponible() else '✗'}</b></div>
      <div class="kv"><span>TTS (edge-tts)</span><b>{'✓' if HAS_EDGE_TTS else '✗'}</b></div>
      <div class="kv"><span>OpenRouter fallback</span><b>{'✓' if api.orch.config.OPENROUTER_API_KEY else '✗'}</b></div>
      <div class="kv"><span>Auth API</span><b>{'✓ (token)' if api.orch.config.API_TOKEN else '✗ abierto'}</b></div>
    </div>

    <div class="card">
      <h2>Estadísticas últimas 24 h</h2>
      <div class="kv"><span>Conversaciones</span><b>{stats24.get('conversaciones', 0)}</b></div>
      <div class="kv"><span>Aprendizajes</span><b>{stats24.get('aprendizajes', 0)}</b></div>
      <div class="kv"><span>Errores</span><b class="warn">{stats24.get('errores', 0)}</b></div>
    </div>

    <div class="card">
      <h2>Aprendizajes activos</h2>
      {'<br>'.join(f'<span class="pill">{_html_escape(t)}</span>' for t in activos) if activos else '<p class="vacio">ninguno en curso</p>'}
    </div>

    <div class="card">
      <h2>Acciones</h2>
      <p style="font-size:.85rem;color:#888;margin:0 0 .5rem 0">
        Disparar tareas manualmente. Si el endpoint requiere token,
        pásalo via cookie/header desde el navegador.
      </p>
      <form method="POST" action="/forzar_reflexion" style="display:inline">
        <button type="submit" style="background:#9c88ff;color:#fff;border:none;padding:.4rem .8rem;border-radius:6px;cursor:pointer;margin:.2rem">
          🧠 Forzar reflexión
        </button>
      </form>
      <form method="POST" action="/perfil" style="display:inline"
            onsubmit="return confirm('¿Resetear el onboarding? El usuario tendrá que rellenar perfil de nuevo.')">
        <input type="hidden" name="reset" value="true">
        <button type="submit" style="background:#f59e0b;color:#fff;border:none;padding:.4rem .8rem;border-radius:6px;cursor:pointer;margin:.2rem">
          ♻ Reset perfil
        </button>
      </form>
      <form method="POST" action="/reiniciar" style="display:inline"
            onsubmit="return confirm('¿Reiniciar Celestia entera? Tardará ~60s en volver.')">
        <button type="submit" style="background:#f87171;color:#fff;border:none;padding:.4rem .8rem;border-radius:6px;cursor:pointer;margin:.2rem">
          ⏻ Reiniciar
        </button>
      </form>
    </div>

    <div class="card" style="grid-column: 1 / -1">
      <h2>Últimos aprendizajes</h2>
      <table><thead><tr><th>Tarea</th><th>Estado</th><th>Intentos</th><th>Resultado</th></tr></thead>
      <tbody>{fila(ultimos_aprend, ('tarea','estado','intentos','resultado'),
                       lambda d, c: d.get(c) if isinstance(d, dict) else '?')}</tbody></table>
    </div>

    <div class="card" style="grid-column: 1 / -1">
      <h2>Últimos errores</h2>
      <table><thead><tr><th>Contexto</th><th>Tipo</th><th>Mensaje</th><th>Acción</th></tr></thead>
      <tbody>{fila(ultimos_err, ('contexto','tipo','mensaje','accion'),
                       lambda d, c: d.get(c) if isinstance(d, dict) else '?')}</tbody></table>
    </div>

    <div class="card" style="grid-column: 1 / -1">
      <h2>Hechos del usuario ({len(hechos)})</h2>
      <table><thead><tr><th>Tipo</th><th>Clave</th><th>Valor</th></tr></thead>
      <tbody>{fila(hechos[:30], ('tipo','clave','valor'),
                       lambda d, c: d.get(c) if isinstance(d, dict) else '?')}</tbody></table>
    </div>

  </div>
</body></html>"""
        return html, 200, {"Content-Type": "text/html; charset=utf-8"}

    @bp.route("/docs", methods=["GET"])
    def docs():
        """OpenAPI 3 mínimo de los endpoints públicos. Útil para integraciones externas."""
        return jsonify({
            "openapi": "3.0.0",
            "info": {"title": "Celestia API", "version": __version__},
            "servers": [{"url": "http://127.0.0.1:8765"}],
            "components": {
                "securitySchemes": {
                    "tokenAuth": {
                        "type": "apiKey", "in": "header", "name": "X-Celestia-Token",
                        "description": "Opcional. Configurar CELESTIA_API_TOKEN en .env."
                    }
                }
            },
            "paths": {ep: {} for ep in (
                "/mensaje", "/audio", "/captura", "/wake_check",
                "/transcribir_llamada", "/salud", "/dashboard", "/reiniciar",
                "/estado", "/docs", "/perfil", "/introspeccion", "/verificar",
                "/plan", "/grafo", "/aprendizaje/estado",
            )},
        })

    @bp.route("/latido", methods=["GET"])
    def latido():
        """¿Sigo viva? Lo más barato que se puede contestar: nada más.

        Sin base de datos, sin red, sin Shizuku. Existe porque `/estado`
        —que sí mira todo eso— tarda segundos cuando el aparato está ocupado:
        el sondeo de Shizuku por rish cuesta 2,4-4,5 s de por sí, y **mientras
        Celestia juega, rish lo tiene cogido el jugador**. El guardián da por
        muerta a la que no contesta en 8 s, así que con `/estado` como única
        señal de vida acababa MATÁNDOLA a media partida (visto el 7 sep 2026,
        14:51: la partida en curso se perdió en el reinicio).

        La distinción es la de siempre en esta casa: ocupada no es muerta.
        """
        return jsonify({"vivo": True, "pid": os.getpid()})

    @bp.route("/estado", methods=["GET"])
    def estado():
        """Health check con verificación real de subsistemas.
        Devuelve status global ('ok'|'degraded'|'fail') + checks individuales.
        Útil para monitoring externo: si checks.db=false, alertar.
        """
        checks: Dict[str, Any] = {}
        # Modelo LLM
        modelo_ok = (
            api.orch.model._ensure_server_alive()
            if api.orch.model._backend == "llama_server"
            else api.orch.model.loaded
        )
        checks["modelo"] = bool(modelo_ok)
        # DB: query trivial
        try:
            api.orch.memory.conn.execute("SELECT 1").fetchone()
            checks["db"] = True
        except Exception as e:
            checks["db"] = False
            checks["db_error"] = str(e)[:200]
        # FAISS index cargado
        checks["faiss_index"] = api.orch.memory.faiss_index is not None
        # ffmpeg disponible (necesario para TTS)
        import shutil as _sh
        checks["ffmpeg"] = bool(_sh.which("ffmpeg"))
        # Conectividad a internet
        try:
            checks["online"] = api.orch.connectivity.is_online()
        except Exception:
            checks["online"] = False
        # Disco libre en MEM_DIR (alerta si <500 MB)
        try:
            import shutil as _sh2
            free_gb = _sh2.disk_usage(str(MEM_DIR)).free / (1024 ** 3)
            checks["disco_libre_gb"] = round(free_gb, 1)
            checks["disco_ok"] = free_gb > 0.5
        except Exception:
            checks["disco_ok"] = None
        # Visión
        local_vis = api._vision_disponible()
        groq_vis = bool(api.orch.config.GROQ_API_KEY)
        # Shizuku: las DOS vías, bridge (8766) y rish — el bridge va con
        # WhatsApp y suele estar apagado. Puede venir «pendiente»: el
        # sondeo de rish tarda segundos y no se paga en esta petición.
        shizuku_estado = _consultar_estado_shizuku()
        checks["shizuku"] = bool(shizuku_estado.get("conectado"))
        # Status global: degraded si falta algo no-crítico, fail si DB rota
        criticos = (checks["modelo"], checks["db"])
        status = "ok"
        if not checks["db"]:
            status = "fail"
        elif not all(criticos) or checks.get("disco_ok") is False:
            status = "degraded"
        # Qué modelo está contestando ahora mismo. El chat lo pinta en el pie
        # («en línea · …»): pedía `modelo`, que no salía por ninguna parte, y
        # acababa enseñando el nombre del backend («groq»), que no dice cuál.
        _cfg = api.orch.config
        modelo_nombre = {
            "groq":        _cfg.GROQ_MODEL,
            "openrouter":  getattr(_cfg, "OPENROUTER_MODEL", ""),
            "cerebras":    getattr(_cfg, "CEREBRAS_MODEL", ""),
            "gemini":      getattr(_cfg, "GEMINI_MODEL", ""),
            "transformers": _cfg.MODEL_NAME,
            # El GGUF se sirve por ruta: del nombre del fichero se entiende
            # cuál es; de «llama_server», no.
            "llama_server": (getattr(_cfg, "GGUF_MODEL_PATH", "") or "").split("/")[-1],
        }.get(api.orch.model._backend, "") or api.orch.model._backend
        # Mejor aún: el que contestó la última vez. Con DeepSeek delante de
        # Groq, la cabecera del chat decía «gpt-oss-120b» sin que lo fuera.
        _ultimo = getattr(api.orch.model, "_ultimo_proveedor", "")
        modelo_nombre = {
            "deepseek":  getattr(_cfg, "DEEPSEEK_MODEL", ""),
            "groq":      _cfg.GROQ_MODEL,
            "cerebras":  getattr(_cfg, "CEREBRAS_MODEL", ""),
            "gemini":    getattr(_cfg, "GEMINI_MODEL", ""),
            "github":    getattr(_cfg, "GITHUB_MODELS_MODEL", ""),
            "openrouter": getattr(_cfg, "OPENROUTER_MODEL", ""),
            "or_xl":     getattr(_cfg, "OPENROUTER_REASONING_MODEL", ""),
            "sambanova": getattr(_cfg, "SAMBANOVA_MODEL", ""),
            "mistral":   getattr(_cfg, "MISTRAL_MODEL", ""),
            "nvidia":    getattr(_cfg, "NVIDIA_MODEL", ""),
            "anonimo":   "pollinations",
        }.get(_ultimo, "") or modelo_nombre
        return jsonify({
            "status":         status,
            "checks":         checks,
            "modelo":         modelo_nombre,
            "whisper":        oido.disponible(),
            "tts":            HAS_EDGE_TTS,
            "vision":         local_vis or groq_vis,
            "vision_backend": "local" if local_vis else ("groq" if groq_vis else "none"),
            "version":        __version__,
            # Recién instalada y sin ninguna clave de IA: el chat enseña en la
            # portada cómo conseguir una (primer_arranque.py).
            "sin_cerebro":    not primer_arranque.hay_cerebro(_cfg),
            # Quién la arrancó (la app de Android pone una marca por arranque):
            # así sabe que el /estado que contesta es el SUYO y no el de otra
            # Celestia del mismo aparato. Vacío si nadie la puso.
            "instancia":      os.environ.get("CELESTIA_INSTANCIA", ""),
            # La versión de `chat.html` que se sirve ahora. La página abierta la
            # compara con la suya y avisa si se ha quedado atrás; así una mejora
            # no espera a que alguien recuerde recargar.
            "pagina":         _version_pagina(),
            "modelo_ok":      modelo_ok,
            "backend":        api.orch.model._backend,
            "shizuku":        shizuku_estado,
        })

    @bp.route("/sentidos", methods=["GET"])
    def sentidos_():
        """Qué puede hacer en este aparato y qué clave gratis le falta para el
        resto (la sección «Sentidos» de Ajustes)."""
        from .. import sentidos
        salida = {"sentidos": sentidos.estado(api)}
        # ?probar=1: hablar y buscar DE VERDAD (red incluida). Lo usa la prueba
        # de la APK en el emulador: tener la pieza no es lo mismo que funcionar.
        if flask_request.args.get("probar") and \
                (flask_request.remote_addr or "") in ("127.0.0.1", "::1"):
            salida["pruebas"] = sentidos.probar(api)
        return jsonify(salida)

    @bp.route("/perfil", methods=["GET", "POST"])
    def perfil():
        """GET: ver perfil.

        POST con {'reset': true}: borrar y reiniciar onboarding.
        POST con {'modo_canal': 'ambos'|'solo_voz'|'solo_texto'}: cómo responde.

        El modo se podía cambiar hablando («respóndeme solo por voz») pero no
        había forma de *ver* cuál estaba puesto ni de volver atrás desde una
        interfaz. Un usuario con `solo_voz` puesto sin recordarlo ve un chat
        que «no le escribe», y no tiene dónde mirarlo.
        """
        if flask_request.method == "POST":
            data = flask_request.get_json(force=True, silent=True) or {}
            if data.get("reset"):
                api._perfil.datos = {}
                api._perfil.guardar()
                api._onboarding.indice = None
                return jsonify({"status": "reseteado"})
            modo = data.get("modo_canal")
            if modo is not None:
                if modo not in ("ambos", "solo_voz", "solo_texto"):
                    return jsonify({"error": "modo_canal tiene que ser "
                                             "ambos, solo_voz o solo_texto"}), 400
                api._perfil.set_modo_canal(modo)
                return jsonify({"status": "ok", "modo_canal": api._perfil.modo_canal})
            # En qué idioma contesta. «auto» (lo normal) sigue al de cada
            # mensaje; un código fijo sirve a quien escribe mezclando o a quien
            # está aprendiendo un idioma y quiere practicarlo siempre.
            idioma = data.get("idioma")
            if idioma is not None:
                if not api._perfil.set_idioma(idioma):
                    return jsonify({"error": "idioma no reconocido",
                                    "validos": ["auto"] + list(idiomas.IDIOMAS)}), 400
                logger.info("Idioma de Celestia: %s", api._perfil.idioma)
                return jsonify({"status": "ok", "idioma": api._perfil.idioma})
        return jsonify(api._perfil.datos)

    @bp.route("/idiomas", methods=["GET"])
    def listar_idiomas():
        """Los idiomas que se pueden elegir, con su nombre en su propia lengua.

        Un menú que pone «Deutsch» lo encuentra quien busca alemán; uno que
        pone «Alemán», no — y quien más lo necesita es justo quien no lee
        español.
        """
        return jsonify({
            "idiomas": idiomas.para_menu(),
            "actual": api._perfil.idioma,
        })

    @bp.route("/diagnostico", methods=["GET", "POST"])
    def diagnostico():
        """Auto-diagnóstico y reparación. Verifica TTS, STT, llama-server y disco."""
        resultado = api._autofix_general()
        return jsonify({"status": "ok", "diagnostico": resultado})

    @bp.route("/introspeccion", methods=["GET"])
    def introspeccion():
        """Devuelve estado interno de Celestia: estadísticas, aprendizajes, errores, hechos."""
        mem = api.orch.memory
        return jsonify({
            "estadisticas_24h":   mem.estadisticas_periodo(86400),
            "estadisticas_7d":    mem.estadisticas_periodo(604800),
            "aprendizajes":       mem.aprendizajes_recientes(15),
            "errores":            mem.errores_recientes(15),
            "fallos_recurrentes": [{"tipo": t, "n": n} for t, n in mem.fallos_recurrentes(604800, 2)],
            "hechos_usuario":     mem.hechos_usuario(),
            "ultima_reflexion":   mem.ultima_reflexion(),
            "aprendizajes_activos_ram": api._aprendizajes_keys_snapshot(),
            "metricas": {
                "vision_cache_hits":   getattr(api, "_vision_cache_hits", 0),
                "vision_cache_misses": getattr(api, "_vision_cache_misses", 0),
                "vision_cache_size":   len(getattr(api, "_vision_cache", {})),
                "rate_buckets_activos": len(getattr(api, "_rate_buckets", {})),
                "embed_cache_hits":    getattr(api.orch.embedder, "cache_hits", 0),
                "embed_cache_misses":  getattr(api.orch.embedder, "cache_misses", 0),
                "embed_cache_hit_rate_pct": round(api.orch.embedder.hit_rate(), 1) if hasattr(api.orch.embedder, "hit_rate") else 0.0,
                "embed_cache_size":    len(getattr(api.orch.embedder, "_cache", {})),
            },
        })

    @bp.route("/verificar", methods=["POST"])
    def verificar_simbolico():
        """Verificación simbólica de un texto.

        POST {"texto": "...", "usar_llm": false}
        Devuelve: {consistente, verificaciones[], contradicciones[], resumen}.
        """
        data = flask_request.get_json(silent=True) or {}
        texto = (data.get("texto") or "").strip()
        if not texto:
            return jsonify({"error": "texto requerido"}), 400
        usar_llm = bool(data.get("usar_llm", False))
        reasoner = getattr(api.orch, "reasoner", None)
        if reasoner is None:
            return jsonify({"error": "razonador no disponible"}), 503
        try:
            res = reasoner.verificar(texto, usar_llm=usar_llm)
        except Exception as e:
            return jsonify({"error": str(e)}), 500

        def _v_dict(v):
            return {
                "claim": {
                    "texto": v.claim.texto, "tipo": v.claim.tipo,
                    "sujeto": v.claim.sujeto, "relacion": v.claim.relacion,
                    "objeto": v.claim.objeto, "expresion": v.claim.expresion,
                    "valor_afirmado": v.claim.valor_afirmado,
                },
                "resultado": v.resultado,
                "metodo": v.metodo,
                "evidencia": v.evidencia,
                "confianza": v.confianza,
            }
        return jsonify({
            "texto": res.texto,
            "consistente": res.consistente,
            "resumen": res.resumen(),
            "verificaciones": [_v_dict(v) for v in res.verificaciones],
            "contradicciones": [_v_dict(v) for v in res.contradicciones],
        })

    return bp
