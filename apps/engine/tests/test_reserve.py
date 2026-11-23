"""D-115: post de reserva por blog — encomenda, cobertura do slot que não sai, validade."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.core.db import session_scope
from app.services import discovery, dispatcher, jobs, publishing, reserve, sites
from app.services.notify import digests
from tests.conftest import advance, fetch

# 21/11/2026 12:00 UTC = 09:00 em São Paulo: dentro da janela da manhã (06:00–12:00).
MORNING = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
PRODUCING = ("QUEUED", "CLAIMED", "RESEARCHING", "WRITING", "VALIDATING", "TRANSLATING",
             "AWAITING_APPROVAL")


@pytest.fixture(autouse=True)
def _tz(monkeypatch):
    monkeypatch.setattr(settings, "tz", "America/Sao_Paulo")


@pytest.fixture
def blog():
    """Só o blog `saude` ligado."""
    with session_scope() as db:
        sites.sync(db)
        db.execute(text("UPDATE sites SET enabled = (site_key = 'saude')"))
    return "saude"


def slot(site: str, due: datetime, status: str = "PLANNED", day: date | None = None) -> int:
    with session_scope() as db:
        return db.execute(
            text("""INSERT INTO publication_jobs
                      (site_key, local_date, slot_number, due_at_utc, produce_after_utc, status)
                    VALUES (:s, :d, 1, :due, :after, :st) RETURNING id"""),
            {"s": site, "d": day or due.date(), "due": due, "after": due - timedelta(hours=14),
             "st": status}).scalar_one()


def approved_reserve(site: str, now: datetime = MORNING, country: str | None = "XX") -> int:
    """Uma reserva produzida e aprovada (o caminho real é o de qualquer job)."""
    with session_scope() as db:
        job_id = reserve.ensure(db, now)[0]
        if country is not None:
            _, idea = discovery.store_idea(db, site_key=site, origin="reserve", url=None,
                                           topic="Entenda como funcionam as vacinas de mRNA",
                                           now=now, country=country)
            db.execute(text("UPDATE publication_jobs SET idea_id = :i WHERE id = :j"),
                       {"i": idea, "j": job_id})
    advance(job_id, *PRODUCING, "APPROVED")
    return job_id


def row(job_id: int) -> dict:
    return dict(fetch("SELECT * FROM publication_jobs WHERE id = :i", i=job_id)[0])


# --- Encomenda -------------------------------------------------------------------------------


def test_janela_da_manha():
    assert reserve.start_at(MORNING) == MORNING
    night = datetime(2026, 11, 21, 23, 0, tzinfo=UTC)          # 20:00 SP
    assert reserve.start_at(night) == datetime(2026, 11, 22, 9, 0, tzinfo=UTC)  # 06:00 SP
    dawn = datetime(2026, 11, 21, 6, 0, tzinfo=UTC)            # 03:00 SP
    assert reserve.start_at(dawn) == datetime(2026, 11, 21, 9, 0, tzinfo=UTC)
    # wait: nunca já, mesmo dentro da janela
    assert reserve.start_at(MORNING, wait=True) == datetime(2026, 11, 22, 9, 0, tzinfo=UTC)


def test_uma_reserva_por_blog_ligado_e_idempotente(blog):
    with session_scope() as db:
        created = reserve.ensure(db, MORNING)
        again = reserve.ensure(db, MORNING)
    assert len(created) == 1 and again == []
    job = row(created[0])
    assert (job["site_key"], job["slot_number"], job["reserve"], job["status"]) == (
        blog, 0, True, "PLANNED")
    assert job["due_at_utc"] == MORNING + timedelta(days=settings.reserve_valid_days)
    assert job["produce_after_utc"] == MORNING


def test_desligada_nao_encomenda(blog, monkeypatch):
    monkeypatch.setattr(settings, "reserve_posts", False)
    with session_scope() as db:
        assert reserve.ensure(db, MORNING) == []


def test_reserva_que_bloqueou_e_trocada_so_na_proxima_janela(blog):
    with session_scope() as db:
        first = reserve.ensure(db, MORNING)[0]
    advance(first, "QUEUED", "CLAIMED", "RESEARCHING", "BLOCKED")
    with session_scope() as db:
        second = reserve.ensure(db, MORNING)[0]
    job = row(second)
    assert job["local_date"] != row(first)["local_date"]       # data seguinte, mesmo dia real
    assert job["produce_after_utc"] == datetime(2026, 11, 22, 9, 0, tzinfo=UTC)


# --- Cobertura -------------------------------------------------------------------------------


def test_slot_bloqueado_recebe_a_reserva_aprovada_e_ela_publica_no_horario(blog):
    due = MORNING + timedelta(hours=8)
    failed = slot(blog, due)
    advance(failed, "QUEUED", "CLAIMED", "RESEARCHING", "BLOCKED", blocked_reason="sem fontes")
    kept = approved_reserve(blog)
    with session_scope() as db:
        assert publishing.due_jobs(db, MORNING + timedelta(days=40), "live") == []
        assert reserve.cover(db, MORNING) == [(failed, kept)]
        assert reserve.cover(db, MORNING) == []                 # cobre uma vez só
    job, old = row(kept), row(failed)
    assert (job["reserve"], job["due_at_utc"], job["covers_job_id"]) == (False, due, failed)
    assert job["local_date"] == old["local_date"]              # conta no dia do slot
    assert old["status"] == "EXPIRED"
    assert old["blocked_reason"].startswith(reserve.COVERED_PREFIX)
    with session_scope() as db:
        assert publishing.due_jobs(db, MORNING, "live") == []   # antes do horário, não
        assert publishing.due_jobs(db, due, "live") == [kept]


def test_sem_reserva_aprovada_o_slot_segue_como_antes(blog):
    failed = slot(blog, MORNING + timedelta(hours=8))
    advance(failed, "QUEUED", "CLAIMED", "RESEARCHING", "BLOCKED")
    with session_scope() as db:
        job_id = reserve.ensure(db, MORNING)[0]                 # ainda a produzir
        assert reserve.cover(db, MORNING) == []
    assert row(failed)["status"] == "BLOCKED" and row(job_id)["reserve"]


@pytest.mark.parametrize(("status", "retry", "hours", "covered"), [
    ("RETRY_WAIT", 12, 8, True),        # a próxima tentativa cai depois do slot (job 372)
    ("RETRY_WAIT", 3, 8, False),        # ainda dá tempo de tentar
    ("RETRY_WAIT", 1, -1, True),        # o slot passou e o job não saiu
    ("PLANNED", None, -1, True),
    ("PLANNED", None, 8, False),
    ("AWAITING_APPROVAL", None, -1, False),   # a decisão é do Euclides
    ("APPROVED", None, -1, False),
])
def test_quando_o_slot_e_coberto(blog, status, retry, hours, covered):
    due = MORNING + timedelta(hours=hours)
    job_id = slot(blog, due)
    path = {"RETRY_WAIT": ("QUEUED", "CLAIMED", "RESEARCHING", "RETRY_WAIT"),
            "PLANNED": (), "AWAITING_APPROVAL": PRODUCING,
            "APPROVED": (*PRODUCING, "APPROVED")}[status]
    extra = {"retry_after_utc": MORNING + timedelta(hours=retry)} if retry else {}
    advance(job_id, *path, **extra)
    approved_reserve(blog)
    with session_scope() as db:
        assert bool(reserve.cover(db, MORNING)) is covered


def test_slot_rejeitado_e_coberto_sem_mudar_de_estado(blog):
    due = MORNING + timedelta(hours=8)
    rejected = slot(blog, due)
    advance(rejected, *PRODUCING, "REJECTED")
    kept = approved_reserve(blog)
    with session_scope() as db:
        assert reserve.cover(db, MORNING) == [(rejected, kept)]
    assert row(rejected)["status"] == "REJECTED"
    assert fetch("SELECT 1 FROM publication_events WHERE job_id = :j "
                 "AND event_type = 'covered_by_reserve'", j=rejected)


def test_reserva_respeita_a_cota_mundial_na_hora_de_cobrir(blog):
    """D-110: a reserva sobre o Brasil não entra se a semana já teve o post do Brasil."""
    with session_scope() as db:
        _, idea = discovery.store_idea(db, site_key=blog, origin="rss", now=MORNING,
                                       url="https://example.org/a", topic="Notícia do Brasil",
                                       country="BR")
    published = slot(blog, MORNING - timedelta(days=1))
    advance(published, *PRODUCING, "APPROVED", "PUBLISHING", "PUBLISHED_CONFIRMED",
            idea_id=idea)
    failed = slot(blog, MORNING + timedelta(hours=8))
    advance(failed, "QUEUED", "CLAIMED", "RESEARCHING", "BLOCKED")
    approved_reserve(blog, country="BR")
    with session_scope() as db:
        assert reserve.cover(db, MORNING) == []


# --- Reconciliação e validade ----------------------------------------------------------------


def test_reconciliacao_cobre_e_encomenda_a_proxima(blog):
    failed = slot(blog, MORNING + timedelta(hours=8))
    advance(failed, "QUEUED", "CLAIMED", "RESEARCHING", "BLOCKED")
    kept = approved_reserve(blog)
    with session_scope() as db:
        result = dispatcher.reconcile(db, MORNING)
    assert result.covered == [[failed, kept]]
    assert len(result.reserves) == 1 and result.reserves[0] != kept
    assert row(result.reserves[0])["reserve"]


def test_reserva_aprovada_espera_sem_prazo_de_progresso_e_vence_na_validade(blog):
    kept = approved_reserve(blog)
    with session_scope() as db:
        db.execute(text("UPDATE publication_jobs SET updated_at = now() - interval '5 days' "
                        "WHERE id = :i"), {"i": kept})
        assert kept not in dispatcher.reconcile(db, datetime.now(UTC)).expired
    assert row(kept)["status"] == "APPROVED"
    with session_scope() as db:
        later = row(kept)["due_at_utc"] + timedelta(minutes=1)
        assert kept in dispatcher.reconcile(db, later).expired


def test_publicar_a_reserva_a_mao_e_recusado(blog, panel_client):
    kept = approved_reserve(blog)
    page = panel_client.post(f"/dashboard/review/{kept}/publish", data={"early": "1"},
                             follow_redirects=False)
    assert "reserva+do+blog" in page.headers["location"]
    assert not fetch("SELECT 1 FROM publication_events WHERE job_id = :j "
                     "AND event_type = 'publish_requested'", j=kept)


# --- E-mail --------------------------------------------------------------------------------


def test_lote_diz_o_que_acontece_com_o_horario(blog, monkeypatch):
    monkeypatch.setattr(settings, "approval_notify_email", "ed@example.com")
    monkeypatch.setattr(settings, "public_base_url", "https://painel.exemplo.test")
    sent: list[str] = []
    waiting = slot(blog, MORNING + timedelta(hours=8))
    advance(waiting, "QUEUED", "CLAIMED", "RESEARCHING", "RETRY_WAIT",
            retry_after_utc=MORNING + timedelta(hours=2), blocked_reason="fontes insuficientes")
    approved_reserve(blog)
    result = digests.approval_batch(
        MORNING, lambda to, name, subject, html: sent.append(html) or "ref")
    assert result["sent"] is True and result["jobs"] == 0 and result["missing"] == 1
    assert "Não vieram neste lote" in sent[0]
    assert "a reserva aprovada" in sent[0] and "publica no horário" in sent[0]


def test_slot_coberto_aparece_como_coberto(blog):
    failed = slot(blog, MORNING + timedelta(hours=8))
    advance(failed, "QUEUED", "CLAIMED", "RESEARCHING", "BLOCKED")
    approved_reserve(blog)
    with session_scope() as db:
        reserve.cover(db, MORNING)
        missing = digests._missing_jobs(db, row(failed)["local_date"])
    html = digests._approval_html([], "https://x", missing, {})
    assert "coberto pela reserva" in html and "#cfe3d4" in html


def test_reserva_guardada_nao_aparece_como_slot(blog):
    kept = approved_reserve(blog)
    with session_scope() as db:
        jobs.transition(db, kept, "EXPIRED")
        missing = digests._missing_jobs(db, row(kept)["local_date"])
    assert missing == []


# --- Post atrasado (22/11, job 372) ----------------------------------------------------------

# Slot da véspera (20/11) e o de hoje (21/11), no fuso do blog (Lisboa); MORNING é 21/11.
YESTERDAY = datetime(2026, 11, 20, 10, 0, tzinfo=UTC)
TODAY = datetime(2026, 11, 21, 17, 0, tzinfo=UTC)


def late_job(site: str) -> int:
    """Produzido para a véspera e aprovado só hoje."""
    job_id = slot(site, YESTERDAY)
    advance(job_id, *PRODUCING, "APPROVED")
    return job_id


def test_aprovado_depois_do_dia_nao_sai_junto_com_o_de_hoje(blog):
    late = late_job(blog)
    today = slot(blog, TODAY)
    advance(today, *PRODUCING, "APPROVED")
    with session_scope() as db:
        assert reserve.is_late(db, late, MORNING)
        assert not reserve.is_late(db, today, MORNING)
        assert publishing.due_jobs(db, MORNING, "live") == []
        assert publishing.due_jobs(db, TODAY, "live") == [today]
        # Aprovado ainda no dia dele (antes da meia-noite no fuso do blog, Lisboa) não é atrasado.
        assert not reserve.is_late(db, late, datetime(2026, 11, 20, 23, 30, tzinfo=UTC))


def test_atrasado_cobre_o_slot_que_falha_antes_da_reserva(blog):
    kept = approved_reserve(blog)
    late = late_job(blog)
    failed = slot(blog, TODAY)
    advance(failed, "QUEUED", "CLAIMED", "RESEARCHING", "BLOCKED", blocked_reason="sem fontes")
    with session_scope() as db:
        assert reserve.cover(db, MORNING) == [(failed, late)]
    job, old = row(late), row(failed)
    assert (job["due_at_utc"], job["covers_job_id"], job["local_date"]) == (
        TODAY, failed, YESTERDAY.date())
    assert old["status"] == "EXPIRED" and old["blocked_reason"].startswith(reserve.LATE_PREFIX)
    assert row(kept)["reserve"] is True                        # a reserva continua guardada
    with session_scope() as db:
        assert not reserve.is_late(db, late, MORNING)
        assert publishing.due_jobs(db, TODAY, "live") == [late]
        missing = digests._missing_jobs(db, old["local_date"])
    html = digests._approval_html([], "https://x", missing, {})
    assert "coberto pelo post atrasado" in html and "#cfe3d4" in html


def test_atrasado_nao_cobre_slot_do_proprio_dia_nem_depois_do_prazo(blog):
    late = late_job(blog)
    with session_scope() as db:                    # o outro slot do mesmo dia (20/11)
        db.execute(text("UPDATE publication_jobs SET slot_number = 2 WHERE id = :i"),
                   {"i": late})
    same_day = slot(blog, YESTERDAY + timedelta(hours=3), day=YESTERDAY.date())
    advance(same_day, "QUEUED", "CLAIMED", "RESEARCHING", "BLOCKED")
    with session_scope() as db:
        assert reserve.cover(db, MORNING) == []
    failed = slot(blog, TODAY)
    advance(failed, "QUEUED", "CLAIMED", "RESEARCHING", "BLOCKED")
    after = YESTERDAY + timedelta(hours=settings.late_hold_hours, minutes=1)
    with session_scope() as db:
        assert reserve.cover(db, after) == []
    assert row(late)["covers_job_id"] is None


def test_atrasado_sem_slot_para_cobrir_expira(blog):
    late = late_job(blog)
    with session_scope() as db:
        assert late not in dispatcher.reconcile(db, MORNING).expired
    with session_scope() as db:
        after = YESTERDAY + timedelta(hours=settings.late_hold_hours, minutes=1)
        assert late in dispatcher.reconcile(db, after).expired


def test_painel_explica_e_nao_publica_o_atrasado(blog, panel_client):
    late = late_job(blog)
    page = panel_client.post(f"/dashboard/review/{late}/publish", follow_redirects=False)
    assert "dia+que+j" in page.headers["location"]
    assert not fetch("SELECT 1 FROM publication_events WHERE job_id = :j "
                     "AND event_type = 'publish_requested'", j=late)
    assert "Post atrasado" in panel_client.get(f"/dashboard/review/{late}").text
    assert "atrasado" in panel_client.get("/dashboard/review").text


def test_lote_marca_o_que_chega_depois_do_dia(blog, monkeypatch):
    monkeypatch.setattr(settings, "approval_notify_email", "ed@example.com")
    monkeypatch.setattr(settings, "public_base_url", "https://painel.exemplo.test")
    sent: list[str] = []
    waiting = slot(blog, YESTERDAY)
    advance(waiting, *PRODUCING)
    digests.approval_batch(datetime.now(UTC),
                           lambda to, name, subject, html: sent.append(html) or "ref")
    assert "<b>atrasado</b>" in sent[0] and "20/11" in sent[0]


def test_lote_diz_ate_quando_a_reserva_cobre_o_job_que_nao_sai(blog, monkeypatch):
    """22/11: a reserva da Política só cobria o 386 se aprovada até 04:28 — o lote não dizia."""
    monkeypatch.setattr(settings, "approval_notify_email", "ed@example.com")
    monkeypatch.setattr(settings, "public_base_url", "https://painel.exemplo.test")
    now = datetime.now(UTC)
    failed = slot(blog, now - timedelta(hours=2))
    advance(failed, "QUEUED", "CLAIMED", "RESEARCHING", "BLOCKED", blocked_reason="sem fontes")
    with session_scope() as db:
        kept = reserve.ensure(db, now)[0]
    advance(kept, *PRODUCING)
    sent: list[str] = []
    digests.approval_batch(now, lambda to, name, subject, html: sent.append(html) or "ref")
    limit = reserve.cover_deadline("BLOCKED", row(failed)["due_at_utc"])
    when = digests._when(limit)
    assert f"aprove até {when} para cobrir o job #{failed}" in sent[0]
    assert f"aprovada até {when}, cobre o horário deste job" in sent[0]
    # Passado o prazo, o lote diz que já não dá.
    late = {**dict(row(failed)), "due_at_utc": now - timedelta(hours=30)}
    note = digests._reserve_note(late, {blog: {"status": "AWAITING_APPROVAL", "id": kept}}, now)
    assert "o prazo para cobrir este horário já passou" in note


def test_prazo_de_cobertura_segue_a_expiracao():
    due = datetime(2026, 11, 22, 7, 28, tzinfo=UTC)
    blocked = due + timedelta(hours=settings.job_expire_hours)
    assert reserve.cover_deadline("BLOCKED", due) == blocked
    assert reserve.cover_deadline("QUEUED", due) == due + timedelta(
        hours=settings.late_production_hours)
