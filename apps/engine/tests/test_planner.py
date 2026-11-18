"""E4.3 — planeador diário e E4.4 — máquina de estados."""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import text

from app.core.db import session_scope
from app.services import jobs, planner, sites
from tests.conftest import advance, fetch

DAY = date(2026, 10, 5)  # segunda-feira; Lisboa em UTC+1 (horário de verão até 25/10)
LISBON = ZoneInfo("Europe/Lisbon")
# Relógio fixo: o planeador não cria slots que já passaram, por isso um teste com `DAY` fixo e o
# relógio real só passava de manhã cedo (e deixou de passar quando o calendário chegou a `DAY`).
BEFORE = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
KEYS = ["cloud", "ia", "financas", "saude", "politica", "culinaria"]


@pytest.fixture
def all_enabled():
    """Os seis sites reais, ligados (em produção começam desligados)."""
    with session_scope() as db:
        sites.sync(db)
        db.execute(text("UPDATE sites SET enabled = true"))


def plan(day: date | None = DAY, **kwargs) -> planner.PlanResult:
    if day is not None:
        kwargs.setdefault("now", BEFORE)
    with session_scope() as db:
        return planner.plan_day(db, day, **kwargs)


# --- Planeador (E4.3) -------------------------------------------------------------------


def test_planeador_duas_vezes_no_mesmo_dia_da_30_slots_e_nao_60(all_enabled):
    first, second = plan(), plan()
    assert (first.created, first.existing, first.sites) == (30, 0, 6)
    assert (second.created, second.existing) == (0, 30)
    assert fetch("SELECT count(*) AS n FROM publication_jobs")[0]["n"] == 30
    # Um evento "planned" por slot — a segunda corrida não acrescenta eventos.
    assert fetch("SELECT count(*) AS n FROM publication_events")[0]["n"] == 30


def test_horarios_em_utc_com_desfasamento_por_site(all_enabled):
    plan()
    rows = fetch("""SELECT site_key, slot_number, due_at_utc, produce_after_utc, status,
                           local_date FROM publication_jobs ORDER BY due_at_utc""")
    by = {(r["site_key"], r["slot_number"]): r for r in rows}
    # D-066: 07:00 em Lisboa = 06:00 UTC; cloud sem desfasamento, culinária com 35 min.
    assert by[("cloud", 1)]["due_at_utc"] == datetime(2026, 10, 5, 6, 0, tzinfo=UTC)
    assert by[("ia", 1)]["due_at_utc"] == datetime(2026, 10, 5, 6, 7, tzinfo=UTC)
    assert by[("culinaria", 1)]["due_at_utc"] == datetime(2026, 10, 5, 6, 35, tzinfo=UTC)
    # Último slot do dia: 18:30 + 35 min = 19:05 local = 18:05 UTC (spec §5.2).
    assert by[("culinaria", 5)]["due_at_utc"] == datetime(2026, 10, 5, 18, 5, tzinfo=UTC)
    local_times = [r["due_at_utc"].astimezone(LISBON).strftime("%H:%M")
                   for r in rows if r["site_key"] == "cloud"]
    assert local_times == ["07:00", "10:30", "12:30", "15:30", "18:30"]
    # Seis blogs nunca arrancam no mesmo instante.
    assert len({r["due_at_utc"] for r in rows}) == 30
    assert all(r["status"] == "PLANNED" and r["local_date"] == DAY for r in rows)


def test_producao_comeca_catorze_horas_antes(all_enabled):
    plan()
    rows = fetch("SELECT due_at_utc, produce_after_utc FROM publication_jobs")
    assert all(r["due_at_utc"] - r["produce_after_utc"] == timedelta(hours=14) for r in rows)
    first = fetch("SELECT produce_after_utc FROM publication_jobs "
                  "WHERE site_key = 'cloud' AND slot_number = 1")[0]
    # D-066: o post das 07:00 de Lisboa produz-se a partir das 13:00 de São Paulo da véspera
    # (16:00 UTC), a tempo do lote de aprovação das 16:00 ou das 20:00.
    assert first["produce_after_utc"] == datetime(2026, 10, 4, 16, 0, tzinfo=UTC)


def test_mudanca_de_hora_de_lisboa_mantem_as_07_00_locais(all_enabled):
    # Lisboa sai do horário de verão a 25/10/2026: as 07:00 locais passam de 06:00 a 07:00 UTC
    # (03:00 → 04:00 em São Paulo, que não muda de hora).
    before = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    plan(date(2026, 10, 24), now=before)
    plan(date(2026, 10, 26), now=before)
    due = {r["local_date"]: r["due_at_utc"] for r in fetch(
        "SELECT local_date, due_at_utc FROM publication_jobs "
        "WHERE site_key = 'cloud' AND slot_number = 1")}
    assert due[date(2026, 10, 24)] == datetime(2026, 10, 24, 6, 0, tzinfo=UTC)
    assert due[date(2026, 10, 26)] == datetime(2026, 10, 26, 7, 0, tzinfo=UTC)


