"""API interna do gateway (E3.3): `/v1/complete` e `/v1/structured`.

O chamador indica o provedor; a cadeia de prioridade por papel entra na E3.5.
Provedores por assinatura (`claude`, `codex`, `agy`) vivem no gateway; os de
API key chegam descritos em `endpoint` em cada pedido, porque o gateway não
guarda configuração nem segredos (D-030).
"""

from __future__ import annotations

import base64
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr

from app import login
from app.config import settings
from app.providers.base import Completion, ErrorKind, Provider, ProviderError
from app.providers.cli import CodexProvider
from app.providers.openai_compat import OpenAICompatibleProvider

router = APIRouter(prefix="/v1")

STATUS_BY_KIND = {
    ErrorKind.TIMEOUT: 504,
    ErrorKind.NOT_AUTHENTICATED: 503,
    ErrorKind.RATE_LIMITED: 429,
    ErrorKind.EMPTY: 502,
    ErrorKind.INVALID_OUTPUT: 502,
    ErrorKind.FAILED: 502,
}


class Endpoint(BaseModel):
    """Serviço compatível com a API da OpenAI (`/chat/completions`)."""

    base_url: str = Field(min_length=1)
    # SecretStr: não aparece em repr, logs nem erros de validação.
    api_key: SecretStr
    model: str = Field(min_length=1)


class CompleteRequest(BaseModel):
    provider: str = Field(min_length=1)
    # Presente = provedor por API key com o nome dado em `provider`.
    endpoint: Endpoint | None = None
    prompt: str = Field(min_length=1)
    system: str = ""
    model: str | None = None
    timeout_s: float | None = Field(default=None, gt=0)


class StructuredRequest(CompleteRequest):
    # JSON Schema do objeto pedido. O Codex exige `additionalProperties: false`
    # e todas as propriedades em `required`.
    json_schema: dict[str, Any]


class UsageOut(BaseModel):
    input_tokens: int
    output_tokens: int


class CompleteResponse(BaseModel):
    provider: str
    model: str
    text: str
    usage: UsageOut
    duration_ms: int


class StructuredResponse(BaseModel):
    provider: str
    model: str
    data: dict[str, Any]
    usage: UsageOut
    duration_ms: int


def _error(status: int, kind: str, provider: str, detail: str = "") -> JSONResponse:
    return JSONResponse(
        status_code=status, content={"error": kind, "provider": provider, "detail": detail}
    )


class StatusRequest(BaseModel):
    provider: str = Field(min_length=1)
    endpoint: Endpoint | None = None


class StatusResponse(BaseModel):
    provider: str
    # ok | not_authenticated | rate_limited | error
    status: str
    detail: str


def _resolve(request: Request, name: str, endpoint: Endpoint | None) -> Provider | JSONResponse:
    providers: dict[str, Provider] = request.app.state.providers
    if endpoint is not None:
        if name in providers:
            return _error(400, "reserved_provider_name", name)
        return OpenAICompatibleProvider(
            name,
            endpoint.base_url,
            endpoint.api_key.get_secret_value(),
            endpoint.model,
            transport=getattr(request.app.state, "api_transport", None),
        )
    provider = providers.get(name)
    if provider is None:
        return _error(404, "unknown_provider", name)
    return provider


async def _call(
    request: Request, body: CompleteRequest, schema: dict[str, Any] | None
) -> Completion | JSONResponse:
    provider = _resolve(request, body.provider, body.endpoint)
    if isinstance(provider, JSONResponse):
        return provider
    timeout_s = min(body.timeout_s or settings.default_timeout_s, settings.max_timeout_s)
    try:
        return await provider.complete(
            body.prompt, system=body.system, schema=schema, model=body.model, timeout_s=timeout_s
        )
    except ProviderError as exc:
        return _error(STATUS_BY_KIND[exc.kind], exc.kind, exc.provider, exc.detail)


def _usage(result: Completion) -> UsageOut:
    return UsageOut(
        input_tokens=result.usage.input_tokens, output_tokens=result.usage.output_tokens
    )


