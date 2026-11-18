"""E10.7 — a aprovação é só humana (D-006): nada na API de token aprova ou rejeita (R23)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.db import session_scope
from app.main import app
from app.services import jobs
from tests.conftest import advance, fetch
from tests.test_sites import cloud, insert_job  # noqa: F401  (fixture `cloud`)


def decision_routes() -> list[tuple[str, str]]:
    """Rotas que decidem (`/approve`, `/reject`); `approval-batch` só envia e-mail."""
    paths = app.openapi()["paths"]
    return [
        (path, method.upper())
        for path, item in paths.items()
        for method in item
        if path.startswith("/api/") and path.rstrip("/").endswith(("/approve", "/reject"))
    ]


def test_nao_ha_rotas_de_aprovacao_na_api_de_token():
    # As únicas que existiam eram as de recusa de `/api/articles` (pipeline antigo), que saiu
    # na E19. Uma rota nova que decida pela API de token reprova aqui.
    assert decision_routes() == []


def test_nenhuma_rota_do_motor_com_token_leva_um_job_a_aprovado(client, auth, cloud):  # noqa: F811
    job = insert_job(cloud, 1, "PLANNED")
    advance(
        job,
        "QUEUED",
        "CLAIMED",
        "RESEARCHING",
        "WRITING",
        "VALIDATING",
        "TRANSLATING",
        "AWAITING_APPROVAL",
    )
    for path in (
        "/api/sched/plan-day",
        "/api/sched/dispatch",
        "/api/sched/reconcile",
        "/api/sched/publish-due",
        "/api/sched/approval-batch",
        "/api/sched/digest",
    ):
        client.post(path, headers=auth)
    status = fetch("SELECT status FROM publication_jobs WHERE id = :i", i=job)[0]["status"]
    assert status == "AWAITING_APPROVAL"
    assert fetch("SELECT count(*) AS n FROM approvals")[0]["n"] == 0


def test_aprovado_exige_uma_decisao_humana_registada(cloud):  # noqa: F811
    job = insert_job(cloud, 1, "PLANNED")
    advance(
        job,
        "QUEUED",
        "CLAIMED",
        "RESEARCHING",
        "WRITING",
        "VALIDATING",
        "TRANSLATING",
        "AWAITING_APPROVAL",
    )
    with session_scope() as db, pytest.raises(ValueError, match="D-006"):
        jobs.transition(db, job, "APPROVED", actor="n8n", expected="AWAITING_APPROVAL")
    with session_scope() as db:
        db.execute(
            text(
                "INSERT INTO approvals (job_id, decision, decided_by, channel) "
                "VALUES (:j, 'approved', 'euclides@example.com', 'panel')"
            ),
            {"j": job},
        )
        jobs.transition(db, job, "APPROVED", actor="painel", expected="AWAITING_APPROVAL")
    assert (
        fetch("SELECT status FROM publication_jobs WHERE id = :i", i=job)[0]["status"] == "APPROVED"
    )
