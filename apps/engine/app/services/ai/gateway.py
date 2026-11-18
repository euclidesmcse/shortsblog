"""Cliente do AI Gateway (`sb-ai-gateway`, rede `sb_internal`).

O gateway não guarda configuração (D-030): cada pedido leva o nome do provedor
e, para os provedores por API key, o `endpoint` com a chave já decifrada.
A chave nunca é escrita em logs nem em mensagens de erro deste módulo.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.config import settings

# Só os testes o substituem (httpx.MockTransport).
_transport: httpx.BaseTransport | None = None

# Folga sobre o limite pedido ao gateway, para a resposta de erro dele chegar.
_HTTP_MARGIN_S = 15.0
# O estado do agy pode levar 90 s no gateway (espera do login) — ver cli.py do gateway.
_STATUS_TIMEOUT_S = 120.0


@dataclass(frozen=True)
class Endpoint:
    base_url: str
    api_key: str
    model: str

    def __repr__(self) -> str:  # a chave nunca entra num repr/traceback
        return f"Endpoint(base_url={self.base_url!r}, model={self.model!r})"


@dataclass(frozen=True)
class GatewayResult:
    provider: str
    model: str
    text: str
    data: dict[str, Any] | None
    input_tokens: int
    output_tokens: int
    duration_ms: int


@dataclass(frozen=True)
class GatewayImage:
    provider: str
    model: str
    data: bytes
    input_tokens: int
    output_tokens: int
    duration_ms: int


class GatewayError(Exception):
    """Falha de uma chamada. `kind` é o do gateway (timeout, not_authenticated,
    rate_limited, empty, invalid_output, failed, …) ou `unreachable`."""

    def __init__(self, provider: str, kind: str, detail: str = "") -> None:
        super().__init__(f"{provider}: {kind}" + (f" ({detail})" if detail else ""))
        self.provider = provider
        self.kind = kind
        self.detail = detail


def call(
    provider: str,
    prompt: str,
    *,
    system: str = "",
    schema: dict[str, Any] | None = None,
    model: str = "",
    endpoint: Endpoint | None = None,
    timeout_s: float | None = None,
) -> GatewayResult:
    """Uma chamada a um provedor. Com `schema`, devolve `data` (objeto JSON)."""
    timeout = timeout_s or settings.ai_timeout_s
    body: dict[str, Any] = {"provider": provider, "prompt": prompt, "timeout_s": timeout}
    if system:
        body["system"] = system
    if endpoint is not None:
        body["endpoint"] = {
            "base_url": endpoint.base_url, "api_key": endpoint.api_key, "model": endpoint.model,
        }
    elif model:
        body["model"] = model
    if schema is not None:
        body["json_schema"] = schema
    path = "/v1/structured" if schema is not None else "/v1/complete"

    try:
        with httpx.Client(transport=_transport, timeout=timeout + _HTTP_MARGIN_S) as client:
            response = client.post(settings.ai_gateway_url.rstrip("/") + path, json=body)
    except httpx.HTTPError as exc:
        # Só o tipo: o pedido leva a API key no corpo.
        raise GatewayError(provider, "unreachable", type(exc).__name__) from None

    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise GatewayError(provider, "failed", f"HTTP {response.status_code} sem JSON")
    if response.status_code != 200:
        kind = payload.get("error")
        detail = payload.get("detail")
        raise GatewayError(
            provider,
            kind if isinstance(kind, str) else "failed",
            detail if isinstance(detail, str) else f"HTTP {response.status_code}",
        )

    raw_usage = payload.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    data = payload.get("data")
    return GatewayResult(
        provider=provider,
        model=str(payload.get("model") or ""),
        text=str(payload.get("text") or ""),
        data=data if isinstance(data, dict) else None,
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        duration_ms=int(payload.get("duration_ms") or 0),
    )


def image(prompt: str, *, provider: str = "codex", timeout_s: float | None = None) -> GatewayImage:
    """Uma imagem pelo gateway (D-056). Só o Codex a gera; devolve os bytes do ficheiro."""
    timeout = timeout_s or settings.ai_image_timeout_s
    body = {"provider": provider, "prompt": prompt, "timeout_s": timeout}
    try:
        with httpx.Client(transport=_transport, timeout=timeout + _HTTP_MARGIN_S) as client:
            response = client.post(settings.ai_gateway_url.rstrip("/") + "/v1/image", json=body)
    except httpx.HTTPError as exc:
        raise GatewayError(provider, "unreachable", type(exc).__name__) from None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise GatewayError(provider, "failed", f"HTTP {response.status_code} sem JSON")
    if response.status_code != 200:
        kind, detail = payload.get("error"), payload.get("detail")
        raise GatewayError(provider, kind if isinstance(kind, str) else "failed",
                           detail if isinstance(detail, str) else f"HTTP {response.status_code}")
    try:
        data = base64.b64decode(str(payload.get("image_base64") or ""), validate=True)
    except ValueError:
        raise GatewayError(provider, "invalid_output", "imagem em base64 inválida") from None
    if not data:
        raise GatewayError(provider, "empty", "sem imagem")
    raw_usage = payload.get("usage")
    usage: dict[str, Any] = raw_usage if isinstance(raw_usage, dict) else {}
    return GatewayImage(
        provider=provider, model=str(payload.get("model") or ""), data=data,
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        duration_ms=int(payload.get("duration_ms") or 0))


def status(provider: str, endpoint: Endpoint | None = None) -> tuple[str, str]:
    """Estado da credencial de um provedor, sem chamar nenhum modelo (E3.6).

    Devolve (ok | not_authenticated | rate_limited | error, detalhe).
    """
    body: dict[str, Any] = {"provider": provider}
    if endpoint is not None:
        body["endpoint"] = {
            "base_url": endpoint.base_url, "api_key": endpoint.api_key, "model": endpoint.model,
        }
    try:
        with httpx.Client(transport=_transport, timeout=_STATUS_TIMEOUT_S) as client:
            response = client.post(settings.ai_gateway_url.rstrip("/") + "/v1/status", json=body)
    except httpx.HTTPError as exc:
        raise GatewayError(provider, "unreachable", type(exc).__name__) from None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if response.status_code != 200 or not isinstance(payload, dict):
        raise GatewayError(provider, "failed", f"HTTP {response.status_code}")
    return str(payload.get("status") or "error"), str(payload.get("detail") or "")


def _login_request(method: str, path: str, provider: str,
                   body: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        with httpx.Client(transport=_transport, timeout=60.0) as client:
            response = client.request(method, settings.ai_gateway_url.rstrip("/") + path,
                                      json=body)
    except httpx.HTTPError as exc:
        raise GatewayError(provider, "unreachable", type(exc).__name__) from None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        raise GatewayError(provider, "failed", f"HTTP {response.status_code} sem JSON")
    if response.status_code != 200:
        raise GatewayError(provider, str(payload.get("error") or "failed"),
                           str(payload.get("detail") or ""))
    return payload


def login_start(provider: str) -> dict[str, Any]:
    """Inicia o login por código de dispositivo no gateway (E3.7): {status, url, code, …}."""
    return _login_request("POST", "/v1/login/start", provider, {"provider": provider})


def login_state(provider: str) -> dict[str, Any]:
    """Login em curso no gateway: status none | running | succeeded | failed."""
    return _login_request("GET", f"/v1/login/{provider}", provider)
