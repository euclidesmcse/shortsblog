"""E18.3b entrega 3 (D-109): limites e horários dos slots na engrenagem."""
from __future__ import annotations

import re
from datetime import UTC, date, datetime, time
from urllib.parse import unquote_plus

import pytest
from sqlalchemy import text

from app.core import vault_cli
from app.core.config import settings
from app.core.db import session_scope
from app.services import planner, sites, vault, websearch
from app.services.ai import quota
from tests.conftest import fetch, login_as

DAY = date(2026, 10, 5)
BEFORE = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


def location(response) -> str:
    return unquote_plus(response.headers["location"])


def limits_version(page: str) -> str:
    block = page.split('id="limits"', 1)[1]
    return re.search(r'name="version" value="([0-9a-f]+)"', block).group(1)


def put(key: str, value: str) -> None:
    with session_scope() as db:
        vault.set_value(db, key, value, "teste")


def post_limits(panel_client, **fields):
    version = limits_version(panel_client.get("/dashboard/system").text)
    return panel_client.post("/dashboard/system/limits", data={"version": version, **fields},
                             follow_redirects=False)


# --- Validação -------------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("7:00, 10:30 12:30;15:30,18:30", "07:00,10:30,12:30,15:30,18:30"),
    ("06:00,08:00,12:00,16:00,23:00", "06:00,08:00,12:00,16:00,23:00"),
])
def test_horarios_validos_sao_normalizados(raw, expected):
    assert vault.validate(vault.item("slot_times"), raw) == expected


@pytest.mark.parametrize("raw,why", [
    ("07:00,10:30,12:30,15:30", "são 5 horários"),
    ("07:00,10:30,12:30,15:30,18:30,20:00", "são 5 horários"),
    ("07:00,10:30,12:30,15:30,25:00", "não é HH:MM"),
    ("07h00,10:30,12:30,15:30,18:30", "não é HH:MM"),
    ("10:30,07:00,12:30,15:30,18:30", "em ordem"),
    ("07:00,07:30,12:30,15:30,18:30", "pelo menos 60 min"),
    ("07:00,10:30,12:30,15:30,23:30", "até 23:00"),
])
def test_horarios_invalidos_sao_recusados(raw, why):
    with pytest.raises(vault.VaultError, match=why):
        vault.validate(vault.item("slot_times"), raw)


def test_faixas_dos_limites():
    cap = vault.item("llm_job_cap")
    assert vault.validate(cap, "0") == "0" and vault.validate(cap, "040") == "40"
    for bad in ("10", "-1", "501", "abc", "1.5"):
        with pytest.raises(vault.VaultError):
            vault.validate(cap, bad)
    assert vault.validate(vault.item("searxng_daily_cap"), "0") == "0"
    with pytest.raises(vault.VaultError):
        vault.validate(vault.item("searxng_job_cap"), "201")
    with pytest.raises(vault.VaultError, match="maior que o teto do dia"):
        vault.check_limits({"searxng_daily_cap": "10", "searxng_job_cap": "20"})
    vault.check_limits({"searxng_daily_cap": "0", "searxng_job_cap": "20"})  # busca desligada


# --- Leitura: cofre > ambiente --------------------------------------------------------
def test_limite_vem_do_ambiente_e_o_cofre_tem_precedencia(monkeypatch):
    monkeypatch.setattr(settings, "llm_job_cap", 40)
    assert vault.limit("llm_job_cap") == 40
    assert vault.effective("llm_job_cap") == ("40", "ambiente")
    put("llm_job_cap", "25")
    assert vault.limit("llm_job_cap") == 25


def test_ambiente_vale_como_esta_mesmo_fora_da_faixa_da_tela(monkeypatch):
    monkeypatch.setattr(settings, "llm_job_cap", 2)
    assert vault.limit("llm_job_cap") == 2


def test_valor_estragado_no_cofre_cai_para_o_ambiente(monkeypatch):
    monkeypatch.setattr(settings, "searxng_job_cap", 20)
    with session_scope() as db:
        db.execute(text("INSERT INTO system_secrets (key, value_enc) VALUES "
                        "('searxng_job_cap', :v), ('slot_times', :s)"),
                   {"v": vault.encrypt("9999"), "s": vault.encrypt("lixo")})
    vault.invalidate()
    assert vault.limit("searxng_job_cap") == 20
    assert vault.slot_times() == vault.DEFAULT_SLOTS


