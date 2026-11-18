"""E3.8 — custos e quotas: limites por provedor e por blog tiram o provedor da cadeia."""
from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text

from app.core.config import settings
from app.core.db import session_scope
from app.services import sites
from app.services.ai import chain, gateway, quota
from app.services.ai.secrets import encrypt
from tests.conftest import fetch


@pytest.fixture
def gw(monkeypatch) -> list[str]:
    """Gateway que responde sempre; devolve a lista dos provedores chamados."""
    called: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":  # estado do login, pedido pela página do painel
            return httpx.Response(200, json={"provider": "codex", "status": "none"})
        body = json.loads(request.content)
        called.append(body["provider"])
        return httpx.Response(200, json={
            "provider": body["provider"], "model": "m", "text": "ok", "duration_ms": 1,
            "usage": {"input_tokens": 1_000_000, "output_tokens": 500_000}})

    monkeypatch.setattr(gateway, "_transport", httpx.MockTransport(handler))
    return called


def limits(name: str, **values) -> None:
    base = dict.fromkeys(("daily_call_limit", "daily_budget_usd", "monthly_budget_usd",
                          "price_in_usd_mtok", "price_out_usd_mtok"))
    with session_scope() as db:
        quota.set_limits(db, name, **{**base, **values})


def add_paid(name: str = "openrouter", **values) -> None:
    with session_scope() as db:
        db.execute(
            text("""INSERT INTO ai_providers (name, kind, family, model, base_url, api_key_enc)
                    VALUES (:n, 'api', 'meta', 'm', 'https://x.test/v1', :k)"""),
            {"n": name, "k": encrypt("sk-teste")},
        )
    # 1 M tokens de entrada + 0,5 M de saída por chamada → US$ 2 + US$ 3 = US$ 5.
    limits(name, price_in_usd_mtok=Decimal("2"), price_out_usd_mtok=Decimal("6"), **values)


def age_usage(interval: str) -> None:
    with session_scope() as db:
        db.execute(text(f"UPDATE provider_usage SET created_at = now() - interval '{interval}'"))


# --- Registo e custo ---------------------------------------------------------------


def test_cada_chamada_fica_registada_com_papel_blog_e_custo(gw):
    add_paid()
    with session_scope() as db:
        chain.set_chain(db, "translator", ["openrouter", "claude"])
    chain.run("translator", "x", purpose="translate", site="cloud")
    chain.run("writer", "x", purpose="article", site="cloud")
    rows = fetch("""SELECT provider, kind, role, purpose, site, input_tokens, output_tokens,
                           cost_usd FROM provider_usage ORDER BY id""")
    assert dict(rows[0]) == {"provider": "openrouter", "kind": "api", "role": "translator",
                             "purpose": "translate", "site": "cloud",
                             "input_tokens": 1_000_000, "output_tokens": 500_000,
                             "cost_usd": Decimal("5.000000")}
    # Assinatura: sem custo por chamada.
    assert (rows[1]["provider"], rows[1]["kind"], rows[1]["cost_usd"]) == (
        "claude", "subscription", Decimal("0"))


def test_provedor_pago_sem_preco_conta_zero(gw):
    add_paid()
    limits("openrouter")  # apaga os preços
    with session_scope() as db:
        chain.set_chain(db, "translator", ["openrouter", "claude"])
    chain.run("translator", "x")
    assert fetch("SELECT cost_usd FROM provider_usage")[0]["cost_usd"] == Decimal("0")


# --- Limite diário de chamadas por assinatura (R19) -----------------------------------


def test_limite_diario_de_chamadas_tira_a_assinatura_da_cadeia(gw):
    limits("claude", daily_call_limit=2)
    assert [chain.run("writer", "x").provider for _ in range(3)] == ["claude", "claude", "codex"]
    result = chain.run("writer", "x")
    assert result.skipped == (("claude", "limit:daily_call_limit"),)
    assert gw == ["claude", "claude", "codex", "codex"]  # o Claude nem é chamado


def test_limite_diario_renova_no_dia_seguinte(gw):
    limits("claude", daily_call_limit=1)
    chain.run("writer", "x")
    assert chain.run("writer", "x").provider == "codex"
    age_usage("2 days")
    assert chain.run("writer", "x").provider == "claude"


