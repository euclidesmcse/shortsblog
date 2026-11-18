"""E12.1 — métricas do motor multi-site."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.db import session_scope
from app.services import telemetry
from tests.conftest import advance, fetch
from tests.test_sites import (  # noqa: F401  (fixture `cloud`: os sites sincronizados)
    cloud,
    insert_job,
)

NOW = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)  # dia 2026-10-05 em São Paulo (como `insert_job`)
UP = (
    "QUEUED",
    "CLAIMED",
    "RESEARCHING",
    "WRITING",
    "VALIDATING",
    "TRANSLATING",
    "AWAITING_APPROVAL",
)


def collect(now: datetime = NOW) -> list[telemetry.Sample]:
    with session_scope() as db:
        return telemetry.samples(db, now)


def value(rows, metric: str, **labels) -> float | None:
    for n, _help, lab, v in rows:
        if n == metric and all(lab.get(k) == x for k, x in labels.items()):
            return v
    return None


@pytest.mark.parametrize(
    "reason, category",
    [
        ("fontes insuficientes: 1 de 2", "fontes_insuficientes"),
        ("revisão: reprodução das fontes", "revisao"),
        ("gates: texto curto: 87 palavras", "gates"),
        ("tradução zh-CN: números", "traducao"),
        ("o redator recusou o tema: x", "redator_recusou"),
        ("pipeline editorial (E6)", "pipeline"),
        (None, "outro"),
        ("qualquer coisa", "outro"),
    ],
)
def test_categoria_do_motivo(reason, category):
    assert telemetry.reason_category(reason) == category


def test_jobs_e_slots_de_hoje_por_blog_e_motivos(cloud):  # noqa: F811
    a, b = insert_job("cloud", 1, "PLANNED"), insert_job("cloud", 2, "PLANNED")
    c = insert_job("ia", 1, "PLANNED")
    advance(a, *UP)
    advance(b, "QUEUED", "CLAIMED", "BLOCKED", blocked_reason="fontes insuficientes: 1 de 2")
    advance(c, "QUEUED", "CLAIMED", "BLOCKED", blocked_reason="coisa nova")
    rows = collect()
    assert value(rows, "sb_slots_planned_today", site="cloud") == 2
    assert value(rows, "sb_jobs_today", site="cloud", status="AWAITING_APPROVAL") == 1
    assert value(rows, "sb_jobs_today", site="cloud", status="BLOCKED") == 1
    assert value(rows, "sb_jobs_blocked_today", site="cloud", reason="fontes_insuficientes") == 1
    assert value(rows, "sb_jobs_blocked_today", site="ia", reason="outro") == 1
    assert collect(NOW + timedelta(days=2)) == [
        r for r in collect(NOW + timedelta(days=2))
    ]  # outro dia: sem jobs
    assert value(collect(NOW + timedelta(days=2)), "sb_slots_planned_today", site="cloud") is None


def test_jobs_presos(cloud):  # noqa: F811
    with session_scope() as db:
        db.execute(text("UPDATE sites SET enabled = true WHERE site_key = 'cloud'"))
    queued = insert_job("cloud", 1, "PLANNED")
    advance(queued, "QUEUED")
    other = insert_job("ia", 1, "PLANNED")  # blog desligado: não conta
    advance(other, "QUEUED")
    approval = insert_job("cloud", 2, "PLANNED")
    advance(approval, *UP)
    with session_scope() as db:
        db.execute(
            text("UPDATE publication_jobs SET updated_at = :t WHERE id = :i"),
            {"t": NOW - timedelta(hours=30), "i": approval},
        )
    rows = collect(NOW + timedelta(hours=1))
    assert value(rows, "sb_jobs_stuck", kind="queued_waiting") == 1
    assert value(rows, "sb_jobs_stuck", kind="approval_waiting") == 1
    assert value(rows, "sb_jobs_stuck", kind="lease_expired") == 0


def test_duracao_por_etapa_e_tempo_ate_a_aprovacao(cloud):  # noqa: F811
    job = insert_job("cloud", 1, "PLANNED")
    start = datetime.now(UTC) - timedelta(hours=3)
    steps = [
        ("WRITING", 0),
        ("VALIDATING", 120),
        ("AWAITING_APPROVAL", 180),
        ("APPROVED", 180 + 3600),
    ]
    with session_scope() as db:
        for status, offset in steps:
            db.execute(
                text(
                    "INSERT INTO publication_events (job_id, event_type, to_status, actor, "
                    "timestamp_utc) VALUES (:j, 'status_changed', :s, 't', :t)"
                ),
                {"j": job, "s": status, "t": start + timedelta(seconds=offset)},
            )
    rows = collect()
    assert value(rows, "sb_stage_avg_seconds", stage="writing") == 120
    assert value(rows, "sb_stage_avg_seconds", stage="validating") == 60
    assert value(rows, "sb_time_to_approval_seconds") == 3600


def test_provedores_custos_estado_e_orcamentos(cloud):  # noqa: F811
    job = insert_job("cloud", 1, "PLANNED")
    with session_scope() as db:
        db.execute(
            text(
                "UPDATE ai_providers SET enabled = true, daily_call_limit = 10 "
                "WHERE name = 'claude'"
            )
        )
        db.execute(
            text("""INSERT INTO provider_usage (provider, kind, role, site, cost_usd, job_id,
                                                outcome)
                           SELECT 'claude', 'subscription', 'writer', 'cloud', 0, :j, 'ok'
                             FROM generate_series(1, 8)"""),
            {"j": job},
        )
        db.execute(
            text("""INSERT INTO provider_usage (provider, kind, role, site, cost_usd, job_id,
                                                outcome)
                           VALUES ('openrouter', 'api', 'writer', 'cloud', 0.5, :j, 'ok'),
                                  ('openrouter', 'api', 'writer', 'cloud', 0.25, :j,
                                   'rate_limited')"""),
            {"j": job},
        )
        db.execute(
            text("INSERT INTO ai_site_budgets (site, daily_budget_usd) VALUES ('cloud', 1.00)")
        )
        db.execute(
            text(
                "INSERT INTO ai_provider_health (provider_id, status, source) "
                "SELECT id, 'not_authenticated', 'probe' FROM ai_providers WHERE name = 'claude'"
            )
        )
    rows = collect(datetime.now(UTC))
    assert value(rows, "sb_provider_calls_24h", provider="claude", outcome="ok") == 8
    assert value(rows, "sb_provider_calls_24h", provider="openrouter", outcome="rate_limited") == 1
    assert value(rows, "sb_provider_cost_usd", provider="openrouter", window="day") == 0.75
    assert value(rows, "sb_provider_status", provider="claude", status="not_authenticated") == 1
    assert (
        value(rows, "sb_budget_used_ratio", scope="provider", name="claude", period="calls_day")
        == 0.8
    )
    assert (
        value(rows, "sb_budget_used_ratio", scope="site", name="cloud", period="cost_day") == 0.75
    )
    assert value(rows, "sb_cost_per_post_usd") == 0.75 and value(rows, "sb_calls_per_post") == 10


def test_metrics_endpoint_expoe_as_novas_metricas_e_nunca_falha(client, cloud):  # noqa: F811
    job = insert_job("cloud", 1, "PLANNED")
    advance(job, *UP)
    response = client.get("/metrics")
    assert response.status_code == 200
    assert 'sb_jobs_stuck{kind="lease_expired"}' in response.text
    assert "sb_provider_status" in response.text
    assert fetch("SELECT 1 FROM publication_jobs WHERE id = :i", i=job)
