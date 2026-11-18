"""Provedores por assinatura: cada chamada é um processo da CLI oficial.

Formatos de saída e flags verificados na E3.2 contra Claude Code 2.1.288,
Codex 0.160.0 e agy 1.2.16 (ADR 0003). Nenhuma chamada dá ferramentas ao modelo:
Claude corre com `--tools ""`; o Codex com `--sandbox read-only`; o agy em modo
`-p`, que nega o que pedir permissão. As flags `--dangerously-*` nunca são usadas.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import signal
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import settings
from app.providers.base import (
    Completion,
    ErrorKind,
    Health,
    Image,
    Provider,
    ProviderError,
    Status,
    Usage,
)

# Só isto passa para a CLI. Em particular ficam de fora ANTHROPIC_API_KEY,
# ANTHROPIC_AUTH_TOKEN e OPENAI_API_KEY, que trocariam a assinatura por
# faturação de API sem aviso.
ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "TZ",
    "LANG",
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "DISABLE_AUTOUPDATER",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
)

AGY_MAX_PROMPT_BYTES = 120_000

# Funcionalidades do Codex que dão ferramentas ao modelo. Por omissão vêm ligadas
# e incluem os conectores da conta ChatGPT (Gmail, Drive, GitHub, Calendar…), que
# correm do lado da OpenAI e por isso NÃO são travados pelo isolamento do
# container (revisão E3, bloqueante #1). O gateway só quer texto e desliga o que
# consegue. Nomes de `codex features list` (0.160.0); ao atualizar o Codex, rever.
#
# O que estas flags NÃO desligam (medido em 2026-10-03, ADR 0003):
# - `unified_exec` continua ativo apesar do `--disable`: a ferramenta `exec` é
#   anunciada ao modelo. Trava-a o sandbox: o `bwrap` não cria namespaces dentro
#   do container e nenhum comando arranca (`tests/isolation.sh` verifica-o).
# - `apply_patch` não tem funcionalidade própria. Trava-a `--sandbox read-only`:
#   "patch rejected: writing is blocked by read-only sandbox".
# - `multi_agent` fica desligado mas `spawn_agent` continua anunciado; falha por
#   causa do `--ephemeral` (não há histórico de onde o subagente arrancar).
# Ou seja: a barreira para estas três é o sandbox e o `--ephemeral`, não esta lista.
CODEX_DISABLED_FEATURES = (
    "apps",
    "plugins",
    "remote_plugin",
    "plugin_sharing",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "image_generation",
    "view_image",
    "in_app_browser",
    "in_app_local_automation",
    "multi_agent",
    "shell_tool",
    "unified_exec",
    "unified_exec_tty",
    "unified_exec_zsh_fork",
    "hooks",
    "goals",
    "skill_search",
    "skill_mcp_dependency_install",
    "tool_suggest",
    "sleep_tool",
    "worktrees",
)

# Chamada de imagem: as mesmas travas, só com `image_generation` ligada (D-056). `view_image`
# continua desligada: o modelo não precisa de ler arquivos para criar uma imagem.
CODEX_IMAGE_DISABLED_FEATURES = tuple(f for f in CODEX_DISABLED_FEATURES if f != "image_generation")
IMAGE_SUFFIXES = (".png", ".webp", ".jpg", ".jpeg")
IMAGE_MAX_BYTES = 15_000_000
IMAGE_INSTRUCTIONS = (
    "Use your image generation tool exactly once to create one image for the description "
    "below. Do not run commands, do not read or write files, do not ask questions. "
    "When the image is created, answer only with the word done.\n\nDescription: "
)

_NOT_AUTH = re.compile(
    r"not logged in|authentication required|please run /login|please sign in|invalid api key|"
    r"login expired|unauthorized|\b401\b|failed to authenticate|oauth session expired|"
    r"could not be refreshed|oauth token has expired",
    re.IGNORECASE,
)
_RATE = re.compile(
    r"rate.?limit|usage limit|quota|too many requests|\b429\b|resource_exhausted",
    re.IGNORECASE,
)


NOT_INSTALLED = "CLI não instalada nesta imagem (a imagem pública traz só o Codex e a API)"


@dataclass(frozen=True)
class ProcResult:
    returncode: int
    stdout: str
    stderr: str


def cli_env() -> dict[str, str]:
    return {k: v for k in ENV_ALLOWLIST if (v := os.environ.get(k)) is not None}


def with_system(system: str, prompt: str) -> str:
    """Para CLIs sem flag de prompt de sistema: instruções à frente do pedido."""
    return f"{system.strip()}\n\n---\n\n{prompt}" if system.strip() else prompt


class SubscriptionCLIProvider:
    name: str = ""

    def __init__(self, binary: str, model: str, concurrency: int) -> None:
        self.binary = binary
        self.model = model
        self._slots = asyncio.Semaphore(concurrency)

    async def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        schema: dict[str, Any] | None = None,
        model: str | None = None,
        timeout_s: float,
    ) -> Completion:
        if len(prompt.encode()) + len(system.encode()) > settings.max_prompt_bytes:
            raise ProviderError(self.name, ErrorKind.FAILED, "prompt demasiado grande")
        started = time.monotonic()
        try:
            # O tempo na fila conta para o limite: o chamador espera no total `timeout_s`.
            async with asyncio.timeout(timeout_s):
                async with self._slots:
                    result = await self._call(prompt, system, schema, model or self.model)
        except TimeoutError:
            raise ProviderError(self.name, ErrorKind.TIMEOUT, f"{timeout_s:g}s") from None
        if result.data is None and not result.text.strip():
            raise ProviderError(self.name, ErrorKind.EMPTY)
        if schema is not None and result.data is None:
            raise ProviderError(self.name, ErrorKind.INVALID_OUTPUT, "sem objeto JSON")
        return Completion(
            provider=self.name,
            model=result.model,
            text=result.text,
            data=result.data,
            usage=result.usage,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    async def _call(
        self, prompt: str, system: str, schema: dict[str, Any] | None, model: str
    ) -> Completion:
        raise NotImplementedError

    # Comando barato de estado e limite de tempo; cada CLI define os seus.
    status_argv: tuple[str, ...] = ()
    status_timeout_s: float = 30

    async def status(self) -> Status:
        """Estado da credencial sem chamar o modelo (não passa pelo semáforo)."""
        try:
            async with asyncio.timeout(self.status_timeout_s):
                res = await self._run(list(self.status_argv))
        except TimeoutError:
            return Status(self.name, Health.ERROR, f"timeout {self.status_timeout_s:g}s")
        except ProviderError as exc:
            return Status(self.name, Health.ERROR, exc.detail)
        except OSError as exc:
            return Status(self.name, Health.ERROR, type(exc).__name__)
        return self._read_status(res)

    def _read_status(self, res: ProcResult) -> Status:
        raise NotImplementedError

    def _status_from_text(self, res: ProcResult, ok: bool) -> Status:
        output = res.stdout + res.stderr
        if _NOT_AUTH.search(output):
            return Status(self.name, Health.NOT_AUTHENTICATED)
        if ok and res.returncode == 0:
            return Status(self.name, Health.OK)
        return Status(self.name, Health.ERROR, output.strip()[-300:])

    async def _run(self, argv: list[str], stdin: str | None = None) -> ProcResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                self.binary,
                *argv,
                stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=settings.workdir,
                env=cli_env(),
                # Grupo próprio: no timeout morre a CLI e tudo o que ela lançou.
                start_new_session=True,
            )
        except FileNotFoundError:
            # E19.6 (D-112): a imagem pública não traz as CLIs proprietárias (Claude Code, agy).
            # Erro tipado em vez de 500: o motor pula o provedor e segue a cadeia.
            raise ProviderError(self.name, ErrorKind.FAILED, NOT_INSTALLED) from None
        try:
            out, err = await proc.communicate(stdin.encode() if stdin is not None else None)
        except asyncio.CancelledError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await proc.wait()
            raise
        return ProcResult(
            returncode=proc.returncode or 0,
            stdout=out.decode(errors="replace"),
            stderr=err.decode(errors="replace"),
        )

    def _classify(self, text: str) -> ProviderError:
        """Erro tipado a partir do que a CLI escreveu (heurística: ADR 0003)."""
        tail = text.strip()[-500:]
        if _NOT_AUTH.search(text):
            return ProviderError(self.name, ErrorKind.NOT_AUTHENTICATED, tail)
        if _RATE.search(text):
            return ProviderError(self.name, ErrorKind.RATE_LIMITED, tail)
        return ProviderError(self.name, ErrorKind.FAILED, tail)

    def _result(
        self, model: str, text: str, data: Any, input_tokens: Any, output_tokens: Any
    ) -> Completion:
        return Completion(
            provider=self.name,
            model=model,
            text=text if isinstance(text, str) else "",
            data=data if isinstance(data, dict) else None,
            usage=Usage(int(input_tokens or 0), int(output_tokens or 0)),
            duration_ms=0,
        )


def _json_object(raw: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


class ClaudeProvider(SubscriptionCLIProvider):
    name = "claude"
    status_argv = ("auth", "status")

    def _read_status(self, res: ProcResult) -> Status:
        out = _json_object(res.stdout)
        if out is None:
            return self._status_from_text(res, ok=False)
        if out.get("loggedIn") is True:
            return Status(self.name, Health.OK)
        return Status(self.name, Health.NOT_AUTHENTICATED)

    async def _call(
        self, prompt: str, system: str, schema: dict[str, Any] | None, model: str
    ) -> Completion:
        argv = [
            "-p",
            "--tools",
            "",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--setting-sources",
            "",
            # Sem isto a CLI usa o seu prompt de agente de programação.
            "--system-prompt",
            system.strip() or "Responda ao pedido do usuário.",
        ]
        if model:
            argv += ["--model", model]
        if schema is not None:
            argv += ["--json-schema", json.dumps(schema)]
        res = await self._run(argv, stdin=prompt)
        out = _json_object(res.stdout)
        if out is None:
            raise self._classify(res.stderr + res.stdout)
        if out.get("is_error") or res.returncode != 0:
            raise self._classify(str(out.get("result") or "") + res.stderr)
        usage = out.get("usage") or {}
        input_tokens = sum(
            int(usage.get(k) or 0)
            for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        )
        used = next(iter(out.get("modelUsage") or {}), model)
        return self._result(
            used,
            out.get("result") or "",
            out.get("structured_output"),
            input_tokens,
            usage.get("output_tokens"),
        )


class CodexProvider(SubscriptionCLIProvider):
    name = "codex"
    status_argv = ("login", "status", "-c", 'cli_auth_credentials_store="file"')

    def _read_status(self, res: ProcResult) -> Status:
        return self._status_from_text(res, ok="logged in" in (res.stdout + res.stderr).lower())

    @staticmethod
    def _base_argv(disabled: tuple[str, ...]) -> list[str]:
        argv = [
            "exec",
            "--json",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "-c",
            'cli_auth_credentials_store="file"',
            # Nada do config.toml do volume pode voltar a ligar o que se desliga abaixo.
            "--ignore-user-config",
            "--ignore-rules",
            "-c",
            'web_search="disabled"',
        ]
        for feature in disabled:
            argv += ["--disable", feature]
        return argv

    async def image(self, prompt: str, *, timeout_s: float) -> Image:
        """Uma imagem pela ferramenta de geração do Codex (assinatura ChatGPT, D-056)."""
        if len(prompt.encode()) > settings.max_image_prompt_bytes:
            raise ProviderError(self.name, ErrorKind.FAILED, "prompt de imagem demasiado grande")
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout_s):
                async with self._slots:
                    found = await self._image_call(prompt)
        except TimeoutError:
            raise ProviderError(self.name, ErrorKind.TIMEOUT, f"{timeout_s:g}s") from None
        return Image(
            provider=self.name,
            model=found.model,
            data=found.data,
            usage=found.usage,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    async def _image_call(self, prompt: str) -> Image:
        argv = self._base_argv(CODEX_IMAGE_DISABLED_FEATURES)
        if self.model:
            argv += ["--model", self.model]
        res = await self._run([*argv, "-"], stdin=IMAGE_INSTRUCTIONS + prompt)
        thread = ""
        usage: dict[str, Any] = {}
        errors: list[str] = []
        for line in res.stdout.splitlines():
            event = _json_object(line)
            if event is None:
                continue
            if event.get("type") == "thread.started":
                thread = str(event.get("thread_id") or "")
            elif event.get("type") == "turn.completed":
                usage = event.get("usage") or {}
            elif event.get("type") in ("error", "turn.failed"):
                errors.append(json.dumps(event))
        if errors or res.returncode != 0:
            raise self._classify("\n".join(errors) + res.stderr)
        data = self._take_image(thread)
        return Image(
            provider=self.name,
            model=self.model or "codex",
            data=data,
            usage=Usage(int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)),
            duration_ms=0,
        )

    def _take_image(self, thread: str) -> bytes:
        """Lê (e apaga) o arquivo que o Codex gravou para este pedido."""
        # O id vem da saída da CLI: só serve como nome de pasta se for um identificador simples.
        if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", thread):
            raise ProviderError(self.name, ErrorKind.INVALID_OUTPUT, "sem identificador do pedido")
        folder = Path(settings.codex_image_dir) / thread
        files = sorted(
            (f for f in folder.glob("*") if f.is_file() and f.suffix.lower() in IMAGE_SUFFIXES),
            key=lambda f: f.stat().st_mtime,
        )
        if not files:
            raise ProviderError(self.name, ErrorKind.EMPTY, "o Codex não gerou nenhuma imagem")
        try:
            if files[-1].stat().st_size > IMAGE_MAX_BYTES:
                raise ProviderError(self.name, ErrorKind.INVALID_OUTPUT, "imagem grande demais")
            return files[-1].read_bytes()
        finally:
            for f in files:
                with contextlib.suppress(OSError):
                    f.unlink()
            with contextlib.suppress(OSError):
                folder.rmdir()

    async def _call(
        self, prompt: str, system: str, schema: dict[str, Any] | None, model: str
    ) -> Completion:
        argv = self._base_argv(CODEX_DISABLED_FEATURES)
        if model:
            argv += ["--model", model]
        schema_path = None
        try:
            if schema is not None:
                with tempfile.NamedTemporaryFile(
                    "w", suffix=".json", prefix="schema-", delete=False
                ) as fh:
                    json.dump(schema, fh)
                    schema_path = fh.name
                argv += ["--output-schema", schema_path]
            # "-": o prompt vem do stdin.
            res = await self._run([*argv, "-"], stdin=with_system(system, prompt))
        finally:
            if schema_path:
                with contextlib.suppress(OSError):
                    os.unlink(schema_path)

        text = ""
        usage: dict[str, Any] = {}
        errors: list[str] = []
        for line in res.stdout.splitlines():
            event = _json_object(line)
            if event is None:
                continue
            kind = event.get("type")
            item = event.get("item") or {}
            if kind == "item.completed" and item.get("type") == "agent_message":
                text = str(item.get("text") or "")
            elif kind == "turn.completed":
                usage = event.get("usage") or {}
            elif kind in ("error", "turn.failed"):
                errors.append(json.dumps(event))
        if errors or res.returncode != 0:
            raise self._classify("\n".join(errors) + res.stderr)
        return self._result(
            model,
            text,
            _json_object(text) if schema is not None else None,
            usage.get("input_tokens"),
            usage.get("output_tokens"),
        )


class AgyProvider(SubscriptionCLIProvider):
    name = "agy"
    # Não há comando de estado: listar modelos exige login e não chama nenhum modelo.
    status_argv = ("models",)

    def _read_status(self, res: ProcResult) -> Status:
        # Sem login o agy sai com 0 e uma mensagem de erro: só conta como bom se
        # vier pelo menos uma linha de modelo ("id<TAB>nome").
        has_models = any("\t" in line for line in res.stdout.splitlines())
        return self._status_from_text(res, ok=has_models)

    async def _call(
        self, prompt: str, system: str, schema: dict[str, Any] | None, model: str
    ) -> Completion:
        full = with_system(system, prompt)
        # O agy só aceita o prompt como argumento (limite do kernel: 128 KiB por argumento).
        if len(full.encode()) > AGY_MAX_PROMPT_BYTES:
            raise ProviderError(self.name, ErrorKind.FAILED, "prompt demasiado grande para o agy")
        argv = ["-p", full, "--output-format", "json", "--sandbox"]
        if model:
            argv += ["--model", model]
        if schema is not None:
            argv += ["--json-schema", json.dumps(schema)]
        res = await self._run(argv)
        # Avisos podem vir antes do JSON; o resultado é a última linha com um objeto.
        out = next(
            (o for line in reversed(res.stdout.splitlines()) if (o := _json_object(line))),
            None,
        )
        if out is None:
            raise self._classify(res.stderr + res.stdout)
        if out.get("status") != "SUCCESS" or res.returncode != 0:
            raise self._classify(json.dumps(out) + res.stderr)
        usage = out.get("usage") or {}
        return self._result(
            model,
            out.get("response") or "",
            out.get("structured_output"),
            usage.get("input_tokens"),
            usage.get("output_tokens"),
        )


def build_providers() -> dict[str, Provider]:
    return {
        "claude": ClaudeProvider("claude", settings.claude_model, settings.claude_concurrency),
        "codex": CodexProvider("codex", settings.codex_model, settings.codex_concurrency),
        "agy": AgyProvider("agy", settings.agy_model, settings.agy_concurrency),
    }
