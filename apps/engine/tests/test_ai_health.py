"""E3.6 — saúde dos provedores de IA: probe, exclusão da cadeia e recuperação."""
from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import text

from app.core.config import settings
from app.core.db import session_scope
from app.jobs import scheduler
from app.services.ai import chain, gateway, health
from app.services.ai.secrets import encrypt
from tests.conftest import fetch


class FakeGateway:
    def __init__(self) -> None:
        self.status: dict[str, object] = {}   # provedor → estado do /v1/status ou exceção
        self.calls: dict[str, object] = {}    # provedor → (http, kind) de erro em /v1/complete
        self.requests: list[tuple[str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        name = body["provider"]
        self.requests.append((request.url.path, body))
        if request.url.path == "/v1/status":
            reply = self.status.get(name, "ok")
            if isinstance(reply, Exception):
                raise reply
            return httpx.Response(200, json={"provider": name, "status": reply, "detail": "d"})
        reply = self.calls.get(name)
        if reply:
            return httpx.Response(reply[0], json={"error": reply[1], "provider": name,
                                                  "detail": "simulado"})
        return httpx.Response(200, json={"provider": name, "model": "m", "text": f"de {name}",
                                         "usage": {"input_tokens": 1, "output_tokens": 1},
                                         "duration_ms": 1})

    def paths(self, path: str) -> list[str]:
        return [body["provider"] for p, body in self.requests if p == path]


@pytest.fixture
def gw(monkeypatch) -> FakeGateway:
    fake = FakeGateway()
    monkeypatch.setattr(gateway, "_transport", httpx.MockTransport(fake))
    return fake


def stored() -> dict[str, tuple[str, str]]:
    rows = fetch("""SELECT p.name, h.status, h.source FROM ai_provider_health h
                      JOIN ai_providers p ON p.id = h.provider_id""")
    return {r["name"]: (r["status"], r["source"]) for r in rows}


def age_health(name: str, minutes: int) -> None:
    with session_scope() as db:
        db.execute(
            text("""UPDATE ai_provider_health
                       SET checked_at = now() - make_interval(mins => :m)
                     WHERE provider_id = (SELECT id FROM ai_providers WHERE name = :n)"""),
            {"m": minutes, "n": name},
        )


# --- Probe ----------------------------------------------------------------------


def test_probe_regista_o_estado_de_cada_provedor(gw):
    gw.status.update({"codex": "not_authenticated", "agy": "error"})
    assert health.probe_all() == {"claude": "ok", "codex": "not_authenticated", "agy": "error"}
    assert stored() == {"claude": ("ok", "probe"), "codex": ("not_authenticated", "probe"),
                        "agy": ("error", "probe")}
    # Só a rota de estado: o probe nunca chama um modelo.
    assert gw.paths("/v1/complete") == [] and gw.paths("/v1/structured") == []
    assert gw.paths("/v1/status") == ["claude", "codex", "agy"]


def test_probe_ignora_provedores_desligados(gw):
    with session_scope() as db:
        db.execute(text("UPDATE ai_providers SET enabled = false WHERE name = 'agy'"))
    assert set(health.probe_all()) == {"claude", "codex"}


def test_probe_com_gateway_inalcancavel_marca_erro_sem_bloquear(gw):
    for name in ("claude", "codex", "agy"):
        gw.status[name] = httpx.ConnectError("sem rede")
    assert set(health.probe_all().values()) == {"error"}
    assert all(d.startswith("gateway:") for d in
               [r["detail"] for r in fetch("SELECT detail FROM ai_provider_health")])


def test_probe_de_provedor_api(gw):
    with session_scope() as db:
        db.execute(
            text("""INSERT INTO ai_providers
                      (name, kind, family, model, base_url, api_key_enc)
                    VALUES ('groq', 'api', 'meta', 'm', 'https://x.test/v1', :k),
                           ('semchave', 'api', 'meta', 'm', 'https://x.test/v1', NULL),
                           ('ilegivel', 'api', 'meta', 'm', 'https://x.test/v1', 'lixo')"""),
            {"k": encrypt("sk-teste")},
        )
    results = health.probe_all()
    assert results["groq"] == "ok"
    assert results["semchave"] == results["ilegivel"] == "not_authenticated"
    sent = [body for path, body in gw.requests if body["provider"] == "groq"][0]
    assert sent["endpoint"] == {"base_url": "https://x.test/v1", "api_key": "sk-teste",
                                "model": "m"}
    # Sem chave utilizável nem chega a ir ao gateway.
    assert {"semchave", "ilegivel"}.isdisjoint(gw.paths("/v1/status"))


# --- Exclusão da cadeia ----------------------------------------------------------


def test_nao_autenticado_sai_da_cadeia_e_volta_quando_autenticar(gw):
    gw.status["claude"] = "not_authenticated"
    health.probe_all()
    result = chain.run("writer", "x")
    assert result.provider == "codex"
    assert result.skipped == (("claude", "unhealthy:not_authenticated"),)
    assert "claude" not in gw.paths("/v1/complete")  # nem tenta

    gw.status["claude"] = "ok"   # o Euclides autenticou
    health.probe_all()
    assert chain.run("writer", "x").provider == "claude"


def test_estado_error_nao_tira_da_cadeia(gw):
    gw.status["claude"] = "error"
    health.probe_all()
    assert chain.run("writer", "x").provider == "claude"


def test_chamada_real_com_429_marca_cota_esgotada(gw):
    gw.calls["claude"] = (429, "rate_limited")
    assert chain.run("writer", "x").provider == "codex"
    assert stored()["claude"] == ("quota_exhausted", "call")
    assert stored()["codex"] == ("ok", "call")
    # Na chamada seguinte o Claude já é saltado sem gastar tempo.
    gw.requests.clear()
    result = chain.run("writer", "x")
    assert result.skipped == (("claude", "unhealthy:quota_exhausted"),)
    assert gw.paths("/v1/complete") == ["codex"]


def test_chamada_real_nao_autenticada_marca_o_provedor(gw):
    gw.calls["claude"] = (503, "not_authenticated")
    chain.run("writer", "x")
    assert stored()["claude"] == ("not_authenticated", "call")


def test_timeout_e_vazio_nao_mudam_a_saude(gw):
    gw.calls["claude"] = (504, "timeout")
    chain.run("writer", "x")
    assert "claude" not in stored()


def test_probe_respeita_a_espera_depois_de_uma_falha_real(gw):
    gw.calls["claude"] = (429, "rate_limited")
    chain.run("writer", "x")
    # O probe barato diz "ok" (há credencial), mas a cota esgotou há instantes.
    assert health.probe_all()["claude"] == "quota_exhausted"
    assert stored()["claude"] == ("quota_exhausted", "call")
    assert chain.run("writer", "x").provider == "codex"

    age_health("claude", settings.ai_cooldown_min + 1)
    assert health.probe_all()["claude"] == "ok"
    gw.calls.clear()
    assert chain.run("writer", "x").provider == "claude"


def test_probe_pode_piorar_durante_a_espera(gw):
    gw.calls["claude"] = (429, "rate_limited")
    chain.run("writer", "x")
    gw.status["claude"] = "not_authenticated"
    assert health.probe_all()["claude"] == "not_authenticated"
    assert stored()["claude"] == ("not_authenticated", "probe")


def test_sucesso_real_repoe_o_estado(gw):
    gw.status["codex"] = "error"
    health.probe_all()
    chain.run("reviewer", "x", writer_family="anthropic")
    assert stored()["codex"] == ("ok", "call")


def test_todos_doentes_esgota_a_cadeia_sem_chamar_ninguem(gw):
    for name in ("claude", "codex", "agy"):
        gw.status[name] = "not_authenticated"
    health.probe_all()
    with pytest.raises(chain.ChainExhausted):
        chain.run("translator", "x")
    assert gw.paths("/v1/complete") == []


# --- Rota e scheduler ------------------------------------------------------------


def test_rota_mostra_a_saude(client, auth, gw):
    body = client.get("/api/ai/chain", headers=auth).json()
    assert {p["health"] for p in body["roles"]["writer"]} == {"unknown"}
    gw.status["codex"] = "not_authenticated"
    health.probe_all()
    body = client.get("/api/ai/chain", headers=auth).json()
    assert {p["name"]: p["health"] for p in body["roles"]["writer"]} == {
        "claude": "ok", "codex": "not_authenticated", "agy": "ok"}


def test_scheduler_corre_o_probe_e_sobrevive_a_falhas(gw, monkeypatch):
    scheduler.probe_ai()
    assert set(stored()) == {"claude", "codex", "agy"}

    def boom() -> None:
        raise RuntimeError("base em baixo")

    monkeypatch.setattr(health, "probe_all", boom)
    scheduler.probe_ai()  # não propaga
