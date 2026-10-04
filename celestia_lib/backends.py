"""Protocolos (typing.Protocol) para los backends de LLM.

Permite implementar nuevos backends (Anthropic, local mlx, etc.) sin tocar
`ModelWrapper`: solo basta con que implementen la firma de `LLMBackend`.

Uso:

    class MiBackend:
        name = "mi_backend"
        def generate(self, messages, **kwargs) -> str:
            return "..."

    # ModelWrapper acepta instancias compatibles con LLMBackend.
"""
from __future__ import annotations

from typing import Any, Dict, List, Protocol, runtime_checkable


@runtime_checkable
class LLMBackend(Protocol):
    """Contrato que debe cumplir cualquier backend de LLM en Celestia.

    Atributos
    ---------
    name : str
        Identificador corto del backend (`groq`, `openrouter`, `local`, etc.).
        Se usa en logs y en `ModelWrapper._backend`.

    Métodos
    -------
    generate(messages, **kwargs) -> str
        Recibe la conversación en formato OpenAI (`role`/`content`) y devuelve
        la respuesta del modelo. Lanza excepción si falla — el orchestrator
        decide si caer al siguiente backend de la cadena.
    """

    name: str

    def generate(self, messages: List[Dict[str, str]], **kwargs: Any) -> str:
        ...  # pragma: no cover


@runtime_checkable
class StreamingLLMBackend(LLMBackend, Protocol):
    """Backend opcionalmente capaz de streaming token-a-token."""

    def stream(self, messages: List[Dict[str, str]], **kwargs: Any):
        """Iterador que rinde tokens parciales conforme llegan."""
        ...  # pragma: no cover
