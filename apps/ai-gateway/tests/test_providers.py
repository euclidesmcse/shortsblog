"""Provedores CLI: argumentos, leitura da saída e classificação de falhas."""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import pytest

from app.config import settings
from app.providers.base import ErrorKind, Health, ProviderError
from app.providers.cli import (
    AGY_MAX_PROMPT_BYTES,
    CODEX_DISABLED_FEATURES,
    CODEX_IMAGE_DISABLED_FEATURES,
    NOT_INSTALLED,
    AgyProvider,
    ClaudeProvider,
    CodexProvider,
    cli_env,
    with_system,
)
from tests.conftest import FakeCLI

SCHEMA = {
    "type": "object",
    "properties": {"titulo": {"type": "string"}},
    "required": ["titulo"],
    "additionalProperties": False,
}

# Regista argv, stdin, cwd e ambiente num ficheiro ao lado do script.
RECORD = """
rec = {"argv": sys.argv[1:], "stdin": sys.stdin.read(), "cwd": os.getcwd(), "env": dict(os.environ)}
open(sys.argv[0] + ".rec", "w").write(json.dumps(rec))
"""

CLAUDE_OK = (
    RECORD
    + """
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
    "result": "pronto", "structured_output": {"titulo": "t"} if "--json-schema" in sys.argv else None,
    "usage": {"input_tokens": 10, "cache_creation_input_tokens": 5, "cache_read_input_tokens": 2,
              "output_tokens": 7},
    "modelUsage": {"claude-haiku-4-5": {}}}))
"""
)

CODEX_OK = (
    RECORD
    + """
schema = "--output-schema" in sys.argv
if schema:
    path = sys.argv[sys.argv.index("--output-schema") + 1]
    open(sys.argv[0] + ".schema", "w").write(open(path).read())
print(json.dumps({"type": "thread.started", "thread_id": "x"}))
print("linha que não é JSON")
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message",
    "text": json.dumps({"titulo": "t"}) if schema else "pronto"}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 9}}))
"""
)

AGY_OK = (
    RECORD
    + """
print("aviso antes do JSON")
print(json.dumps({"status": "SUCCESS", "response": "pronto\\n",
    "structured_output": {"titulo": "t"} if "--json-schema" in sys.argv else None,
    "usage": {"input_tokens": 50, "output_tokens": 3}}))
"""
)


def recorded(binary: str) -> dict:
    return json.loads(Path(binary + ".rec").read_text())


def run(coro):
    return asyncio.run(coro)


# --- Caminho feliz -----------------------------------------------------------


def test_claude_text(fake_cli: FakeCLI, workdir: Path) -> None:
    binary = fake_cli(CLAUDE_OK)
    result = run(
        ClaudeProvider(binary, "haiku", 1).complete("olá", system="sê breve", timeout_s=10)
    )
    assert (result.provider, result.text, result.data) == ("claude", "pronto", None)
    assert result.model == "claude-haiku-4-5"
    # entrada = tokens novos + cache criada + cache lida
    assert (result.usage.input_tokens, result.usage.output_tokens) == (17, 7)
    rec = recorded(binary)
    argv = rec["argv"]
    assert rec["stdin"] == "olá"
    assert rec["cwd"] == str(workdir)
    assert argv[argv.index("--tools") + 1] == ""  # sem ferramentas
    assert argv[argv.index("--system-prompt") + 1] == "sê breve"
    assert argv[argv.index("--model") + 1] == "haiku"
    assert "--bare" not in argv
    assert not any(a.startswith("--dangerously") for a in argv)


def test_claude_structured(fake_cli: FakeCLI) -> None:
    binary = fake_cli(CLAUDE_OK)
    result = run(ClaudeProvider(binary, "", 1).complete("x", schema=SCHEMA, timeout_s=10))
    assert result.data == {"titulo": "t"}
    argv = recorded(binary)["argv"]
    assert json.loads(argv[argv.index("--json-schema") + 1]) == SCHEMA
    assert "--model" not in argv


