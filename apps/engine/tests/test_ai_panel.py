"""E3.7 — painel "Provedores de IA"."""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.core.db import session_scope
from app.services.ai import chain, gateway, providers
from app.services.ai.secrets import decrypt
from tests.conftest import fetch

KEY = "sk-chave-do-painel"
API_FORM = {"name": "OpenRouter", "family": "meta", "base_url": "https://openrouter.ai/api/v1/",
            "model": "meta/llama", "api_key": KEY}


class FakeGateway:
    def __init__(self) -> None:
        self.status: dict[str, str] = {}
        self.login: dict[str, object] = {"provider": "codex", "status": "none"}
        self.requests: list[tuple[str, str, dict]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.method, request.url.path, body))
        path = request.url.path
        if path == "/v1/status":
            return httpx.Response(200, json={"provider": body["provider"], "detail": "",
                                             "status": self.status.get(body["provider"], "ok")})
        if path == "/v1/login/start":
            if self.status.get(body["provider"], "ok") == "ok" and body["provider"] == "codex":
                return httpx.Response(409, json={"error": "already_authenticated",
                                                 "provider": "codex", "detail": ""})
            if body["provider"] != "codex":
                return httpx.Response(400, json={"error": "login_not_supported",
                                                 "provider": body["provider"], "detail": ""})
            self.login = {"provider": "codex", "status": "running", "expires_in_s": 890,
                          "url": "https://auth.exemplo.test/codex/device", "code": "ABCD-12345"}
            return httpx.Response(200, json=self.login)
        if path.startswith("/v1/login/"):
            return httpx.Response(200, json=self.login)
        return httpx.Response(404, json={"error": "x"})


@pytest.fixture
def gw(monkeypatch) -> FakeGateway:
    fake = FakeGateway()
    monkeypatch.setattr(gateway, "_transport", httpx.MockTransport(fake))
    return fake


def post(client, path: str, **data):
    return client.post(f"/dashboard/ai{path}", data=data, follow_redirects=False)


def flash(response) -> dict[str, str]:
    assert response.status_code == 303, response.text
    parts = urlsplit(response.headers["location"])
    assert parts.path == "/dashboard/ai"
    return {k: v[0] for k, v in parse_qs(parts.query).items()}


def order(role: str) -> list[str]:
    with session_scope() as db:
        return [p.name for p in chain.load_chain(db, role)]


# --- Acesso ----------------------------------------------------------------------


