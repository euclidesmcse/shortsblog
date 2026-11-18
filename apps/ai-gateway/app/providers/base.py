"""Tipos comuns aos provedores de IA."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class ErrorKind(StrEnum):
    TIMEOUT = "timeout"
    NOT_AUTHENTICATED = "not_authenticated"
    RATE_LIMITED = "rate_limited"
    EMPTY = "empty"
    INVALID_OUTPUT = "invalid_output"
    FAILED = "failed"


class ProviderError(Exception):
    """Falha de um provedor, classificada para o chamador decidir o fallback."""

    def __init__(self, provider: str, kind: ErrorKind, detail: str = "") -> None:
        super().__init__(f"{provider}: {kind}" + (f" ({detail})" if detail else ""))
        self.provider = provider
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class Completion:
    provider: str
    model: str
    text: str
    # Preenchido só nas chamadas com esquema JSON.
    data: dict[str, Any] | None
    usage: Usage
    duration_ms: int


@dataclass(frozen=True)
class Image:
    provider: str
    model: str
    data: bytes
    usage: Usage
    duration_ms: int


class Health(StrEnum):
    OK = "ok"
    NOT_AUTHENTICATED = "not_authenticated"
    RATE_LIMITED = "rate_limited"
    ERROR = "error"


@dataclass(frozen=True)
class Status:
    provider: str
    status: Health
    detail: str = ""


class Provider(Protocol):
    """O que a API precisa de um provedor, seja CLI de assinatura ou API key."""

    name: str

    async def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        schema: dict[str, Any] | None = None,
        model: str | None = None,
        timeout_s: float,
    ) -> Completion: ...

    async def status(self) -> Status:
        """Verificação barata (E3.6): há credencial válida? Não gasta cota de modelo."""
        ...
