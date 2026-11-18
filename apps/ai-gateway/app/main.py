"""AI Gateway — API interna para os provedores de IA (E3.2–E3.4)."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import login
from app.api import router
from app.providers.cli import build_providers


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    yield
    # Não deixar processos de login vivos ao encerrar.
    await login.cancel_all()


app = FastAPI(
    title="ShortsBlog AI Gateway",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)
app.state.providers = build_providers()
app.state.api_transport = None  # só os testes o substituem
app.include_router(router)


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    # O 422 por omissão devolve o `input` recebido, que pode trazer uma API key.
    errors = [{k: e[k] for k in ("type", "loc", "msg") if k in e} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


CLIS = ("claude", "codex", "agy")
VERSION_TIMEOUT_S = 15


async def cli_version(name: str) -> str | None:
    """Versão reportada pela CLI, ou None se não existir ou não responder."""
    path = shutil.which(name)
    if path is None:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            path,
            "--version",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), VERSION_TIMEOUT_S)
    except (OSError, TimeoutError):
        return None
    if proc.returncode != 0:
        return None
    return out.decode(errors="replace").strip().splitlines()[0] if out.strip() else None


@app.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/clis")
async def clis() -> dict[str, str | None]:
    versions = await asyncio.gather(*(cli_version(name) for name in CLIS))
    return dict(zip(CLIS, versions, strict=True))
