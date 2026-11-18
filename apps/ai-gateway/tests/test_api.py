"""API `/v1/complete` e `/v1/structured`: contrato HTTP e códigos de erro."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from tests.conftest import AgyProvider, ClaudeProvider, CodexProvider, FakeCLI, install
from tests.test_providers import AGY_OK, CLAUDE_OK, CODEX_OK, SCHEMA


def test_health(client: TestClient) -> None:
    assert client.get("/health/live").json() == {"status": "ok"}


def test_complete(client: TestClient, fake_cli: FakeCLI) -> None:
    install("claude", ClaudeProvider(fake_cli(CLAUDE_OK), "", 1))
    res = client.post("/v1/complete", json={"provider": "claude", "prompt": "olá"})
    assert res.status_code == 200
    body = res.json()
    assert body["provider"] == "claude"
    assert body["text"] == "pronto"
    assert body["usage"] == {"input_tokens": 17, "output_tokens": 7}
    assert body["duration_ms"] >= 0
    assert "data" not in body


def test_structured_for_each_provider(client: TestClient, fake_cli: FakeCLI) -> None:
    install("claude", ClaudeProvider(fake_cli(CLAUDE_OK), "", 1))
    install("codex", CodexProvider(fake_cli(CODEX_OK), "", 1))
    install("agy", AgyProvider(fake_cli(AGY_OK), "", 1))
    for name in ("claude", "codex", "agy"):
        res = client.post(
            "/v1/structured", json={"provider": name, "prompt": "x", "json_schema": SCHEMA}
        )
        assert res.status_code == 200, name
        assert res.json()["data"] == {"titulo": "t"}
        assert "text" not in res.json()


def test_unknown_provider(client: TestClient) -> None:
    res = client.post("/v1/complete", json={"provider": "copilot", "prompt": "x"})
    assert res.status_code == 404
    assert res.json()["error"] == "unknown_provider"


def test_validation(client: TestClient) -> None:
    assert client.post("/v1/complete", json={"provider": "claude", "prompt": ""}).status_code == 422
    assert (
        client.post("/v1/structured", json={"provider": "claude", "prompt": "x"}).status_code == 422
    )
    res = client.post("/v1/complete", json={"provider": "claude", "prompt": "x", "timeout_s": 0})
    assert res.status_code == 422


def test_error_status_codes(client: TestClient, fake_cli: FakeCLI) -> None:
    cases = {
        "time.sleep(30)": (504, "timeout"),
        'sys.stderr.write("Not logged in"); sys.exit(1)': (503, "not_authenticated"),
        'sys.stderr.write("429 Too Many Requests"); sys.exit(1)': (429, "rate_limited"),
        'print(json.dumps({"type": "turn.completed", "usage": {}}))': (502, "empty"),
        'sys.stderr.write("explodiu"); sys.exit(3)': (502, "failed"),
    }
    for body, (status, kind) in cases.items():
        install("codex", CodexProvider(fake_cli(body), "", 1))
        res = client.post(
            "/v1/complete", json={"provider": "codex", "prompt": "x", "timeout_s": 0.5}
        )
        assert (res.status_code, res.json()["error"]) == (status, kind), body
        assert res.json()["provider"] == "codex"


def test_timeout_is_capped(client: TestClient, fake_cli: FakeCLI, monkeypatch) -> None:
    monkeypatch.setattr(settings, "max_timeout_s", 0.5)
    install("claude", ClaudeProvider(fake_cli("time.sleep(30)"), "", 1))
    res = client.post("/v1/complete", json={"provider": "claude", "prompt": "x", "timeout_s": 600})
    assert res.status_code == 504


# --- Estado dos provedores (E3.6) ---------------------------------------------


def test_status_of_each_cli(client: TestClient, fake_cli: FakeCLI) -> None:
    record = 'open(sys.argv[0] + ".argv", "w").write(json.dumps(sys.argv[1:]))\n'
    cases = [
        ("claude", ClaudeProvider, 'print(json.dumps({"loggedIn": True}))', "ok"),
        ("claude", ClaudeProvider, 'print(json.dumps({"loggedIn": False}))', "not_authenticated"),
        ("claude", ClaudeProvider, 'print("avaria"); sys.exit(2)', "error"),
        ("codex", CodexProvider, 'print("Logged in using ChatGPT")', "ok"),
        ("codex", CodexProvider, 'print("Not logged in"); sys.exit(1)', "not_authenticated"),
        ("codex", CodexProvider, 'sys.stderr.write("avaria"); sys.exit(1)', "error"),
        ("agy", AgyProvider, 'print("gemini-x\\tGemini X")', "ok"),
        ("agy", AgyProvider, 'print("Authentication required."); sys.exit(1)', "not_authenticated"),
        ("agy", AgyProvider, "sys.exit(0)", "error"),
    ]
    expected_argv = {"claude": ["auth", "status"], "codex": ["login", "status"], "agy": ["models"]}
    for name, cls, body, expected in cases:
        binary = fake_cli(record + body)
        install(name, cls(binary, "", 1))
        res = client.post("/v1/status", json={"provider": name})
        assert res.status_code == 200, body
        assert (res.json()["provider"], res.json()["status"]) == (name, expected), body
        argv = json.loads(Path(binary + ".argv").read_text())
        assert argv[: len(expected_argv[name])] == expected_argv[name]
        assert "-p" not in argv and "exec" not in argv  # nunca chama o modelo


def test_status_timeout_and_unknown(client: TestClient, fake_cli: FakeCLI, monkeypatch) -> None:
    monkeypatch.setattr(CodexProvider, "status_timeout_s", 0.3)
    install("codex", CodexProvider(fake_cli("time.sleep(30)"), "", 1))
    res = client.post("/v1/status", json={"provider": "codex"})
    assert (res.status_code, res.json()["status"]) == (200, "error")
    assert "timeout" in res.json()["detail"]
    assert client.post("/v1/status", json={"provider": "copilot"}).status_code == 404


def test_status_does_not_wait_for_the_call_queue(client: TestClient, fake_cli: FakeCLI) -> None:
    provider = CodexProvider(fake_cli('print("Logged in")'), "", 1)
    install("codex", provider)
    # Semáforo ocupado: uma chamada real em curso não pode atrasar o estado.
    provider._slots = asyncio.Semaphore(0)
    assert client.post("/v1/status", json={"provider": "codex"}).json()["status"] == "ok"


def test_image_endpoint(
    client: TestClient, fake_cli: FakeCLI, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import base64

    from app.config import settings
    from tests.test_providers import PNG, _codex_image

    monkeypatch.setattr(settings, "codex_image_dir", str(tmp_path / "images"))
    install("codex", CodexProvider(_codex_image(fake_cli, tmp_path / "images"), "", 1))
    res = client.post("/v1/image", json={"prompt": "blue cubes", "timeout_s": 10})
    assert res.status_code == 200, res.text
    assert base64.b64decode(res.json()["image_base64"]) == PNG
    assert res.json()["provider"] == "codex"


def test_image_so_com_codex(client: TestClient, fake_cli: FakeCLI) -> None:
    install("claude", ClaudeProvider(fake_cli(CLAUDE_OK), "", 1))
    res = client.post("/v1/image", json={"provider": "claude", "prompt": "x"})
    assert res.status_code == 400 and res.json()["error"] == "image_not_supported"