def test_slot_times_do_ambiente_e_reserva_de_fabrica(monkeypatch):
    monkeypatch.setattr(settings, "slot_times", "06:00,09:00,12:00,15:00,18:00")
    assert vault.slot_times()[0] == time(6, 0)
    monkeypatch.setattr(settings, "slot_times", "estragado")
    assert vault.slot_times() == vault.DEFAULT_SLOTS


# --- Consumidores ---------------------------------------------------------------------
def test_planeador_usa_os_horarios_do_cofre():
    with session_scope() as db:
        sites.sync(db)
        db.execute(text("UPDATE sites SET enabled = (site_key = 'cloud')"))
    put("slot_times", "06:00,09:00,12:00,15:00,18:00")
    with session_scope() as db:
        planner.plan_day(db, DAY, now=BEFORE)
    rows = fetch("SELECT due_at_utc FROM publication_jobs WHERE site_key = 'cloud' "
                 "ORDER BY slot_number LIMIT 1")
    assert rows[0]["due_at_utc"] == datetime(2026, 10, 5, 5, 0, tzinfo=UTC)  # 06:00 Lisboa (UTC+1)


def test_orcamento_de_ia_por_job_le_o_cofre(monkeypatch):
    monkeypatch.setattr(settings, "llm_job_cap", 0)
    with session_scope() as db:
        sites.sync(db)
        job_id = db.execute(text(
            "INSERT INTO publication_jobs (site_key, local_date, slot_number, due_at_utc, "
            "produce_after_utc, status) VALUES ('cloud', '2026-10-05', 1, now(), now(), 'QUEUED') "
            "RETURNING id")).scalar_one()
        for _ in range(15):
            db.execute(text("INSERT INTO provider_usage (provider, kind, model, role, job_id, "
                            "outcome) VALUES ('claude', 'cli', 'm', 'writer', :j, 'ok')"),
                       {"j": job_id})
    with session_scope() as db:
        assert quota.job_blocked(db, job_id) is None          # ambiente: 0 desliga
    put("llm_job_cap", "15")
    with session_scope() as db:
        assert quota.job_blocked(db, job_id) == "llm_job_cap"


def test_teto_de_buscas_le_o_cofre(monkeypatch):
    monkeypatch.setattr(settings, "searxng_daily_cap", 1000)
    monkeypatch.setattr(settings, "searxng_job_cap", 0)
    assert websearch._spend(7, "") == ""
    put("searxng_daily_cap", "0")
    assert websearch._spend(7, "") == "daily"


# --- Portal ---------------------------------------------------------------------------
def test_portal_grava_limites_e_audita_antes_e_depois(panel_client, monkeypatch):
    monkeypatch.setattr(settings, "llm_job_cap", 40)
    page = panel_client.get("/dashboard/system").text
    assert "Limites e horários" in page and "07:00,10:30,12:30,15:30,18:30" in page
    response = post_limits(panel_client, llm_job_cap="30", searxng_daily_cap="",
                           slot_times="07:00,10:30,12:30,15:30,18:30")
    assert "Gravado no cofre: llm_job_cap." in location(response)
    assert vault.effective("llm_job_cap") == ("30", "banco")
    row = fetch("SELECT action, target, details FROM audit_log "
                "WHERE action LIKE 'system.%'")[0]
    assert row["action"] == "system.secret.set" and row["target"] == "secret:llm_job_cap"
    assert row["details"]["before"] == "40" and row["details"]["after"] == "30"


def test_portal_horarios_avisam_que_valem_depois_dos_dias_planejados(panel_client):
    with session_scope() as db:
        sites.sync(db)
        db.execute(text("UPDATE sites SET enabled = true"))
        planner.plan_day(db, DAY, now=BEFORE)
    response = post_limits(panel_client, slot_times="06:30,10:30,12:30,15:30,18:30")
    assert "ainda não planejados (depois de 05/10)" in location(response)
    assert vault.slot_times()[0] == time(6, 30)
    page = panel_client.get("/dashboard/system").text
    assert "já há slots criados até <strong>05/10</strong>" in page


