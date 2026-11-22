"""E10.4 — publicação na hora pelo motor (`publish-due`) e prazos de expiração."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.db import session_scope
from app.services import dispatcher, jobs, publishing
from tests.conftest import advance, fetch
from tests.test_sites import cloud, insert_job  # noqa: F401  (fixture `cloud`)

DUE = datetime(2026, 10, 5, 11, 30, tzinfo=UTC)  # a hora do slot de `insert_job`
BEFORE = DUE - timedelta(minutes=10)
AFTER = DUE + timedelta(minutes=10)
TO_APPROVAL = (
    "QUEUED",
    "CLAIMED",
    "RESEARCHING",
    "WRITING",
    "VALIDATING",
    "TRANSLATING",
    "AWAITING_APPROVAL",
)


@pytest.fixture
def queued(monkeypatch) -> list[int]:
    calls: list[int] = []
    monkeypatch.setattr(publishing, "enqueue_publish", calls.append)
    return calls


@pytest.fixture
def enabled(cloud):  # noqa: F811
    with session_scope() as db:
        db.execute(text("UPDATE sites SET enabled = true WHERE site_key = :k"), {"k": cloud})
    return cloud


def approved(site: str, slot: int = 1) -> int:
    job = insert_job(site, slot, "PLANNED")
    advance(job, *TO_APPROVAL, "APPROVED")
    return job


def of_today(job: int) -> int:
    """Os testes com o relógio real: o slot é de hoje no fuso do blog (senão é um post
    atrasado, que espera um slot para cobrir — `reserve.LATE_SQL`)."""
    with session_scope() as db:
        db.execute(text("""UPDATE publication_jobs j SET local_date =
                              (now() AT TIME ZONE s.timezone)::date
                             FROM sites s WHERE s.site_key = j.site_key AND j.id = :i"""),
                   {"i": job})
    return job


def run(now: datetime, mode: str = "draft") -> dict:
    with session_scope() as db:
        return publishing.publish_due(db, now, mode)


def test_antes_da_hora_do_slot_nao_publica_e_na_hora_publica(enabled, queued):
    job = approved(enabled)
    assert run(BEFORE)["requested"] == [] and queued == []
    assert run(AFTER)["requested"] == [job] and queued == [job]
    event = fetch(
        "SELECT actor, details FROM publication_events WHERE job_id = :i "
        "AND event_type = 'publish_requested'",
        i=job,
    )[0]
    assert event["actor"] == "scheduler" and event["details"]["mode"] == "draft"


def test_aprovado_depois_do_slot_publica_logo_na_corrida_seguinte(enabled, queued):
    job = insert_job(enabled, 1, "PLANNED")
    assert run(AFTER)["requested"] == []  # ainda não aprovado
    advance(job, *TO_APPROVAL, "APPROVED")
    assert run(AFTER + timedelta(minutes=1))["requested"] == [job]


def test_pedido_recente_nao_se_repete_e_em_draft_so_uma_vez(enabled, queued):
    job = of_today(approved(enabled))
    now = datetime.now(UTC)                       # os eventos levam a hora real da base
    assert run(now)["requested"] == [job]
    assert run(now + timedelta(seconds=30))["requested"] == []          # ainda a correr
    with session_scope() as db:
        jobs.record_event(db, job, "drafts_created", actor="publisher", details={})
    later = now + timedelta(minutes=5)
    assert run(later)["requested"] == []                                # rascunhos já criados
    assert run(later, "live")["requested"] == [job]                     # em live, publica


def test_blog_desligado_nao_publica(cloud, queued):  # noqa: F811
    approved(cloud)
    assert run(AFTER)["requested"] == []


def test_um_por_blog_o_mais_antigo_e_nunca_com_outro_a_publicar(enabled, queued):
    first, second = approved(enabled, 1), approved(enabled, 2)
    assert run(AFTER)["requested"] == [first]
    advance(first, "PUBLISHING")  # entra em publicação: o blog fica ocupado
    assert run(AFTER + timedelta(minutes=5))["requested"] == []
    assert (
        fetch("SELECT status FROM publication_jobs WHERE id = :i", i=second)[0]["status"]
        == "APPROVED"
    )


def test_em_live_retoma_o_que_esta_em_espera(enabled, queued):
    wait = approved(enabled, 1)
    advance(wait, "PUBLISHING", "RETRY_WAIT", retry_after_utc=AFTER + timedelta(hours=1))
    assert run(AFTER, "live")["requested"] == []  # ainda não chegou a hora de repetir
    assert run(AFTER + timedelta(hours=2), "live")["requested"] == [wait]
    assert run(AFTER + timedelta(hours=3), "draft")["requested"] == []  # em draft só APPROVED


def test_rota_exige_token_e_devolve_o_pedido(client, auth, enabled, queued):
    job = approved(enabled)
    assert client.post("/api/sched/publish-due").status_code in (401, 403)
    response = client.post("/api/sched/publish-due", headers=auth)
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "draft" and body["requested"] in ([job], [])  # depende do relógio real


# --- Prazos ---------------------------------------------------------------------------------


def test_sem_aprovacao_expira_72_horas_depois_do_slot(enabled):
    job = insert_job(enabled, 1, "PLANNED")
    advance(job, *TO_APPROVAL)
    with session_scope() as db:
        assert job not in dispatcher.reconcile(db, DUE + timedelta(hours=71)).expired
    with session_scope() as db:
        assert job in dispatcher.reconcile(db, DUE + timedelta(hours=73)).expired


def test_aprovado_tarde_nao_expira_antes_de_publicar(enabled):
    """Aprovar às 60 h após o slot: o prazo conta desde a aprovação, não desde o slot."""
    job = of_today(approved(enabled))
    with session_scope() as db:
        assert job not in dispatcher.reconcile(db, datetime.now(UTC) + timedelta(hours=1)).expired
    with session_scope() as db:
        assert job in dispatcher.reconcile(db, datetime.now(UTC) + timedelta(hours=25)).expired


# --- 21/11: RETRY_WAIT da produção nunca vai para a publicação; slot passado expira ----------


def test_espera_da_producao_sem_aprovacao_nao_e_pedida_para_publicar(enabled, queued):
    """O 372 (nunca aprovado, à espera da cota de IA) recebeu 3 pedidos de publicação."""
    job = insert_job(enabled, 1, "PLANNED")
    advance(job, "QUEUED", "CLAIMED", "RESEARCHING", "RETRY_WAIT",
            retry_after_utc=AFTER - timedelta(minutes=1))
    assert run(AFTER, "live")["requested"] == [] and queued == []


def test_producao_atrasada_expira_pelo_horario_do_slot_mesmo_com_tentativas_recentes(enabled):
    late = insert_job(enabled, 1, "PLANNED")
    advance(late, "QUEUED", "CLAIMED", "RESEARCHING", "RETRY_WAIT")
    paid = approved(enabled, 2)                       # aprovado a repetir a publicação
    advance(paid, "PUBLISHING", "RETRY_WAIT")
    with session_scope() as db:                       # tentativas recentes: updated_at é agora
        db.execute(text("UPDATE publication_jobs SET updated_at = now(), due_at_utc = :d "
                        "WHERE id IN (:a, :b)"),
                   {"d": datetime.now(UTC) - timedelta(hours=3), "a": late, "b": paid})
        assert dispatcher.reconcile(db, datetime.now(UTC)).expired == []    # 3 h: ainda vale
        db.execute(text("UPDATE publication_jobs SET due_at_utc = :d WHERE id IN (:a, :b)"),
                   {"d": datetime.now(UTC) - timedelta(hours=7), "a": late, "b": paid})
        expired = dispatcher.reconcile(db, datetime.now(UTC)).expired
    assert late in expired and paid not in expired    # o aprovado só pela falta de progresso