def test_exige_sessao_do_painel(client, gw):
    response = client.get("/dashboard/ai", follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith("/login")
    response = post(client, "/providers/claude/toggle", enabled="false")
    assert response.headers["location"].startswith("/login")
    assert fetch("SELECT enabled FROM ai_providers WHERE name = 'claude'")[0]["enabled"] is True


def test_token_de_maquina_nao_abre_o_painel(client, auth, gw):
    assert client.get("/dashboard/ai", headers=auth, follow_redirects=False).status_code == 303


# --- Página ----------------------------------------------------------------------


def test_pagina_mostra_estado_cadeias_e_ajuda(panel_client, gw):
    gw.status["claude"] = "not_authenticated"
    post(panel_client, "/providers/claude/probe")
    page = panel_client.get("/dashboard/ai").text
    assert "Provedores de IA" in page
    for label in ("Redator", "Revisor", "Tradutor", "Pesquisador"):
        assert label in page
    assert "não autenticado" in page
    # Claude não autenticado: instrução do login próprio no container (D-038).
    assert "docker exec -it sb-ai-gateway claude auth login" in page
    assert ".credentials.json" not in page
    # agy por verificar: instrução do terminal; Codex: botão Autenticar.
    assert "docker exec -it sb-ai-gateway agy" in page
    assert "Configuração inválida" not in page


def test_painel_principal_liga_a_pagina(panel_client, gw):
    assert 'href="/dashboard/ai"' in panel_client.get("/").text


def test_mensagens_sao_escapadas(panel_client, gw):
    page = panel_client.get("/dashboard/ai", params={"err": "<script>alert(1)</script>"}).text
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


# --- Ligar/desligar, modelo, verificar --------------------------------------------


def test_desligar_e_ligar(panel_client, gw):
    assert "desligado" in flash(post(panel_client, "/providers/agy/toggle", enabled="false"))["msg"]
    assert fetch("SELECT enabled FROM ai_providers WHERE name = 'agy'")[0]["enabled"] is False
    assert "desligado" in panel_client.get("/dashboard/ai").text
    post(panel_client, "/providers/agy/toggle", enabled="true")
    assert fetch("SELECT enabled FROM ai_providers WHERE name = 'agy'")[0]["enabled"] is True


def test_desligar_que_quebra_a_regra_e_rejeitado(panel_client, gw):
    post(panel_client, "/providers/agy/toggle", enabled="false")
    # Sem agy e sem codex, o redator Claude ficaria sem revisor de outra família.
    result = flash(post(panel_client, "/providers/codex/toggle", enabled="false"))
    assert "não tem revisor de outra família" in result["err"]
    assert fetch("SELECT enabled FROM ai_providers WHERE name = 'codex'")[0]["enabled"] is True


def test_modelo(panel_client, gw):
    flash(post(panel_client, "/providers/claude/model", model="sonnet"))
    assert fetch("SELECT model FROM ai_providers WHERE name = 'claude'")[0]["model"] == "sonnet"
    assert "inválido" in flash(post(panel_client, "/providers/claude/model", model="a b"))["err"]
    flash(post(panel_client, "/providers/claude/model", model=""))  # volta ao padrão da CLI
    assert fetch("SELECT model FROM ai_providers WHERE name = 'claude'")[0]["model"] == ""


def test_verificar_forca_o_probe_e_reativa(panel_client, gw):
    with session_scope() as db:
        from app.services.ai import health

        health.record(db, "claude", "not_authenticated", "call")
    # O probe periódico respeitaria a espera; o botão não.
    result = flash(post(panel_client, "/providers/claude/probe"))
    assert result == {"msg": "claude: autenticado."}
    assert fetch("""SELECT h.status FROM ai_provider_health h JOIN ai_providers p
                     ON p.id = h.provider_id WHERE p.name = 'claude'""")[0]["status"] == "ok"

    gw.status["claude"] = "not_authenticated"
    result = flash(post(panel_client, "/providers/claude/probe"))
    assert "fora da cadeia" in result["err"]
    assert "err" in flash(post(panel_client, "/providers/copilot/probe"))


# --- Autenticar ---------------------------------------------------------------------


def test_autenticar_codex_ja_autenticado_e_recusado(panel_client, gw):
    # Iniciar um login apaga a credencial atual: com o Codex bom, nem botão nem pedido.
    post(panel_client, "/providers/codex/probe")
    assert 'action="/dashboard/ai/providers/codex/login"' not in panel_client.get(
        "/dashboard/ai").text
    assert "já está autenticado" in flash(post(panel_client, "/providers/codex/login"))["err"]


def test_autenticar_codex_mostra_endereco_e_codigo(panel_client, gw):
    gw.status["codex"] = "not_authenticated"
    post(panel_client, "/providers/codex/probe")
    assert 'action="/dashboard/ai/providers/codex/login"' in panel_client.get(
        "/dashboard/ai").text
    assert "introduza o código" in flash(post(panel_client, "/providers/codex/login"))["msg"]
    page = panel_client.get("/dashboard/ai").text
    assert "https://auth.exemplo.test/codex/device" in page
    assert "ABCD-12345" in page
    assert "Já autorizei" in page


def test_autenticar_sem_codigo_ou_nao_suportado(panel_client, gw, monkeypatch):
    gw.status["codex"] = "not_authenticated"
    assert "não foi possível iniciar" in flash(post(panel_client, "/providers/agy/login"))["err"]

    def failed(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"provider": "codex", "status": "failed",
                                         "url": None, "code": None})

    monkeypatch.setattr(gateway, "_transport", httpx.MockTransport(failed))
    assert "código de dispositivo" in flash(post(panel_client, "/providers/codex/login"))["err"]


def test_pagina_abre_com_o_gateway_em_baixo(panel_client, monkeypatch):
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sem rede")

    monkeypatch.setattr(gateway, "_transport", httpx.MockTransport(down))
    assert panel_client.get("/dashboard/ai").status_code == 200