def test_codex_text(fake_cli: FakeCLI) -> None:
    binary = fake_cli(CODEX_OK)
    result = run(CodexProvider(binary, "", 1).complete("olá", system="regras", timeout_s=10))
    assert (result.text, result.data) == ("pronto", None)
    assert (result.usage.input_tokens, result.usage.output_tokens) == (100, 9)
    rec = recorded(binary)
    argv = rec["argv"]
    assert argv[0] == "exec" and argv[-1] == "-"
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert "--ephemeral" in argv and "--skip-git-repo-check" in argv
    # Revisão E3 #1: nenhuma funcionalidade que dê ferramentas ao modelo, em
    # especial os conectores da conta ChatGPT (apps/plugins).
    disabled = {argv[i + 1] for i, a in enumerate(argv) if a == "--disable"}
    assert {
        "apps",
        "plugins",
        "remote_plugin",
        "browser_use",
        "computer_use",
        "image_generation",
        "shell_tool",
        "unified_exec",
        "multi_agent",
    } <= disabled
    assert disabled == set(CODEX_DISABLED_FEATURES)
    assert "--enable" not in argv
    assert "--ignore-user-config" in argv and "--ignore-rules" in argv
    assert 'web_search="disabled"' in argv
    assert not any(a.startswith("--dangerously") for a in argv)
    assert rec["stdin"] == with_system("regras", "olá")


def test_codex_structured_writes_and_removes_schema_file(fake_cli: FakeCLI) -> None:
    binary = fake_cli(CODEX_OK)
    result = run(CodexProvider(binary, "", 1).complete("x", schema=SCHEMA, timeout_s=10))
    assert result.data == {"titulo": "t"}
    assert json.loads(Path(binary + ".schema").read_text()) == SCHEMA
    argv = recorded(binary)["argv"]
    assert not os.path.exists(argv[argv.index("--output-schema") + 1])


def test_agy_text_and_default_model(fake_cli: FakeCLI) -> None:
    binary = fake_cli(AGY_OK)
    result = run(AgyProvider(binary, "gemini-x", 1).complete("olá", timeout_s=10))
    assert (result.text, result.model) == ("pronto\n", "gemini-x")
    assert (result.usage.input_tokens, result.usage.output_tokens) == (50, 3)
    argv = recorded(binary)["argv"]
    assert argv[:2] == ["-p", "olá"]
    assert "--sandbox" in argv
    assert argv[argv.index("--model") + 1] == "gemini-x"
    assert not any(a.startswith("--dangerously") for a in argv)


def test_agy_structured_and_model_override(fake_cli: FakeCLI) -> None:
    binary = fake_cli(AGY_OK)
    result = run(
        AgyProvider(binary, "gemini-x", 1).complete("x", schema=SCHEMA, model="outro", timeout_s=10)
    )
    assert result.data == {"titulo": "t"}
    argv = recorded(binary)["argv"]
    assert argv[argv.index("--model") + 1] == "outro"


# --- Ambiente passado às CLIs ------------------------------------------------


def test_api_keys_never_reach_the_cli(fake_cli: FakeCLI, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "SEGREDO_QUALQUER"):
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("CODEX_HOME", "/state/codex")
    binary = fake_cli(CLAUDE_OK)
    run(ClaudeProvider(binary, "", 1).complete("x", timeout_s=10))
    env = recorded(binary)["env"]
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "OPENAI_API_KEY" not in env
    assert "SEGREDO_QUALQUER" not in env
    assert env["CODEX_HOME"] == "/state/codex"
    assert "PATH" in cli_env()


# --- Falhas ------------------------------------------------------------------


def error_of(coro) -> ProviderError:
    with pytest.raises(ProviderError) as info:
        run(coro)
    return info.value


def test_timeout_kills_the_process_group(fake_cli: FakeCLI, tmp_path: Path) -> None:
    marker = tmp_path / "sobreviveu"
    binary = fake_cli(
        f"""
        import subprocess
        # Um filho que escreveria o marcador se sobrevivesse ao timeout.
        subprocess.Popen(["sh", "-c", "sleep 2; touch {marker}"])
        time.sleep(30)
        """
    )
    started = time.monotonic()
    err = error_of(ClaudeProvider(binary, "", 1).complete("x", timeout_s=0.5))
    assert err.kind is ErrorKind.TIMEOUT
    assert time.monotonic() - started < 5
    time.sleep(2.5)
    assert not marker.exists()


def test_queue_wait_counts_towards_the_timeout(fake_cli: FakeCLI) -> None:
    slow = fake_cli("time.sleep(3)\n" + CLAUDE_OK)
    provider = ClaudeProvider(slow, "", 1)

    async def both() -> list[object]:
        return await asyncio.gather(
            provider.complete("a", timeout_s=10),
            provider.complete("b", timeout_s=1),
            return_exceptions=True,
        )

    first, second = run(both())
    assert getattr(first, "text", None) == "pronto"
    assert isinstance(second, ProviderError) and second.kind is ErrorKind.TIMEOUT