def test_sem_data_planeia_hoje_e_amanha_na_data_local_do_site(all_enabled):
    # 23:30 UTC de dia 4 já é dia 5 em Lisboa (00:30), mas ainda dia 4 em São Paulo: vale o fuso
    # do site. Sem data, o planeador cria hoje e amanhã (D-066).
    plan(None, now=datetime(2026, 10, 4, 23, 30, tzinfo=UTC))
    assert {r["local_date"] for r in fetch("SELECT local_date FROM publication_jobs")} == {
        DAY, date(2026, 10, 6)}
    # 22:30 UTC de dia 5 ainda é dia 5 em Lisboa: o dia 5 já passou e o 6 já existe.
    late = datetime(2026, 10, 5, 22, 30, tzinfo=UTC)
    assert planner.local_today("Europe/Lisbon", late) == DAY
    assert plan(None, now=late) == planner.PlanResult(created=0, existing=30, sites=6,
                                                      skipped=30)
    # 23:30 UTC de dia 5 já é dia 6 em Lisboa: cria o dia 7 sem mexer nos anteriores.
    result = plan(None, now=datetime(2026, 10, 5, 23, 30, tzinfo=UTC))
    assert (result.created, result.existing) == (30, 30)
    counts = fetch("SELECT local_date, count(*) AS n FROM publication_jobs GROUP BY 1 ORDER BY 1")
    assert [(r["local_date"], r["n"]) for r in counts] == [
        (DAY, 30), (date(2026, 10, 6), 30), (date(2026, 10, 7), 30)]


def test_sites_desligados_nao_sao_planeados():
    with session_scope() as db:
        sites.sync(db)  # como em produção: todos desligados
    assert plan() == planner.PlanResult(created=0, existing=0, sites=0, skipped=0)
    with session_scope() as db:
        db.execute(text("UPDATE sites SET enabled = true WHERE site_key = 'cloud'"))
    assert plan().created == 5
    assert {r["site_key"] for r in fetch("SELECT site_key FROM publication_jobs")} == {"cloud"}


def test_daily_target_menor_cria_menos_slots(all_enabled):
    with session_scope() as db:
        db.execute(text("UPDATE sites SET daily_target = 2 WHERE site_key = 'ia'"))
        db.execute(text("UPDATE sites SET daily_target = 0 WHERE site_key = 'saude'"))
    assert plan().created == 5 * 4 + 2
    assert [r["slot_number"] for r in fetch(
        "SELECT slot_number FROM publication_jobs WHERE site_key = 'ia' ORDER BY 1")] == [1, 2]


def test_reexecucao_completa_o_que_falta(all_enabled):
    plan()
    with session_scope() as db:
        db.execute(text("DELETE FROM publication_jobs "
                        "WHERE site_key = 'cloud' AND slot_number > 3"))
    result = plan()
    assert (result.created, result.existing) == (2, 28)


def test_reexecucao_nao_mexe_em_jobs_ja_avancados(all_enabled):
    plan()
    job = fetch("SELECT id FROM publication_jobs WHERE site_key = 'cloud' AND slot_number = 1")[0]
    with session_scope() as db:
        jobs.transition(db, job["id"], "QUEUED")
    plan()
    assert fetch("SELECT status FROM publication_jobs WHERE id = :i", i=job["id"])[0][
        "status"] == "QUEUED"


# --- Máquina de estados (E4.4) ---------------------------------------------------------------


@pytest.fixture
def job(all_enabled) -> int:
    plan()
    return fetch("SELECT id FROM publication_jobs WHERE site_key = 'cloud' AND slot_number = 1")[
        0]["id"]


def go(job_id: int, *states: str, **kwargs) -> None:
    advance(job_id, *states, **kwargs)


def status(job_id: int) -> str:
    return fetch("SELECT status FROM publication_jobs WHERE id = :i", i=job_id)[0]["status"]


HAPPY = ("QUEUED", "CLAIMED", "RESEARCHING", "WRITING", "VALIDATING", "TRANSLATING",
         "AWAITING_APPROVAL", "APPROVED", "PUBLISHING", "PUBLISHED_CONFIRMED")


def test_os_17_estados_e_todos_tem_regras():
    assert len(jobs.STATES) == 17 and set(jobs.STATES) == set(jobs.TRANSITIONS)
    assert "READY_TO_PUBLISH" not in jobs.STATES
    for targets in jobs.TRANSITIONS.values():
        assert set(targets) <= set(jobs.STATES)
    assert all(jobs.TRANSITIONS[s] == () for s in jobs.TERMINAL)


