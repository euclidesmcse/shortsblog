"""Login conduzido pelo painel (E3.7): fluxo de código de dispositivo.

Só o Codex tem um fluxo que funciona sem terminal (`codex login --device-auth`
imprime um URL e um código e fica à espera). O `agy` exige um terminal
interativo e o Claude usa o login copiado do servidor (D-029): para esses o
painel mostra a instrução do runbook em vez de um botão.

ATENÇÃO: `codex login` apaga a credencial existente logo ao arrancar, antes de o
utilizador autorizar. Por isso a API recusa iniciar um login quando o provedor
já está autenticado, salvo pedido explícito (`force`).

O processo de login fica vivo em segundo plano até o utilizador autorizar no
browser, falhar ou expirar. Há no máximo um por provedor.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import time
from dataclasses import dataclass, field

from app.config import settings
from app.providers.cli import cli_env

LOGIN_COMMANDS: dict[str, tuple[str, ...]] = {
    "codex": ("codex", "login", "--device-auth", "-c", 'cli_auth_credentials_store="file"'),
}

# O código do Codex expira em 15 minutos; depois disso o processo é morto.
MAX_AGE_S = 16 * 60
# Depois de terminar, o resultado fica visível este tempo e a sessão é descartada.
KEEP_FINISHED_S = 120
# Quanto se espera pelo URL e pelo código ao iniciar.
START_WAIT_S = 20.0

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_URL = re.compile(r"https://[^\s\"']+")
_CODE = re.compile(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,6}\b")


@dataclass
class LoginSession:
    provider: str
    proc: asyncio.subprocess.Process
    started: float = field(default_factory=time.monotonic)
    output: str = ""
    reader: asyncio.Task[None] | None = None
    finished: float | None = None

    @property
    def url(self) -> str | None:
        match = _URL.search(self.output)
        return match.group(0) if match else None

    @property
    def code(self) -> str | None:
        match = _CODE.search(self.output)
        return match.group(0) if match else None

    @property
    def status(self) -> str:
        if self.proc.returncode is None:
            return "running"
        return "succeeded" if self.proc.returncode == 0 else "failed"

    def state(self) -> dict[str, object]:
        # O endereço e o código só interessam enquanto o login está à espera.
        running = self.proc.returncode is None
        return {
            "provider": self.provider,
            "status": self.status,
            "url": self.url if running else None,
            "code": self.code if running else None,
            "age_s": int(time.monotonic() - self.started),
            "expires_in_s": max(0, int(MAX_AGE_S - (time.monotonic() - self.started))),
        }


_sessions: dict[str, LoginSession] = {}


def supported(provider: str) -> bool:
    return provider in LOGIN_COMMANDS


def current(provider: str) -> LoginSession | None:
    session = _sessions.get(provider)
    if (
        session is not None
        and session.finished is not None
        and time.monotonic() - session.finished > KEEP_FINISHED_S
    ):
        del _sessions[provider]
        return None
    return session


async def start(provider: str) -> LoginSession:
    """Inicia (ou reinicia) o login e espera pelo URL e pelo código."""
    await cancel(provider)
    binary, *argv = LOGIN_COMMANDS[provider]
    proc = await asyncio.create_subprocess_exec(
        binary,
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=settings.workdir,
        env=cli_env(),
        start_new_session=True,
    )
    session = LoginSession(provider, proc)
    session.reader = asyncio.create_task(_read(session))
    _sessions[provider] = session

    deadline = time.monotonic() + START_WAIT_S
    while time.monotonic() < deadline and proc.returncode is None:
        if session.url and session.code:
            break
        await asyncio.sleep(0.2)
    return session


async def cancel(provider: str) -> None:
    session = _sessions.pop(provider, None)
    if session is None:
        return
    _kill(session)
    if session.reader is not None:
        session.reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await session.reader
    # Recolhe o processo (sem isto ficava um zombie até ao fim do gateway).
    await session.proc.wait()


async def cancel_all() -> None:
    for provider in list(_sessions):
        await cancel(provider)


async def _read(session: LoginSession) -> None:
    assert session.proc.stdout is not None
    try:
        async with asyncio.timeout(MAX_AGE_S):
            while chunk := await session.proc.stdout.read(4096):
                text = _ANSI.sub("", chunk.decode(errors="replace"))
                # Só o fim interessa (URL, código, resultado); limita a memória.
                session.output = (session.output + text)[-8000:]
            await session.proc.wait()
    except TimeoutError:
        _kill(session)
        await session.proc.wait()
    finally:
        session.finished = time.monotonic()
        session.output = ""  # o código já não serve; não o guardar em memória


def _kill(session: LoginSession) -> None:
    if session.proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(session.proc.pid, signal.SIGKILL)