def test_concurrency_limit(fake_cli: FakeCLI, tmp_path: Path) -> None:
    log = tmp_path / "log"
    body = (
        f'open({str(log)!r}, "a").write("in\\n")\n'
        "time.sleep(0.4)\n"
        f'open({str(log)!r}, "a").write("out\\n")\n'
    )
    binary = fake_cli(body + CLAUDE_OK)
    provider = ClaudeProvider(binary, "", 2)

    async def many() -> None:
        await asyncio.gather(*(provider.complete("x", timeout_s=20) for _ in range(5)))

    run(many())
    running = peak = 0
    for line in log.read_text().split():
        running += 1 if line == "in" else -1
        peak = max(peak, running)
    assert peak == 2


def test_empty_response(fake_cli: FakeCLI) -> None:
    binary = fake_cli('print(json.dumps({"is_error": False, "result": "  "}))')
    assert (
        error_of(ClaudeProvider(binary, "", 1).complete("x", timeout_s=10)).kind is ErrorKind.EMPTY
    )


def test_agy_denied_tool_is_empty(fake_cli: FakeCLI) -> None:
    binary = fake_cli(
        'print(json.dumps({"status": "SUCCESS", "response": "", '
        '"denied_actions": [{"action": "command"}]}))'
    )
    assert error_of(AgyProvider(binary, "", 1).complete("x", timeout_s=10)).kind is ErrorKind.EMPTY


def test_structured_without_object_is_invalid(fake_cli: FakeCLI) -> None:
    binary = fake_cli(
        'print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", '
        '"text": "isto não é JSON"}}))'
    )
    err = error_of(CodexProvider(binary, "", 1).complete("x", schema=SCHEMA, timeout_s=10))
    assert err.kind is ErrorKind.INVALID_OUTPUT


@pytest.mark.parametrize(
    ("provider_cls", "body", "kind"),
    [
        (
            ClaudeProvider,
            'print(json.dumps({"is_error": True, "result": "Not logged in · Please run /login"}))\n'
            "sys.exit(1)",
            ErrorKind.NOT_AUTHENTICATED,
        ),
        (
            ClaudeProvider,
            'print(json.dumps({"is_error": True, "result": "API Error: 429 rate_limit_error"}))\n'
            "sys.exit(1)",
            ErrorKind.RATE_LIMITED,
        ),
        (
            # Mensagem real do Claude Code 2.1.288 quando a sessão copiada do servidor caduca
            # (R27, visto em 2026-10-04): tem de contar como falta de login, não como falha.
            ClaudeProvider,
            'print(json.dumps({"is_error": True, "result": "Failed to authenticate: OAuth '
            'session expired and could not be refreshed"}))\nsys.exit(1)',
            ErrorKind.NOT_AUTHENTICATED,
        ),
        (ClaudeProvider, 'print("erro sem JSON"); sys.exit(2)', ErrorKind.FAILED),
        (
            CodexProvider,
            'sys.stderr.write("Not logged in\\n"); sys.exit(1)',
            ErrorKind.NOT_AUTHENTICATED,
        ),
        (
            CodexProvider,
            'print(json.dumps({"type": "error", "message": "You have hit your usage limit"}))\n'
            "sys.exit(1)",
            ErrorKind.RATE_LIMITED,
        ),
        (
            AgyProvider,
            'print("Authentication required. Please visit the URL to log in:"); sys.exit(1)',
            ErrorKind.NOT_AUTHENTICATED,
        ),
        (
            AgyProvider,
            'print(json.dumps({"status": "ERROR", "error": "RESOURCE_EXHAUSTED: quota"}))',
            ErrorKind.RATE_LIMITED,
        ),
        (AgyProvider, 'print(json.dumps({"status": "ERROR"}))', ErrorKind.FAILED),
    ],
)
def test_failure_classification(
    fake_cli: FakeCLI, provider_cls: type, body: str, kind: ErrorKind
) -> None:
    assert error_of(provider_cls(fake_cli(body), "", 1).complete("x", timeout_s=10)).kind is kind


def test_prompt_size_limits(fake_cli: FakeCLI) -> None:
    binary = fake_cli(AGY_OK)
    big = "a" * (AGY_MAX_PROMPT_BYTES + 1)
    assert error_of(AgyProvider(binary, "", 1).complete(big, timeout_s=10)).kind is ErrorKind.FAILED
    assert not Path(binary + ".rec").exists()  # nem chegou a lançar a CLI
    huge = "a" * 400_001
    assert (
        error_of(ClaudeProvider(binary, "", 1).complete(huge, timeout_s=10)).kind
        is ErrorKind.FAILED
    )


