"""Provedor genérico por API key: qualquer serviço com `/chat/completions`
compatível com a OpenAI (OpenRouter, NVIDIA NIM, Groq, Gemini-compat…).

Configura-se só com `base_url` + `api_key` + `model`. O gateway não guarda
nada disto (D-030): o motor envia a configuração em cada chamada, e a chave
nunca aparece em logs nem em mensagens de erro.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

from app.config import settings
from app.providers.base import Completion, ErrorKind, Health, ProviderError, Status, Usage

STATUS_TIMEOUT_S = 20.0

# Um semáforo por nome de provedor, partilhado entre chamadas.
_slots: dict[str, asyncio.Semaphore] = {}


def _strip_fences(text: str) -> str:
    """Alguns modelos embrulham o JSON em ```json … ```."""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("\n", 1)[1] if "\n" in body else ""
        body = body.rsplit("```", 1)[0]
    return body.strip()


def _parse_object(text: str, schema: dict[str, Any]) -> dict[str, Any] | None:
    try:
        value = json.loads(_strip_fences(text))
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    # Nem todos os serviços impõem o esquema; aqui só se garante o nível de topo.
    # A validação completa é do chamador.
    required = schema.get("required")
    if isinstance(required, list) and any(key not in value for key in required):
        return None
    return value


class OpenAICompatibleProvider:
    def __init__(
        self,
        name: str,
        base_url: str,
        api_key: str,
        model: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self._api_key = api_key
        self._transport = transport

    def __repr__(self) -> str:  # a chave nunca entra num repr/traceback
        return f"OpenAICompatibleProvider(name={self.name!r}, base_url={self.base_url!r})"

    async def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        schema: dict[str, Any] | None = None,
        model: str | None = None,
        timeout_s: float,
    ) -> Completion:
        if settings.api_require_https and not self.base_url.startswith("https://"):
            raise ProviderError(self.name, ErrorKind.FAILED, "base_url tem de ser https")
        used_model = model or self.model
        slots = _slots.setdefault(self.name, asyncio.Semaphore(settings.api_concurrency))
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout_s), slots:
                text, usage = await self._chat(prompt, system, schema, used_model)
        except TimeoutError:
            raise ProviderError(self.name, ErrorKind.TIMEOUT, f"{timeout_s:g}s") from None
        except httpx.HTTPError as exc:
            # Só o tipo do erro: a mensagem pode conter o URL com credenciais.
            raise ProviderError(self.name, ErrorKind.FAILED, type(exc).__name__) from None
        if not text.strip():
            raise ProviderError(self.name, ErrorKind.EMPTY)
        data = None
        if schema is not None:
            data = _parse_object(text, schema)
            if data is None:
                raise ProviderError(self.name, ErrorKind.INVALID_OUTPUT, "sem objeto JSON válido")
        return Completion(
            provider=self.name,
            model=used_model,
            text=text,
            data=data,
            usage=usage,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    async def status(self) -> Status:
        """`GET /models` com a chave: barato e não gera texto."""
        if settings.api_require_https and not self.base_url.startswith("https://"):
            return Status(self.name, Health.ERROR, "base_url tem de ser https")
        try:
            async with (
                asyncio.timeout(STATUS_TIMEOUT_S),
                httpx.AsyncClient(
                    transport=self._transport, timeout=None, follow_redirects=False
                ) as client,
            ):
                res = await client.get(
                    f"{self.base_url}/models",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
        except TimeoutError:
            return Status(self.name, Health.ERROR, f"timeout {STATUS_TIMEOUT_S:g}s")
        except httpx.HTTPError as exc:
            return Status(self.name, Health.ERROR, type(exc).__name__)
        if res.status_code in (401, 403):
            return Status(self.name, Health.NOT_AUTHENTICATED, f"HTTP {res.status_code}")
        if res.status_code == 429:
            return Status(self.name, Health.RATE_LIMITED, "HTTP 429")
        if res.status_code >= 400:
            return Status(self.name, Health.ERROR, f"HTTP {res.status_code}")
        return Status(self.name, Health.OK)

    async def _chat(
        self, prompt: str, system: str, schema: dict[str, Any] | None, model: str
    ) -> tuple[str, Usage]:
        messages = []
        instructions = system.strip()
        if schema is not None:
            # O esquema vai também no texto: serviços sem `json_schema` ainda o veem.
            instructions = (
                f"{instructions}\n\nResponda apenas com um objeto JSON que cumpra este "
                f"JSON Schema, sem texto à volta:\n{json.dumps(schema, ensure_ascii=False)}"
            ).strip()
        if instructions:
            messages.append({"role": "system", "content": instructions})
        messages.append({"role": "user", "content": prompt})
        payload: dict[str, Any] = {"model": model, "messages": messages}

        formats: list[dict[str, Any] | None] = [None]
        if schema is not None:
            formats = [
                {
                    "type": "json_schema",
                    "json_schema": {"name": "output", "schema": schema, "strict": True},
                },
                # Serviços que rejeitam `json_schema` (HTTP 400) costumam aceitar isto.
                {"type": "json_object"},
            ]
        async with httpx.AsyncClient(
            transport=self._transport, timeout=None, follow_redirects=False
        ) as client:
            for index, response_format in enumerate(formats):
                body = payload | ({"response_format": response_format} if response_format else {})
                res = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=body,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                if res.status_code == 400 and index + 1 < len(formats):
                    continue
                return self._read(res)
        raise AssertionError("inalcançável")  # pragma: no cover

    def _read(self, res: httpx.Response) -> tuple[str, Usage]:
        if res.status_code in (401, 403):
            raise ProviderError(self.name, ErrorKind.NOT_AUTHENTICATED, f"HTTP {res.status_code}")
        if res.status_code == 429:
            raise ProviderError(self.name, ErrorKind.RATE_LIMITED, "HTTP 429")
        if res.status_code >= 400:
            # Um serviço pode devolver a chave na mensagem de erro: nunca a repetir.
            detail = res.text[:300].replace(self._api_key, "***")
            raise ProviderError(self.name, ErrorKind.FAILED, f"HTTP {res.status_code}: {detail}")
        try:
            body = res.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, LookupError, TypeError):
            raise ProviderError(self.name, ErrorKind.FAILED, "resposta sem choices") from None
        usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
        return (
            content if isinstance(content, str) else "",
            Usage(int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)),
        )