def test_limite_zero_desliga_e_sem_limite_nao_trava(gw):
    limits("claude", daily_call_limit=0)
    assert chain.run("writer", "x").provider == "codex"
    limits("claude")
    assert [chain.run("writer", "x").provider for _ in range(5)] == ["claude"] * 5


# --- Orçamento dos provedores pagos ----------------------------------------------------


def test_orcamento_diario_esgotado_remove_o_provedor(gw):
    add_paid(daily_budget_usd=Decimal("9"))
    with session_scope() as db:
        chain.set_chain(db, "translator", ["openrouter", "claude"])
    # US$ 5 por chamada: a 2.ª ainda passa (5 < 9), a 3.ª já não (10 >= 9).
    assert [chain.run("translator", "x").provider for _ in range(3)] == [
        "openrouter", "openrouter", "claude"]
    assert chain.run("translator", "x").skipped == (("openrouter", "limit:daily_budget"),)
    age_usage("26 hours")
    assert chain.run("translator", "x").provider == "openrouter"


def test_orcamento_mensal_esgotado_remove_o_provedor(gw):
    add_paid(monthly_budget_usd=Decimal("5"))
    with session_scope() as db:
        chain.set_chain(db, "translator", ["openrouter", "claude"])
    assert chain.run("translator", "x").provider == "openrouter"
    result = chain.run("translator", "x")
    assert result.provider == "claude"
    assert result.skipped == (("openrouter", "limit:monthly_budget"),)
    age_usage("40 days")
    assert chain.run("translator", "x").provider == "openrouter"


def test_todos_no_limite_esgota_a_cadeia(gw):
    for name in ("claude", "codex", "agy"):
        limits(name, daily_call_limit=0)
    with pytest.raises(chain.ChainExhausted) as info:
        chain.run("writer", "x")
    assert all(reason == "limit:daily_call_limit" for _, reason in info.value.attempts)
    assert gw == []


# --- Orçamento de chamadas por job (llm_job_cap, revisão 360° de 5/11) --------------------


def make_job(slot: int = 1) -> int:
    """Um job real (o `provider_usage.job_id` tem FK para `publication_jobs`)."""
    with session_scope() as db:
        sites.sync(db)
        return db.execute(text(
            """INSERT INTO publication_jobs
                 (site_key, local_date, slot_number, due_at_utc, produce_after_utc, status)
               VALUES ('cloud', '2026-10-05', :n, now(), now(), 'QUEUED') RETURNING id"""),
            {"n": slot}).scalar_one()


def test_orcamento_por_job_trava_so_o_job_que_gastou(gw, monkeypatch):
    monkeypatch.setattr(settings, "llm_job_cap", 2)
    first, second = make_job(1), make_job(2)
    with quota.for_job(first):
        chain.run("writer", "x")
        chain.run("writer", "x")
        with pytest.raises(chain.JobCallCap) as info:
            chain.run("writer", "x")
    # É uma ChainExhausted (a produção trata com RETRY_WAIT e espera do R27, sem código novo).
    assert isinstance(info.value, chain.ChainExhausted)
    assert "llm_job_cap" in str(info.value)
    # Outro job e chamadas fora de job seguem livres: o teto é do job, não do dia.
    with quota.for_job(second):
        assert chain.run("writer", "x").provider == "claude"
    assert chain.run("writer", "x").provider == "claude"
    assert gw == ["claude"] * 4  # a 3.ª chamada do 1.º job nem chegou ao gateway


def test_orcamento_por_job_conta_falhas_e_renova_no_dia_seguinte(gw, monkeypatch):
    monkeypatch.setattr(settings, "llm_job_cap", 1)
    job_id = make_job()
    with quota.for_job(job_id):
        chain.run("writer", "x")
        with pytest.raises(chain.JobCallCap):
            chain.run("writer", "x")
    age_usage("2 days")
    with quota.for_job(job_id):
        assert chain.run("writer", "x").provider == "claude"