@pytest.mark.parametrize("fields,err", [
    ({"llm_job_cap": "5"}, "pelo menos 15"),
    ({"slot_times": "07:00,08:00"}, "são 5 horários"),
    ({"searxng_daily_cap": "10", "searxng_job_cap": "20"}, "maior que o teto do dia"),
])
def test_portal_recusa_valor_invalido_sem_gravar(panel_client, fields, err):
    assert err in location(post_limits(panel_client, **fields))
    assert fetch("SELECT count(*) AS n FROM system_secrets")[0]["n"] == 0


def test_portal_formulario_velho_e_nada_mudou(panel_client, monkeypatch):
    monkeypatch.setattr(settings, "searxng_job_cap", 20)
    version = limits_version(panel_client.get("/dashboard/system").text)
    assert "Nada mudou" in location(post_limits(panel_client, searxng_job_cap="20"))
    put("searxng_job_cap", "10")
    stale = panel_client.post("/dashboard/system/limits",
                              data={"version": version, "searxng_job_cap": "5"},
                              follow_redirects=False)
    assert "mudou desde que a tela foi aberta" in location(stale)
    assert vault.limit("searxng_job_cap") == 10


def test_portal_voltar_ao_ambiente(panel_client, monkeypatch):
    monkeypatch.setattr(settings, "searxng_job_cap", 20)
    put("searxng_job_cap", "10")
    version = limits_version(panel_client.get("/dashboard/system").text)
    response = panel_client.post("/dashboard/system/clear",
                                 data={"key": "searxng_job_cap", "version": version},
                                 follow_redirects=False)
    assert "agora vale o ambiente" in location(response) and response.headers[
        "location"].endswith("#limits")
    assert vault.limit("searxng_job_cap") == 20


def test_operador_nao_mexe_nos_limites(client):
    login_as(client, "op@example.com", "operador")
    response = client.post("/dashboard/system/limits", data={"llm_job_cap": "100"},
                           follow_redirects=False)
    assert response.status_code == 403
    assert fetch("SELECT count(*) AS n FROM system_secrets")[0]["n"] == 0


def test_cota_diaria_de_ia_no_resumo_e_auditoria_dos_limites(panel_client):
    with session_scope() as db:
        db.execute(text("INSERT INTO ai_providers (name, kind, family, model, base_url, "
                        "enabled) VALUES ('deepx', 'api', 'deepx', 'm1', "
                        "'https://api.deepx.test', false)"))
    page = panel_client.get("/dashboard/system").text
    assert "Chamadas hoje / limite" in page and "0 / sem limite" in page
    panel_client.post("/dashboard/ai/providers/deepx/limits", data={"daily_call_limit": "50"})
    assert "0 / 50" in panel_client.get("/dashboard/system").text
    row = fetch("SELECT action, target, details FROM audit_log "
                "WHERE action = 'ai.provider.limits'")[0]
    assert row["target"] == "ai:deepx"
    assert row["details"]["before"]["daily_call_limit"] is None
    assert row["details"]["after"]["daily_call_limit"] == "50"


def test_orcamento_do_blog_vai_para_a_auditoria(panel_client):
    panel_client.post("/dashboard/ai/site-budgets",
                      data={"site": "cloud", "daily_budget_usd": "1.5"})
    row = fetch("SELECT target, details FROM audit_log WHERE action = 'ai.site_budget'")[0]
    assert row["target"] == "site:cloud"
    assert row["details"]["before"] == {} and row["details"]["after"]["daily_budget_usd"] == "1.50"


def test_painel_mostra_o_primeiro_slot_do_cofre(panel_client):
    put("slot_times", "06:00,09:00,12:00,15:00,18:00")
    assert "1.º slot às 06:00 de Lisboa" in panel_client.get("/").text


def test_cli_status_mostra_os_limites(monkeypatch, capsys):
    monkeypatch.setattr(settings, "llm_job_cap", 40)
    assert vault_cli.main(["import-env", "llm_job_cap", "--apply"]) == 0
    assert vault.effective("llm_job_cap") == ("40", "banco")
    assert vault_cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert re.search(r"llm_job_cap\s+banco", out) and "slot_times" in out
