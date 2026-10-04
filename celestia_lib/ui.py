"""Constantes UI/UX: emojis y prefijos de estado consistentes en toda Celestia.

Para que el usuario aprenda a leer:
  ✓ = éxito (acción completada)
  ✗ = error (no se pudo completar)
  ⚠ = advertencia (atención, pero no bloqueante)
  ⏳ = en progreso (esperar)
  🔒 = bloqueado por seguridad/vault
  📄 = documento / archivo
  💡 = sugerencia / pista
  ⏰ = recordatorio / tiempo
"""

# Marcadores de estado
E_OK    = "✓"
E_FAIL  = "✗"
E_WARN  = "⚠"
E_WAIT  = "⏳"
E_LOCK  = "🔒"

# Iconos temáticos
E_DOC   = "📄"
E_FILE  = "📁"
E_TIP   = "💡"
E_TIME  = "⏰"
E_EYE   = "👁"
E_KEY   = "🔑"
E_BOT   = "🤖"
E_WRITE = "✍️"


def ok(msg: str) -> str:
    """Formatea un mensaje de éxito: '✓ <msg>'."""
    return f"{E_OK} {msg}"


def fail(msg: str) -> str:
    """Formatea un mensaje de fallo: '✗ <msg>'."""
    return f"{E_FAIL} {msg}"


def warn(msg: str) -> str:
    """Formatea una advertencia: '⚠ <msg>'."""
    return f"{E_WARN} {msg}"


def wait(msg: str) -> str:
    """Formatea un estado en progreso: '⏳ <msg>'."""
    return f"{E_WAIT} {msg}"