def test_orcamento_por_job_desligado_por_omissao(gw):
    assert settings.llm_job_cap == 0
    with quota.for_job(make_job()):
        for _ in range(5):
            chain.run("writer", "x")
    assert len(gw) == 5


# --- Orçamento por blog ------------------------------------------------------------------


def test_orcamento_do_blog_trava_so_os_pagos_e_so_esse_blog(gw):
    add_paid()
    with session_scope() as db:
        chain.set_chain(db, "translator", ["openrouter", "claude"])
        quota.set_site_budget(db, "Cloud", Decimal("5"), None)
    assert chain.run("translator", "x", site="cloud").provider == "openrouter"
    # O blog cloud atingiu o teto: passa a usar a assinatura…
    result = chain.run("translator", "x", site="cloud")
    assert (result.provider, result.skipped) == (
        "claude", (("openrouter", "limit:site_daily_budget"),))
    # …mas outro blog (ou uma chamada sem blog) continua no provedor pago.
    assert chain.run("translator", "x", site="carros").provider == "openrouter"
    assert chain.run("translator", "x").provider == "openrouter"


def test_orcamento_mensal_do_blog(gw):
    add_paid()
    with session_scope() as db:
        chain.set_chain(db, "translator", ["openrouter", "claude"])
        quota.set_site_budget(db, "cloud", None, Decimal("5"))
    chain.run("translator", "x", site="cloud")
    assert chain.run("translator", "x", site="cloud").skipped == (
        ("openrouter", "limit:site_monthly_budget"),)


def test_orcamento_do_blog_vazio_apaga_e_valida(gw):
    with session_scope() as db:
        quota.set_site_budget(db, "cloud", Decimal("1"), Decimal("10"))
        assert [b["site"] for b in quota.site_budgets(db)] == ["cloud"]
        quota.set_site_budget(db, "cloud", None, None)
        assert quota.site_budgets(db) == []
    for bad in ("", "com espaço"):
        with pytest.raises(ValueError), session_scope() as db:
            quota.set_site_budget(db, bad, Decimal("1"), None)
    with pytest.raises(ValueError), session_scope() as db:
        quota.set_site_budget(db, "cloud", Decimal("-1"), None)


def test_limites_invalidos(gw):
    with pytest.raises(ValueError, match="negativos"), session_scope() as db:
        quota.set_limits(db, "claude", daily_call_limit=-1, daily_budget_usd=None,
                         monthly_budget_usd=None, price_in_usd_mtok=None,
                         price_out_usd_mtok=None)
    with pytest.raises(ValueError, match="desconhecido"):
        limits("copilot", daily_call_limit=1)


# --- Painel e rotas ----------------------------------------------------------------------


def post(client, path: str, **data):
    return client.post(f"/dashboard/ai{path}", data=data, follow_redirects=False)


def test_painel_grava_limites_e_mostra_uso(panel_client, gw):
    response = post(panel_client, "/providers/claude/limits", daily_call_limit="1")
    assert response.status_code == 303 and "msg=" in response.headers["location"]
    assert fetch("SELECT daily_call_limit FROM ai_providers WHERE name = 'claude'")[0][
        "daily_call_limit"] == 1
    chain.run("writer", "x")
    page = panel_client.get("/dashboard/ai").text
    assert "limite diário de chamadas atingido" in page
    assert "<strong>1</strong> de 1 chamadas" in page
    # Campo vazio = sem limite.
    post(panel_client, "/providers/claude/limits", daily_call_limit="")
    assert fetch("SELECT daily_call_limit FROM ai_providers WHERE name = 'claude'")[0][
        "daily_call_limit"] is None


def test_painel_precos_e_orcamentos_com_virgula(panel_client, gw):
    add_paid()
    post(panel_client, "/providers/openrouter/limits", price_in_usd_mtok="0,25",
         price_out_usd_mtok="1.5", daily_budget_usd="2", monthly_budget_usd="30")
    row = fetch("SELECT * FROM ai_providers WHERE name = 'openrouter'")[0]
    assert (row["price_in_usd_mtok"], row["price_out_usd_mtok"]) == (
        Decimal("0.2500"), Decimal("1.5000"))
    assert (row["daily_budget_usd"], row["monthly_budget_usd"]) == (
        Decimal("2.00"), Decimal("30.00"))
    bad = post(panel_client, "/providers/openrouter/limits", daily_budget_usd="muito")
    assert "err=" in bad.headers["location"]
    assert fetch("SELECT daily_budget_usd FROM ai_providers WHERE name = 'openrouter'")[0][
        "daily_budget_usd"] == Decimal("2.00")