# --- Imagem pelo Codex (D-056) ----------------------------------------------------------------

THREAD = "01a112b3-560a-7b02-8215-080ad0abd8f0"
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 64


def _codex_image(fake_cli: FakeCLI, images: Path, *, write: bool = True, fail: bool = False) -> str:
    return fake_cli(
        RECORD
        + f"""
print(json.dumps({{"type": "thread.started", "thread_id": "{THREAD}"}}))
if {write!r}:
    folder = os.path.join({str(images)!r}, "{THREAD}")
    os.makedirs(folder)
    open(os.path.join(folder, "exec-1.png"), "wb").write({PNG!r})
if {fail!r}:
    print(json.dumps({{"type": "turn.failed", "error": {{"message": "usage limit reached"}}}}))
    sys.exit(1)
print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 300, "output_tokens": 20}}}}))
"""
    )


def test_codex_image_devolve_o_ficheiro_e_apaga_o_do_volume(
    fake_cli: FakeCLI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    images = tmp_path / "images"
    monkeypatch.setattr(settings, "codex_image_dir", str(images))
    binary = _codex_image(fake_cli, images)
    result = run(CodexProvider(binary, "", 1).image("blue cubes", timeout_s=10))
    assert result.data == PNG and result.usage.input_tokens == 300
    assert not (images / THREAD).exists()  # o gateway não deixa imagens no volume
    rec = recorded(binary)
    argv = rec["argv"]
    # Só `image_generation` fica ligada; as outras travas (revisão E3 #1) mantêm-se.
    disabled = {argv[i + 1] for i, a in enumerate(argv) if a == "--disable"}
    assert disabled == set(CODEX_IMAGE_DISABLED_FEATURES)
    assert "image_generation" not in disabled and {"apps", "plugins", "shell_tool"} <= disabled
    assert argv[argv.index("--sandbox") + 1] == "read-only" and "--ephemeral" in argv
    assert rec["stdin"].endswith("Description: blue cubes")


def test_codex_image_sem_ficheiro_e_vazio(
    fake_cli: FakeCLI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "codex_image_dir", str(tmp_path / "images"))
    binary = _codex_image(fake_cli, tmp_path / "images", write=False)
    with pytest.raises(ProviderError) as exc:
        run(CodexProvider(binary, "", 1).image("x", timeout_s=10))
    assert exc.value.kind is ErrorKind.EMPTY


def test_codex_image_cota_esgotada_e_classificada(
    fake_cli: FakeCLI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "codex_image_dir", str(tmp_path / "images"))
    binary = _codex_image(fake_cli, tmp_path / "images", write=False, fail=True)
    with pytest.raises(ProviderError) as exc:
        run(CodexProvider(binary, "", 1).image("x", timeout_s=10))
    assert exc.value.kind is ErrorKind.RATE_LIMITED


def test_codex_image_ignora_id_de_pedido_fora_do_padrao(
    fake_cli: FakeCLI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O id vem da saída da CLI: nunca vira um caminho fora da pasta das imagens."""
    monkeypatch.setattr(settings, "codex_image_dir", str(tmp_path / "images"))
    binary = fake_cli(
        'print(json.dumps({"type": "thread.started", "thread_id": "../../etc"}))\n'
        'print(json.dumps({"type": "turn.completed", "usage": {}}))'
    )
    with pytest.raises(ProviderError) as exc:
        run(CodexProvider(binary, "", 1).image("x", timeout_s=10))
    assert exc.value.kind is ErrorKind.INVALID_OUTPUT


def test_codex_image_prompt_grande_demais(fake_cli: FakeCLI) -> None:
    with pytest.raises(ProviderError):
        run(CodexProvider(fake_cli("pass"), "", 1).image("x" * 5000, timeout_s=10))


# --- E19.6 (D-112): imagem pública sem as CLIs proprietárias --------------------------------
@pytest.mark.parametrize("cls", [ClaudeProvider, AgyProvider])
def test_cli_ausente_vira_erro_tipado(tmp_path, cls):
    missing = str(tmp_path / "nao-instalada")
    err = error_of(cls(missing, "", 1).complete("x", timeout_s=10))
    assert err.kind is ErrorKind.FAILED and err.detail == NOT_INSTALLED
    status = run(cls(missing, "", 1).status())
    assert status.status is Health.ERROR and status.detail == NOT_INSTALLED