@router.post("/complete", response_model=CompleteResponse)
async def complete(request: Request, body: CompleteRequest) -> CompleteResponse | JSONResponse:
    result = await _call(request, body, None)
    if isinstance(result, JSONResponse):
        return result
    return CompleteResponse(
        provider=result.provider,
        model=result.model,
        text=result.text,
        usage=_usage(result),
        duration_ms=result.duration_ms,
    )


@router.post("/structured", response_model=StructuredResponse)
async def structured(
    request: Request, body: StructuredRequest
) -> StructuredResponse | JSONResponse:
    result = await _call(request, body, body.json_schema)
    if isinstance(result, JSONResponse):
        return result
    assert result.data is not None  # garantido por Provider.complete quando há esquema
    return StructuredResponse(
        provider=result.provider,
        model=result.model,
        data=result.data,
        usage=_usage(result),
        duration_ms=result.duration_ms,
    )


class ImageRequest(BaseModel):
    provider: str = Field(default="codex", min_length=1)
    prompt: str = Field(min_length=1)
    timeout_s: float | None = Field(default=None, gt=0)


class ImageResponse(BaseModel):
    provider: str
    model: str
    image_base64: str
    usage: UsageOut
    duration_ms: int


@router.post("/image", response_model=ImageResponse)
async def image(request: Request, body: ImageRequest) -> ImageResponse | JSONResponse:
    """Uma imagem pelo Codex (D-056). Só este provedor tem geração de imagem."""
    provider = _resolve(request, body.provider, None)
    if isinstance(provider, JSONResponse):
        return provider
    if not isinstance(provider, CodexProvider):
        return _error(400, "image_not_supported", body.provider)
    timeout_s = min(body.timeout_s or settings.default_timeout_s, settings.max_timeout_s)
    try:
        result = await provider.image(body.prompt, timeout_s=timeout_s)
    except ProviderError as exc:
        return _error(STATUS_BY_KIND[exc.kind], exc.kind, exc.provider, exc.detail)
    return ImageResponse(
        provider=result.provider,
        model=result.model,
        image_base64=base64.b64encode(result.data).decode(),
        usage=UsageOut(
            input_tokens=result.usage.input_tokens, output_tokens=result.usage.output_tokens
        ),
        duration_ms=result.duration_ms,
    )


@router.post("/status", response_model=StatusResponse)
async def status(request: Request, body: StatusRequest) -> StatusResponse | JSONResponse:
    """Estado da credencial de um provedor (E3.6). Não chama nenhum modelo."""
    provider = _resolve(request, body.provider, body.endpoint)
    if isinstance(provider, JSONResponse):
        return provider
    result = await provider.status()
    return StatusResponse(provider=result.provider, status=result.status, detail=result.detail)


class LoginRequest(BaseModel):
    provider: str = Field(min_length=1)
    # Iniciar um login APAGA a credencial atual do provedor (visto com o Codex
    # 0.160.0 em 2026-10-03). Sem `force`, recusa-se quando já há login válido.
    force: bool = False


@router.post("/login/start")
async def login_start(request: Request, body: LoginRequest) -> JSONResponse:
    """Inicia o login por código de dispositivo e devolve o URL e o código (E3.7)."""
    if not login.supported(body.provider):
        return _error(400, "login_not_supported", body.provider, "autenticar pelo terminal")
    if not body.force:
        provider = _resolve(request, body.provider, None)
        if not isinstance(provider, JSONResponse):
            current = await provider.status()
            if current.status == "ok":
                return _error(
                    409,
                    "already_authenticated",
                    body.provider,
                    "um novo login apagaria a credencial atual",
                )
    try:
        session = await login.start(body.provider)
    except OSError as exc:
        return _error(502, "failed", body.provider, type(exc).__name__)
    return JSONResponse(session.state())


@router.get("/login/{provider}")
async def login_state(provider: str) -> JSONResponse:
    session = login.current(provider)
    if session is None:
        return JSONResponse({"provider": provider, "status": "none"})
    return JSONResponse(session.state())


@router.post("/login/cancel")
async def login_cancel(body: LoginRequest) -> JSONResponse:
    await login.cancel(body.provider)
    return JSONResponse({"provider": body.provider, "status": "none"})