def test_painel_orcamento_por_blog(panel_client, gw):
    post(panel_client, "/site-budgets", site="cloud", daily_budget_usd="1,5",
         monthly_budget_usd="")
    assert dict(fetch("SELECT site, daily_budget_usd, monthly_budget_usd "
                      "FROM ai_site_budgets")[0]) == {
        "site": "cloud", "daily_budget_usd": Decimal("1.50"), "monthly_budget_usd": None}
    assert "cloud" in panel_client.get("/dashboard/ai").text
    assert "err=" in post(panel_client, "/site-budgets", site="a b").headers["location"]


def test_rota_de_maquina_mostra_limite_atingido(client, auth, gw):
    limits("claude", daily_call_limit=0)
    body = client.get("/api/ai/chain", headers=auth).json()
    reached = {p["name"]: p["limit_reached"] for p in body["roles"]["writer"]}
    assert reached == {"claude": "daily_call_limit", "codex": None, "agy": None}


def test_custo_do_painel_principal_vem_do_provider_usage(panel_client, gw):
    add_paid()
    with session_scope() as db:
        chain.set_chain(db, "translator", ["openrouter", "claude"])
    chain.run("translator", "x")  # US$ 5
    from app.api.routes.dashboard import _dashboard_data

    with session_scope() as db:
        assert _dashboard_data(db, 30)["totals"]["cost_usd"] == pytest.approx(5.0)


# --- Revisão E3 #2: tentativas falhadas também contam -------------------------------------


def failing_gateway(monkeypatch, kind: str, status: int = 502) -> list[str]:
    called: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        called.append(body["provider"])
        if body["provider"] == "claude":
            return httpx.Response(status, json={"error": kind, "provider": "claude",
                                                "detail": ""})
        return httpx.Response(200, json={"provider": body["provider"], "model": "m",
                                         "text": "ok", "duration_ms": 1, "usage": {}})

    monkeypatch.setattr(gateway, "_transport", httpx.MockTransport(handler))
    return called


@pytest.mark.parametrize("kind", ["timeout", "empty", "invalid_output", "failed"])
def test_falhas_que_gastam_cota_contam_para_o_limite_diario(monkeypatch, kind):
    called = failing_gateway(monkeypatch, kind)
    limits("claude", daily_call_limit=2)
    for _ in range(4):
        assert chain.run("writer", "x", site="cloud").provider == "codex"
    # O Claude falhou 2 vezes, atingiu o limite e deixou de ser chamado.
    assert called.count("claude") == 2
    rows = fetch("SELECT outcome, cost_usd, input_tokens, site FROM provider_usage "
                 "WHERE provider = 'claude'")
    assert [dict(r) for r in rows] == [
        {"outcome": kind, "cost_usd": Decimal("0"), "input_tokens": 0, "site": "cloud"}] * 2
    assert {r["outcome"] for r in fetch(
        "SELECT outcome FROM provider_usage WHERE provider = 'codex'")} == {"ok"}


@pytest.mark.parametrize(("kind", "status"),
                         [("not_authenticated", 503), ("rate_limited", 429)])
def test_falhas_sem_trabalho_do_modelo_nao_contam(monkeypatch, kind, status):
    failing_gateway(monkeypatch, kind, status)
    chain.run("writer", "x")
    assert fetch("SELECT 1 FROM provider_usage WHERE provider = 'claude'") == []


def test_gateway_inalcancavel_nao_conta(monkeypatch):
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("sem rede")

    monkeypatch.setattr(gateway, "_transport", httpx.MockTransport(down))
    with pytest.raises(chain.ChainExhausted):
        chain.run("writer", "x")
    assert fetch("SELECT 1 FROM provider_usage") == []