def test_caminho_completo_fica_todo_registado(job):
    go(job, *HAPPY, actor="teste", execution_id="exec-1")
    assert status(job) == "PUBLISHED_CONFIRMED"
    events = fetch("""SELECT event_type, from_status, to_status, actor, execution_id
                        FROM publication_events WHERE job_id = :j ORDER BY id""", j=job)
    assert events[0]["event_type"] == "planned"
    changes = [(e["from_status"], e["to_status"]) for e in events[1:]]
    assert changes == list(zip(("PLANNED", *HAPPY[:-1]), HAPPY, strict=True))
    assert all(e["actor"] == "teste" and e["execution_id"] == "exec-1" for e in events[1:])


@pytest.mark.parametrize(
    ("path", "target"),
    [
        ((), "PUBLISHING"),                                # saltar tudo
        ((), "APPROVED"),
        (HAPPY[:6], "PUBLISHING"),                         # TRANSLATING → publicar sem aprovação
        (HAPPY[:7], "PUBLISHING"),                         # AWAITING_APPROVAL → publicar direto
        (HAPPY[:7], "PUBLISHED_CONFIRMED"),
        (HAPPY[:3], "TRANSLATING"),                        # saltar redação e validação
        (HAPPY, "QUEUED"),                                 # estado final não reabre
        (HAPPY[:7] + ("REJECTED",), "APPROVED"),           # rejeitado não volta
        (HAPPY[:2] + ("RETRY_WAIT",), "PUBLISHING"),       # contornar a aprovação pelo retry
    ],
)
def test_transicao_invalida_e_rejeitada(job, path, target):
    go(job, *path)
    before = status(job)
    events_before = len(fetch("SELECT 1 FROM publication_events WHERE job_id = :j", j=job))
    with pytest.raises(jobs.InvalidTransition) as info, session_scope() as db:
        jobs.transition(db, job, target)
    assert (info.value.current, info.value.target) == (before, target)
    assert status(job) == before
    assert len(fetch("SELECT 1 FROM publication_events WHERE job_id = :j", j=job)) == events_before


def test_nao_ha_caminho_para_publicar_sem_aprovacao():
    # D-006: o único estado que leva a PUBLISHING pela primeira vez é APPROVED
    # (VERIFY_REQUIRED só existe depois de já ter estado em PUBLISHING).
    into_publishing = {s for s, targets in jobs.TRANSITIONS.items() if "PUBLISHING" in targets}
    assert into_publishing == {"APPROVED", "VERIFY_REQUIRED"}
    assert {s for s, t in jobs.TRANSITIONS.items() if "VERIFY_REQUIRED" in t} == {"PUBLISHING"}
    # E a APPROVED só se chega por decisão humana ou por nova tentativa de um já aprovado.
    assert {s for s, t in jobs.TRANSITIONS.items() if "APPROVED" in t} == {
        "AWAITING_APPROVAL", "RETRY_WAIT"}


def test_expected_recusa_quando_outro_processo_se_adiantou(job):
    go(job, "QUEUED", "CLAIMED")
    with pytest.raises(jobs.InvalidTransition), session_scope() as db:
        jobs.transition(db, job, "RESEARCHING", expected="QUEUED")
    with session_scope() as db:
        assert jobs.transition(db, job, "RESEARCHING", expected=("CLAIMED", "QUEUED")) == "CLAIMED"


def test_campos_gravados_com_a_transicao_e_lease_limpa_fora_da_producao(job):
    until = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
    go(job, "QUEUED")
    with session_scope() as db:
        jobs.transition(db, job, "CLAIMED", lease_until_utc=until, lease_owner="w1",
                        attempt_count=1)
    row = fetch("SELECT lease_until_utc, lease_owner, attempt_count FROM publication_jobs "
                "WHERE id = :i", i=job)[0]
    assert (row["lease_until_utc"], row["lease_owner"], row["attempt_count"]) == (until, "w1", 1)
    go(job, "RESEARCHING")  # continua em produção: a lease mantém-se
    assert fetch("SELECT lease_owner FROM publication_jobs WHERE id = :i", i=job)[0][
        "lease_owner"] == "w1"
    with session_scope() as db:
        jobs.transition(db, job, "BLOCKED", blocked_reason="sem fontes",
                        details={"gate": "sources"})
    row = fetch("SELECT lease_until_utc, lease_owner, blocked_reason FROM publication_jobs "
                "WHERE id = :i", i=job)[0]
    assert (row["lease_until_utc"], row["lease_owner"], row["blocked_reason"]) == (
        None, None, "sem fontes")
    last = fetch("SELECT details FROM publication_events WHERE job_id = :j ORDER BY id DESC "
                 "LIMIT 1", j=job)[0]
    assert last["details"] == {"gate": "sources"}