# --- Ordem por papel ---------------------------------------------------------------


def test_reordenar_remover_e_acrescentar(panel_client, gw):
    flash(post(panel_client, "/chain/translator", action="down", provider="claude"))
    assert order("translator") == ["codex", "claude", "agy"]
    flash(post(panel_client, "/chain/translator", action="up", provider="agy"))
    assert order("translator") == ["codex", "agy", "claude"]
    flash(post(panel_client, "/chain/translator", action="remove", provider="agy"))
    assert order("translator") == ["codex", "claude"]
    flash(post(panel_client, "/chain/translator", action="add", provider="agy"))
    assert order("translator") == ["codex", "claude", "agy"]
    # Subir o primeiro não faz nada.
    flash(post(panel_client, "/chain/translator", action="up", provider="codex"))
    assert order("translator") == ["codex", "claude", "agy"]


def test_cadeia_invalida_e_rejeitada_no_painel(panel_client, gw):
    post(panel_client, "/chain/reviewer", action="remove", provider="agy")
    result = flash(post(panel_client, "/chain/reviewer", action="remove", provider="codex"))
    assert "claude (anthropic) não tem revisor de outra família" in result["err"]
    assert order("reviewer") == ["codex", "claude"]
    assert "err" in flash(post(panel_client, "/chain/editor", action="up", provider="claude"))
    assert "err" in flash(post(panel_client, "/chain/writer", action="explodir", provider="claude"))


# --- API keys ----------------------------------------------------------------------


def test_api_key_cifrada_e_nunca_reexibida(panel_client, gw):
    assert "msg" in flash(post(panel_client, "/api-providers", **API_FORM))
    row = fetch("SELECT * FROM ai_providers WHERE name = 'openrouter'")[0]
    assert (row["kind"], row["family"], row["model"]) == ("api", "meta", "meta/llama")
    assert row["base_url"] == "https://openrouter.ai/api/v1"
    assert KEY not in row["api_key_enc"] and decrypt(row["api_key_enc"]) == KEY

    page = panel_client.get("/dashboard/ai").text
    assert "openrouter" in page
    assert KEY not in page and row["api_key_enc"] not in page
    with session_scope() as db:
        listed = providers.list_providers(db)
    assert all("api_key_enc" not in p for p in listed)


def test_atualizar_sem_chave_mantem_a_guardada(panel_client, gw):
    post(panel_client, "/api-providers", **API_FORM)
    flash(post(panel_client, "/api-providers", **{**API_FORM, "model": "outro", "api_key": ""}))
    row = fetch("SELECT model, api_key_enc FROM ai_providers WHERE name = 'openrouter'")[0]
    assert row["model"] == "outro" and decrypt(row["api_key_enc"]) == KEY
    flash(post(panel_client, "/api-providers", **{**API_FORM, "api_key": "sk-nova"}))
    assert decrypt(fetch("SELECT api_key_enc FROM ai_providers WHERE name = 'openrouter'")
                   [0]["api_key_enc"]) == "sk-nova"


@pytest.mark.parametrize(
    ("override", "fragment"),
    [({"base_url": "http://x.test/v1"}, "https"), ({"name": "claude"}, "assinatura"),
     ({"name": "Nome Com Espaço"}, "nome"), ({"api_key": ""}, "API key em falta"),
     ({"family": "x y"}, "família"), ({"model": "a b"}, "modelo")],
)
def test_provedor_api_invalido(panel_client, gw, override, fragment):
    result = flash(post(panel_client, "/api-providers", **{**API_FORM, **override}))
    assert fragment in result["err"]
    assert KEY not in result["err"]
    assert fetch("SELECT 1 FROM ai_providers WHERE kind = 'api'") == []


def test_provedor_api_entra_na_cadeia_e_e_verificado_com_a_chave(panel_client, gw):
    post(panel_client, "/api-providers", **API_FORM)
    flash(post(panel_client, "/chain/translator", action="add", provider="openrouter"))
    assert order("translator")[-1] == "openrouter"
    flash(post(panel_client, "/providers/openrouter/probe"))
    sent = [body for _, path, body in gw.requests if path == "/v1/status"][-1]
    assert sent["endpoint"]["api_key"] == KEY


