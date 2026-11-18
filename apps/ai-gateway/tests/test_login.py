"""Login por código de dispositivo conduzido pelo painel (E3.7)."""

from __future__ import annotations

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from app import login
from tests.conftest import AgyProvider, CodexProvider, FakeCLI, install

DEVICE = """
print("Welcome to Codex")
print("1. Open this link in your browser and sign in to your account")
print("   \\x1b[94mhttps://auth.exemplo.test/codex/device\\x1b[0m")
print("2. Enter this one-time code (expires in 15 minutes)")
print("   \\x1b[94mCINP-W47O0\\x1b[0m", flush=True)
"""


@pytest.fixture(autouse=True)
def _cleanup(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(login, "START_WAIT_S", 3.0)
    yield
    for session in list(login._sessions.values()):
        login._kill(session)
    login._sessions.clear()


def use(monkeypatch: pytest.MonkeyPatch, binary: str) -> None:
    monkeypatch.setitem(login.LOGIN_COMMANDS, "codex", (binary, "login", "--device-auth"))


@pytest.fixture(autouse=True)
def _codex_signed_out(fake_cli: FakeCLI) -> None:
    """Por omissão o Codex está sem login, que é quando o painel inicia um."""
    install("codex", CodexProvider(fake_cli('print("Not logged in"); sys.exit(1)'), "", 1))


def test_start_returns_url_and_code_while_waiting(
    client: TestClient, fake_cli: FakeCLI, monkeypatch: pytest.MonkeyPatch
) -> None:
    use(monkeypatch, fake_cli(DEVICE + "time.sleep(30)"))
    with client:
        res = client.post("/v1/login/start", json={"provider": "codex"})
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "running"
        assert body["url"] == "https://auth.exemplo.test/codex/device"  # sem códigos ANSI
        assert body["code"] == "CINP-W47O0"
        assert 0 < body["expires_in_s"] <= login.MAX_AGE_S
        assert client.get("/v1/login/codex").json()["status"] == "running"


def test_success_and_failure_are_reported(
    client: TestClient, fake_cli: FakeCLI, monkeypatch: pytest.MonkeyPatch
) -> None:
    with client:
        use(monkeypatch, fake_cli(DEVICE + 'time.sleep(0.5); print("Successfully logged in")'))
        assert client.post("/v1/login/start", json={"provider": "codex"}).json()["code"]
        time.sleep(1.2)
        done = client.get("/v1/login/codex").json()
        # Terminado: o endereço e o código (já gasto) deixam de ser devolvidos…
        assert (done["status"], done["url"], done["code"]) == ("succeeded", None, None)
        # …e a sessão é descartada passado o tempo de retenção.
        monkeypatch.setattr(login, "KEEP_FINISHED_S", 0)
        assert client.get("/v1/login/codex").json() == {"provider": "codex", "status": "none"}
        monkeypatch.setattr(login, "KEEP_FINISHED_S", 120)

        use(monkeypatch, fake_cli('print("device code disabled"); sys.exit(1)'))
        body = client.post("/v1/login/start", json={"provider": "codex"}).json()
        assert body["status"] == "failed" and body["url"] is None and body["code"] is None


def test_restart_kills_the_previous_login(
    client: TestClient, fake_cli: FakeCLI, monkeypatch: pytest.MonkeyPatch
) -> None:
    use(monkeypatch, fake_cli(DEVICE + "time.sleep(30)"))
    with client:
        client.post("/v1/login/start", json={"provider": "codex"})
        first = login.current("codex")
        assert first is not None
        client.post("/v1/login/start", json={"provider": "codex"})
        second = login.current("codex")
        assert second is not None and second is not first
        assert first.proc.returncode is not None  # o anterior morreu
        assert second.proc.returncode is None


def test_login_expires(fake_cli: FakeCLI, monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, fake_cli(DEVICE + "time.sleep(30)"))
    monkeypatch.setattr(login, "MAX_AGE_S", 1)

    async def scenario() -> str:
        session = await login.start("codex")
        await asyncio.sleep(2)
        status = session.status
        await login.cancel_all()
        return status

    assert asyncio.run(scenario()) == "failed"


def test_unsupported_and_unknown(client: TestClient) -> None:
    for name in ("claude", "agy", "copilot"):
        res = client.post("/v1/login/start", json={"provider": name})
        assert (res.status_code, res.json()["error"]) == (400, "login_not_supported")
    assert client.get("/v1/login/codex").json() == {"provider": "codex", "status": "none"}


def test_login_env_has_no_api_keys(
    client: TestClient, fake_cli: FakeCLI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "x")
    body = (
        'print("https://x.test ABCD-12345", "OPENAI_API_KEY" in os.environ, flush=True)\n'
        "time.sleep(30)"
    )
    use(monkeypatch, fake_cli(body))
    with client:
        client.post("/v1/login/start", json={"provider": "codex"})
        session = login.current("codex")
        assert session is not None and "False" in session.output


def test_agy_signed_out_is_not_ok(client: TestClient, fake_cli: FakeCLI) -> None:
    # Comportamento real do agy 1.2.16 sem login: sai com 0 e esta mensagem.
    body = (
        'print("Fetching available models...")\n'
        'print("Error: Please sign in to view available models. Launch the CLI without '
        'arguments to sign in.")'
    )
    install("agy", AgyProvider(fake_cli(body), "", 1))
    assert client.post("/v1/status", json={"provider": "agy"}).json()["status"] == (
        "not_authenticated"
    )
    install("agy", AgyProvider(fake_cli('print("Fetching available models...")'), "", 1))
    assert client.post("/v1/status", json={"provider": "agy"}).json()["status"] == "error"


def test_cancel(client: TestClient, fake_cli: FakeCLI, monkeypatch: pytest.MonkeyPatch) -> None:
    use(monkeypatch, fake_cli(DEVICE + "time.sleep(30)"))
    with client:
        client.post("/v1/login/start", json={"provider": "codex"})
        session = login.current("codex")
        assert session is not None
        res = client.post("/v1/login/cancel", json={"provider": "codex"})
        assert res.json() == {"provider": "codex", "status": "none"}
        assert session.proc.returncode is not None
        assert client.get("/v1/login/codex").json()["status"] == "none"


def test_start_refuses_to_wipe_a_working_login(
    client: TestClient, fake_cli: FakeCLI, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = fake_cli("pass") + ".started"
    use(monkeypatch, fake_cli(f"open({marker!r}, 'w').write('x')\n" + DEVICE + "time.sleep(30)"))
    install("codex", CodexProvider(fake_cli('print("Logged in using ChatGPT")'), "", 1))
    with client:
        res = client.post("/v1/login/start", json={"provider": "codex"})
        assert (res.status_code, res.json()["error"]) == (409, "already_authenticated")
        assert login.current("codex") is None
        assert not __import__("os").path.exists(marker)  # o comando de login nem arrancou
        forced = client.post("/v1/login/start", json={"provider": "codex", "force": True})
        assert forced.status_code == 200 and forced.json()["status"] == "running"