def test_erros_de_uso(job):
    with pytest.raises(ValueError, match="estado desconhecido"), session_scope() as db:
        jobs.transition(db, job, "READY_TO_PUBLISH")
    with pytest.raises(ValueError, match="não alteráveis"), session_scope() as db:
        jobs.transition(db, job, "QUEUED", status="PUBLISHING")
    with pytest.raises(ValueError, match="não alteráveis"), session_scope() as db:
        jobs.transition(db, job, "QUEUED", site_key="ia")
    with pytest.raises(jobs.JobNotFound), session_scope() as db:
        jobs.transition(db, 999_999, "QUEUED")


def test_falha_a_meio_nao_deixa_estado_sem_evento(job):
    with pytest.raises(RuntimeError), session_scope() as db:
        jobs.transition(db, job, "QUEUED")
        raise RuntimeError("falha depois da transição, antes do commit")
    assert status(job) == "PLANNED"
    assert len(fetch("SELECT 1 FROM publication_events WHERE job_id = :j", j=job)) == 1


# --- Revisão E4: regras garantidas no ponto único -----------------------------------------


def test_slots_ja_passados_nao_sao_criados(all_enabled):
    # D-034. 10:10 UTC = 11:10 em Lisboa: os slots das 07:00 e das 10:30 (até ao das 11:05
    # da culinária, com o desfasamento de 35 min) já passaram. Amanhã é criado inteiro.
    noon = datetime(2026, 10, 5, 10, 10, tzinfo=UTC)
    result = plan(None, now=noon)
    assert (result.created, result.skipped) == (18 + 30, 12)
    assert sorted({r["slot_number"] for r in fetch(
        "SELECT slot_number FROM publication_jobs WHERE local_date = :d", d=DAY)}) == [3, 4, 5]
    # Site ligado a meio do dia: idem; correr outra vez não cria nem duplica.
    again = plan(None, now=noon)
    assert (again.created, again.existing, again.skipped) == (0, 48, 12)
    # Ao fim do dia já não há nada a criar: hoje passou e amanhã já existe.
    assert plan(None, now=datetime(2026, 10, 5, 21, 0, tzinfo=UTC)).created == 0


def test_estado_ativo_exige_lease(job):
    go(job, "QUEUED")
    for missing in ({}, {"lease_owner": "w"}, {"lease_until_utc": datetime.now(UTC)}):
        with pytest.raises(ValueError, match="exige lease"), session_scope() as db:
            jobs.transition(db, job, "CLAIMED", **missing)
    assert status(job) == "QUEUED"
    go(job, *HAPPY[1:8])                      # …até APPROVED (com aprovação registada)
    with pytest.raises(ValueError, match="exige lease"), session_scope() as db:
        jobs.transition(db, job, "PUBLISHING")    # a lease foi limpa ao sair da produção
    assert status(job) == "APPROVED"


def test_aprovado_exige_decisao_humana_registada(job):
    with session_scope() as db:
        for state in HAPPY[:7]:
            extra = ({"lease_owner": "w", "lease_until_utc": datetime.now(UTC)}
                     if state == "CLAIMED" else {})
            jobs.transition(db, job, state, **extra)
    with pytest.raises(ValueError, match="aprovação registada"), session_scope() as db:
        jobs.transition(db, job, "APPROVED")
    # Uma rejeição não serve de aprovação.
    with session_scope() as db:
        db.execute(text("INSERT INTO approvals (job_id, decision, decided_by, channel) "
                        "VALUES (:j, 'rejected', 'euclides', 'panel')"), {"j": job})
    with pytest.raises(ValueError, match="aprovação registada"), session_scope() as db:
        jobs.transition(db, job, "APPROVED")
    assert status(job) == "AWAITING_APPROVAL"


def test_retry_wait_nao_serve_para_contornar_a_aprovacao(job):
    # CLAIMED → RETRY_WAIT → APPROVED era aceite sem nunca ter havido aprovação.
    go(job, "QUEUED", "CLAIMED", "RETRY_WAIT")
    with pytest.raises(ValueError, match="aprovação registada"), session_scope() as db:
        jobs.transition(db, job, "APPROVED")
    assert status(job) == "RETRY_WAIT"


def test_worker_sem_a_lease_nao_mexe_no_job(job):
    go(job, "QUEUED")
    with session_scope() as db:
        jobs.transition(db, job, "CLAIMED", lease_owner="entrega-1",
                        lease_until_utc=datetime.now(UTC) + timedelta(minutes=20))
    with pytest.raises(jobs.LeaseLost), session_scope() as db:
        jobs.transition(db, job, "RESEARCHING", owner="entrega-2")
    assert status(job) == "CLAIMED"
    with session_scope() as db:
        jobs.transition(db, job, "RESEARCHING", owner="entrega-1")
    assert status(job) == "RESEARCHING"