def test_apagar_provedor_api(panel_client, gw):
    post(panel_client, "/api-providers", **API_FORM)
    with session_scope() as db:
        chain.set_chain(db, "translator", ["openrouter", "claude", "agy"])
    flash(post(panel_client, "/api-providers/openrouter/delete"))
    assert order("translator") == ["claude", "agy"]
    assert fetch("SELECT position FROM ai_role_chain WHERE role = 'translator' ORDER BY 1") == [
        {"position": 1}, {"position": 2}]
    assert "assinatura" in flash(post(panel_client, "/api-providers/claude/delete"))["err"]


def test_mudar_a_familia_de_um_provedor_api_respeita_a_regra(panel_client, gw):
    # Modelo sem pista de família, para ser a regra da cadeia a recusar.
    form = {**API_FORM, "model": "modelo-neutro"}
    post(panel_client, "/api-providers", **form)
    with session_scope() as db:
        chain.set_chain(db, "writer", ["claude"])
        chain.set_chain(db, "reviewer", ["openrouter"])
    result = flash(post(panel_client, "/api-providers", **{**form, "family": "anthropic"}))
    assert "não tem revisor de outra família" in result["err"]
    assert fetch("SELECT family FROM ai_providers WHERE name = 'openrouter'")[0]["family"] == "meta"


def test_alteracoes_ficam_no_log_com_o_utilizador(panel_client, gw, caplog):
    import logging

    with caplog.at_level(logging.INFO):
        post(panel_client, "/providers/agy/toggle", enabled="false")
        post(panel_client, "/api-providers", **API_FORM)
    assert "euclides@example.com" in caplog.text
    assert KEY not in caplog.text


# --- Revisão E3 #3: o modelo não pode contradizer a família declarada -------------------


@pytest.mark.parametrize(
    ("model", "family"),
    [("claude-opus-5-5-low", "anthropic"), ("anthropic/claude-sonnet", "anthropic"),
     ("gpt-5", "openai"), ("openai/gpt-4.1", "openai"), ("gemini-3.1-pro-high", "google"),
     ("meta-llama/llama-4", "meta"), ("nvidia/nemotron", None), ("", None),
     ("sonnet", "anthropic"), ("opus", "anthropic"), ("haiku", "anthropic"),
     ("sonnet-4.5", "anthropic"), ("anthropic.claude-x", "anthropic"),
     ("o3", "openai"), ("o1", "openai"), ("gpt5", "openai"), ("chatgpt-5", "openai")],
)
def test_familia_implicita_no_nome_do_modelo(model, family):
    assert providers.implied_family(model) == family


def test_agy_nao_aceita_modelo_claude(panel_client, gw):
    # O agy também serve modelos Claude; com família google declarada, Claude reveria Claude.
    result = flash(post(panel_client, "/providers/agy/model", model="claude-opus-5-5-low"))
    assert "é da família anthropic" in result["err"]
    assert fetch("SELECT model FROM ai_providers WHERE name = 'agy'")[0]["model"] == (
        "gemini-3.1-pro-high")
    ok = flash(post(panel_client, "/providers/agy/model", model="gemini-3.8-flash-low"))
    assert "msg" in ok
    assert "err" in flash(post(panel_client, "/providers/claude/model", model="gpt-5"))
    # Só gemini-*: um id sem pista de família também é recusado no agy.
    only = flash(post(panel_client, "/providers/agy/model", model="modelo-desconhecido"))
    assert "só aceita modelos gemini" in only["err"]
    assert "err" in flash(post(panel_client, "/providers/codex/model", model="opus"))
    assert "msg" in flash(post(panel_client, "/providers/claude/model", model="sonnet"))


def test_provedor_api_com_modelo_de_outra_familia_e_recusado(panel_client, gw):
    form = {**API_FORM, "family": "meta", "model": "anthropic/claude-sonnet-5"}
    assert "é da família anthropic" in flash(post(panel_client, "/api-providers", **form))["err"]
    assert fetch("SELECT 1 FROM ai_providers WHERE kind = 'api'") == []
    assert "msg" in flash(post(panel_client, "/api-providers", **{**form, "family": "anthropic"}))
