"""Provedor genérico por API key, contra um servidor simulado (sem rede)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.providers.base import ErrorKind, ProviderError
from app.providers.openai_compat import OpenAICompatibleProvider
from tests.test_providers import SCHEMA

KEY = "sk-segredo-de-teste"
Handler = Callable[[httpx.Request], httpx.Response]


def chat(content: Any, usage: dict[str, int] | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": {"content": content}}],
            "usage": usage or {"prompt_tokens": 11, "completion_tokens": 4},
        },
    )


def provider(
    handler: Handler, base_url: str = "https://api.exemplo.test/v1/"
) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        "openrouter", base_url, KEY, "modelo-x", transport=httpx.MockTransport(handler)
    )


def run(coro):
    return asyncio.run(coro)


def error_of(coro) -> ProviderError:
    with pytest.raises(ProviderError) as info:
        run(coro)
    return info.value


def test_text_request_and_response() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return chat("Lisboa")

    result = run(provider(handler).complete("capital?", system="uma palavra", timeout_s=5))
    assert (result.provider, result.model, result.text, result.data) == (
        "openrouter",
        "modelo-x",
        "Lisboa",
        None,
    )
    assert (result.usage.input_tokens, result.usage.output_tokens) == (11, 4)
    request = seen[0]
    assert str(request.url) == "https://api.exemplo.test/v1/chat/completions"
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body == {
        "model": "modelo-x",
        "messages": [
            {"role": "system", "content": "uma palavra"},
            {"role": "user", "content": "capital?"},
        ],
    }


def test_model_override_and_no_system_message() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return chat("ok")

    result = run(provider(handler).complete("x", model="outro", timeout_s=5))
    assert result.model == "outro"
    assert bodies[0]["model"] == "outro"
    assert [m["role"] for m in bodies[0]["messages"]] == ["user"]


def test_structured_uses_json_schema() -> None:
    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return chat('```json\n{"titulo": "t"}\n```')

    result = run(provider(handler).complete("x", schema=SCHEMA, timeout_s=5))
    assert result.data == {"titulo": "t"}
    assert bodies[0]["response_format"]["type"] == "json_schema"
    assert bodies[0]["response_format"]["json_schema"]["schema"] == SCHEMA
    assert "JSON Schema" in bodies[0]["messages"][0]["content"]


def test_structured_falls_back_to_json_object_on_400() -> None:
    formats: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        kind = json.loads(request.content)["response_format"]["type"]
        formats.append(kind)
        if kind == "json_schema":
            return httpx.Response(400, json={"error": "response_format não suportado"})
        return chat('{"titulo": "t"}')

    result = run(provider(handler).complete("x", schema=SCHEMA, timeout_s=5))
    assert result.data == {"titulo": "t"}
    assert formats == ["json_schema", "json_object"]


@pytest.mark.parametrize(
    "content", ["não é JSON", "[1, 2]", '{"outro": 1}'], ids=["texto", "lista", "falta-required"]
)
def test_structured_invalid_output(content: str) -> None:
    err = error_of(provider(lambda _: chat(content)).complete("x", schema=SCHEMA, timeout_s=5))
    assert err.kind is ErrorKind.INVALID_OUTPUT


@pytest.mark.parametrize(
    ("response", "kind"),
    [
        (httpx.Response(401, json={"error": "bad key"}), ErrorKind.NOT_AUTHENTICATED),
        (httpx.Response(403), ErrorKind.NOT_AUTHENTICATED),
        (httpx.Response(429), ErrorKind.RATE_LIMITED),
        (httpx.Response(500, text="avaria"), ErrorKind.FAILED),
        (httpx.Response(200, json={"sem": "choices"}), ErrorKind.FAILED),
        (httpx.Response(200, text="<html>"), ErrorKind.FAILED),
        (chat(""), ErrorKind.EMPTY),
        (chat(None), ErrorKind.EMPTY),
    ],
)
def test_failures(response: httpx.Response, kind: ErrorKind) -> None:
    assert error_of(provider(lambda _: response).complete("x", timeout_s=5)).kind is kind


def test_timeout() -> None:
    async def slow(_: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        return chat("tarde")

    err = error_of(provider(slow).complete("x", timeout_s=0.3))
    assert err.kind is ErrorKind.TIMEOUT


def test_network_error_does_not_leak_details() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"falhou com {KEY}", request=request)

    err = error_of(provider(boom).complete("x", timeout_s=5))
    assert err.kind is ErrorKind.FAILED
    assert KEY not in str(err) and KEY not in err.detail


def test_key_never_in_repr_or_errors() -> None:
    p = provider(lambda _: httpx.Response(401, text=f"chave {KEY} inválida"))
    assert KEY not in repr(p)
    err = error_of(p.complete("x", timeout_s=5))
    assert KEY not in str(err)


def test_key_redacted_from_upstream_error_body() -> None:
    p = provider(lambda _: httpx.Response(500, text=f"erro ao usar {KEY}"))
    err = error_of(p.complete("x", timeout_s=5))
    assert err.kind is ErrorKind.FAILED
    assert KEY not in err.detail and "***" in err.detail


def test_https_required() -> None:
    called: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        called.append(1)
        return chat("x")

    err = error_of(provider(handler, "http://api.exemplo.test/v1").complete("x", timeout_s=5))
    assert err.kind is ErrorKind.FAILED and "https" in err.detail
    assert not called  # a chave nem chegou a sair


def test_concurrency_limit_per_provider_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "api_concurrency", 2)
    running = peak = 0

    async def handler(_: httpx.Request) -> httpx.Response:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.1)
        running -= 1
        return chat("ok")

    async def many() -> None:
        transport = httpx.MockTransport(handler)
        # Uma instância por pedido, como a API faz: o limite é pelo nome.
        await asyncio.gather(
            *(
                OpenAICompatibleProvider(
                    "limite-teste", "https://x.test", KEY, "m", transport=transport
                ).complete("x", timeout_s=5)
                for _ in range(6)
            )
        )

    run(many())
    assert peak == 2


# --- Pela API ----------------------------------------------------------------


@pytest.fixture
def api_transport() -> Iterator[Callable[[Handler], None]]:
    def set_handler(handler: Handler) -> None:
        app.state.api_transport = httpx.MockTransport(handler)

    yield set_handler
    app.state.api_transport = None


ENDPOINT = {"base_url": "https://api.exemplo.test/v1", "api_key": KEY, "model": "modelo-x"}


def test_api_complete_with_endpoint(client: TestClient, api_transport) -> None:
    api_transport(lambda _: chat("Lisboa"))
    res = client.post(
        "/v1/complete", json={"provider": "groq", "prompt": "capital?", "endpoint": ENDPOINT}
    )
    assert res.status_code == 200
    assert res.json()["provider"] == "groq"
    assert res.json()["text"] == "Lisboa"
    assert KEY not in res.text


def test_api_structured_with_endpoint(client: TestClient, api_transport) -> None:
    api_transport(lambda _: chat('{"titulo": "t"}'))
    res = client.post(
        "/v1/structured",
        json={"provider": "groq", "prompt": "x", "endpoint": ENDPOINT, "json_schema": SCHEMA},
    )
    assert res.status_code == 200
    assert res.json()["data"] == {"titulo": "t"}


def test_api_error_never_echoes_the_key(client: TestClient, api_transport) -> None:
    api_transport(lambda _: httpx.Response(401, text=f"chave {KEY} inválida"))
    res = client.post(
        "/v1/complete", json={"provider": "groq", "prompt": "x", "endpoint": ENDPOINT}
    )
    assert res.status_code == 503
    assert res.json()["error"] == "not_authenticated"
    assert KEY not in res.text


def test_api_validation_error_never_echoes_the_key(client: TestClient) -> None:
    bad = {"base_url": "https://x.test", "api_key": KEY}  # falta o modelo
    res = client.post("/v1/complete", json={"provider": "groq", "prompt": "x", "endpoint": bad})
    assert res.status_code == 422
    assert KEY not in res.text


def test_api_endpoint_cannot_take_a_subscription_name(client: TestClient, api_transport) -> None:
    api_transport(lambda _: chat("x"))
    res = client.post(
        "/v1/complete", json={"provider": "claude", "prompt": "x", "endpoint": ENDPOINT}
    )
    assert res.status_code == 400
    assert res.json()["error"] == "reserved_provider_name"


# --- Estado (E3.6) -------------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (httpx.Response(200, json={"data": []}), "ok"),
        (httpx.Response(401), "not_authenticated"),
        (httpx.Response(403), "not_authenticated"),
        (httpx.Response(429), "rate_limited"),
        (httpx.Response(500), "error"),
    ],
)
def test_status(response: httpx.Response, expected: str) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response

    result = run(provider(handler).status())
    assert result.status == expected
    assert seen[0].method == "GET" and seen[0].url.path == "/v1/models"
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"


def test_status_network_error_and_http() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"falhou com {KEY}", request=request)

    result = run(provider(boom).status())
    assert result.status == "error" and KEY not in result.detail
    plain = run(provider(lambda _: chat("x"), "http://api.exemplo.test/v1").status())
    assert plain.status == "error" and "https" in plain.detail


def test_api_status_with_endpoint(client: TestClient, api_transport) -> None:
    api_transport(lambda _: httpx.Response(401, text=f"chave {KEY} inválida"))
    res = client.post("/v1/status", json={"provider": "groq", "endpoint": ENDPOINT})
    assert res.status_code == 200
    assert res.json() == {"provider": "groq", "status": "not_authenticated", "detail": "HTTP 401"}
    assert KEY not in res.text
