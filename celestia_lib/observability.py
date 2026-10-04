"""Observabilidad opcional: Sentry para tracking de errores.

Si SENTRY_DSN está definido en el entorno, `init_sentry()` arranca sentry_sdk
con integración Flask + threads. Los errores no controlados llegan a Sentry
con `request_id` como tag para correlacionar.

Si SENTRY_DSN está vacío o `sentry_sdk` no está instalado, es no-op.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("celestia_v1")

_inicializado = False


def init_sentry(dsn: str, environment: str = "production",
                  release: Optional[str] = None) -> bool:
    """Inicializa Sentry si está disponible. Idempotente.

    Returns
    -------
    bool : True si Sentry quedó activo, False si no.
    """
    global _inicializado
    if _inicializado:
        return True
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.flask import FlaskIntegration
        from sentry_sdk.integrations.threading import ThreadingIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
    except ImportError:
        logger.info("sentry_sdk no instalado — pip install sentry-sdk[flask] para activar")
        return False

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=environment,
            release=release or "celestia@1.5.1",
            integrations=[
                FlaskIntegration(),
                ThreadingIntegration(propagate_hub=True),
                LoggingIntegration(level=logging.INFO, event_level=logging.ERROR),
            ],
            # Sample agresivo en alpha — bajar a 0.1 en producción real
            traces_sample_rate=0.2,
            send_default_pii=False,   # privacidad — NO mandar IPs/usernames a Sentry
            attach_stacktrace=True,
        )
        _inicializado = True
        logger.info("Sentry inicializado (env=%s)", environment)
        return True
    except Exception as e:
        logger.warning("No pude iniciar Sentry: %s", e)
        return False


def capture_with_request_id(exc: Exception, request_id: str = "-") -> None:
    """Captura una excepción a Sentry con request_id como tag.
    No-op si Sentry no está activo.
    """
    if not _inicializado:
        return
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            scope.set_tag("request_id", request_id)
            sentry_sdk.capture_exception(exc)
    except Exception:
        pass
