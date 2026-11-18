"""E4.5 dispatcher, E4.6 dois workers, E4.7 reconciliação, E4.8 rotas de agendamento."""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import text

from app.core import cache
from app.core.config import settings
from app.core.db import session_scope
from app.jobs import production
from app.services import dispatcher, jobs, planner, sites
from tests.conftest import advance, fetch

DAY = date(2026, 10, 5)
TOMORROW = datetime.now(UTC).date() + timedelta(days=1)
# 12:00 UTC: com 14 h de antecedência (D-066), já passou a hora de produção de todos os slots
# do dia.
NOON = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


@pytest.fixture
def planned():
    with session_scope() as db:
        sites.sync(db)
        db.execute(text("UPDATE sites SET enabled = true"))
        # Relógio fixo: o planeador não cria slots já passados (ver test_planner.BEFORE).
        planner.plan_day(db, DAY, now=datetime(2026, 10, 4, 12, 0, tzinfo=UTC))


def late() -> datetime:
    """Bem depois de tudo. O `updated_at` dos aprovados é o relógio real, por isso o 'agora' dos
    testes de prazo tem de andar com ele (uma data fixa deixa de expirar o aprovado de hoje)."""
    return datetime.now(UTC) + timedelta(days=10)


def statuses(where: str = "true") -> dict[int, str]:
    rows = fetch(f"SELECT id, status FROM publication_jobs WHERE {where} ORDER BY id")
    return {r["id"]: r["status"] for r in rows}


def claim_all(now: datetime = NOON, owner: str = "w") -> list[int]:
    claimed = []
    while True:
        with session_scope() as db:
            job_id = dispatcher.claim(db, owner, now)
        if job_id is None:
            return claimed
        claimed.append(job_id)


def site_of(job_id: int) -> str:
    return fetch("SELECT site_key FROM publication_jobs WHERE id = :i", i=job_id)[0]["site_key"]


# --- Fila (E4.5) ---------------------------------------------------------------------------


def test_so_entra_na_fila_o_que_chegou_a_hora_de_producao(planned):
    # D-066: o 1.º slot do cloud é às 07:00 de Lisboa (06:00 UTC) e produz-se 14 h antes,
    # às 16:00 UTC da véspera (13:00 em São Paulo).
    early = datetime(2026, 10, 4, 15, 59, tzinfo=UTC)
    with session_scope() as db:
        assert dispatcher.queue_due(db, early) == 0
        # Só o cloud (desfasamento 0) chegou à hora.
        assert dispatcher.queue_due(db, early + timedelta(minutes=1)) == 1
        assert dispatcher.queue_due(db, NOON) > 1
        assert dispatcher.queue_due(db, NOON) == 0  # idempotente
    queued = fetch("SELECT site_key, slot_number FROM publication_jobs WHERE status = 'QUEUED'")
    # Às 12:00 UTC já chegou a hora de produção de todos os slots do dia (o último, 18:05 UTC,
    # produz-se desde as 04:05 UTC).
    assert sorted({r["slot_number"] for r in queued}) == [1, 2, 3, 4, 5]
    assert len(queued) == 30


def test_site_desligado_nao_entra_na_fila_nem_e_entregue(planned):
    with session_scope() as db:
        db.execute(text("UPDATE sites SET enabled = false WHERE site_key = 'cloud'"))
        dispatcher.queue_due(db, NOON)
    assert "QUEUED" not in statuses("site_key = 'cloud'").values()
    assert all(site_of(j) != "cloud" for j in claim_all())


def test_um_job_ativo_por_site_e_n_no_total(planned, monkeypatch):
    monkeypatch.setattr(settings, "max_active_jobs", 4)
    with session_scope() as db:
        dispatcher.queue_due(db, NOON)
    claimed = claim_all()
    assert len(claimed) == 4                                   # N global
    assert len({site_of(j) for j in claimed}) == 4             # um por site
    assert claim_all() == []
    row = fetch("SELECT status, lease_owner, lease_until_utc, attempt_count "
                "FROM publication_jobs WHERE id = :i", i=claimed[0])[0]
    assert (row["status"], row["lease_owner"], row["attempt_count"]) == ("CLAIMED", "w", 1)
    assert row["lease_until_utc"] == NOON + timedelta(minutes=settings.job_lease_minutes)

    # Um job termina: abre vaga, e o próximo é de um site que não está ocupado.
    with session_scope() as db:
        jobs.transition(db, claimed[0], "BLOCKED", blocked_reason="teste")
    (next_job,) = claim_all()
    busy = {site_of(j) for j in claimed[1:]}
    assert site_of(next_job) not in busy


def test_o_mais_urgente_primeiro(planned, monkeypatch):
    monkeypatch.setattr(settings, "max_active_jobs", 1)
    with session_scope() as db:
        dispatcher.queue_due(db, NOON)
    (first,) = claim_all()
    row = fetch("SELECT site_key, slot_number FROM publication_jobs WHERE id = :i", i=first)[0]
    assert (row["site_key"], row["slot_number"]) == ("cloud", 1)  # 08:30, o mais cedo de todos


def test_dois_dispatchers_concorrentes_nao_pegam_o_mesmo_job(planned, monkeypatch):
    monkeypatch.setattr(settings, "max_active_jobs", 6)
    with session_scope() as db:
        dispatcher.queue_due(db, NOON)
    results: list[int] = []
    lock = threading.Lock()
    start = threading.Barrier(8)

    def worker(name: str) -> None:
        start.wait()
        for _ in range(40):
            with session_scope() as db:
                job_id = dispatcher.claim(db, name, NOON)
            if job_id is not None:
                with lock:
                    results.append(job_id)
            time.sleep(0.005)

    threads = [threading.Thread(target=worker, args=(f"d{i}",)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(results) == len(set(results)) == 6              # nenhum job entregue duas vezes
    assert len({site_of(j) for j in results}) == 6             # nem dois do mesmo site
    assert fetch("SELECT count(*) AS n FROM publication_jobs WHERE status = 'CLAIMED'")[0][
        "n"] == 6
    # Cada job tem exatamente um evento de entrega.
    events = fetch("SELECT job_id, count(*) AS n FROM publication_events "
                   "WHERE to_status = 'CLAIMED' GROUP BY job_id")
    assert all(e["n"] == 1 for e in events) and len(events) == 6


def test_por_omissao_um_job_ativo_de_cada_vez(planned):
    # Alinhado com o único worker `content`: um segundo job ficaria na fila com a lease a contar.
    assert settings.max_active_jobs == 1
    with session_scope() as db:
        dispatcher.queue_due(db, NOON)
    assert len(claim_all()) == 1


def test_renovar_a_lease(planned):
    with session_scope() as db:
        dispatcher.queue_due(db, NOON)
    job = claim_all()[0]
    later = NOON + timedelta(minutes=15)
    with session_scope() as db:
        assert dispatcher.renew_lease(db, job, "w", later) is True
        assert dispatcher.renew_lease(db, job, "outro", later) is False   # não é dele
    assert fetch("SELECT lease_until_utc FROM publication_jobs WHERE id = :i", i=job)[0][
        "lease_until_utc"] == later + timedelta(minutes=settings.job_lease_minutes)
    with session_scope() as db:
        jobs.transition(db, job, "BLOCKED")
        assert dispatcher.renew_lease(db, job, "w", later) is False       # já não está ativo


# --- Reconciliação (E4.7) --------------------------------------------------------------------


def test_worker_morto_a_meio_a_lease_expira_e_o_job_volta(planned, monkeypatch):
    monkeypatch.setattr(settings, "max_active_jobs", 1)   # um só job em produção
    with session_scope() as db:
        dispatcher.queue_due(db, NOON)
    job = claim_all()[0]
    advance(job, "RESEARCHING", "WRITING")           # o worker morre aqui

    with session_scope() as db:                      # ainda dentro da lease: nada acontece
        assert dispatcher.reconcile(db, NOON + timedelta(minutes=5)).requeued == []
    after = NOON + timedelta(minutes=settings.job_lease_minutes + 1)
    with session_scope() as db:
        result = dispatcher.reconcile(db, after)
    assert result.requeued == [job]
    row = fetch("SELECT status, lease_owner, retry_after_utc FROM publication_jobs "
                "WHERE id = :i", i=job)[0]
    assert (row["status"], row["lease_owner"]) == ("RETRY_WAIT", None)
    assert row["retry_after_utc"] == after + timedelta(minutes=settings.job_retry_minutes)

    with session_scope() as db:                      # ainda em espera
        dispatcher.queue_due(db, after)
    assert statuses(f"id = {job}")[job] == "RETRY_WAIT"
    back = after + timedelta(minutes=settings.job_retry_minutes)
    with session_scope() as db:
        dispatcher.queue_due(db, back)
    assert statuses(f"id = {job}")[job] == "QUEUED"
    with session_scope() as db:
        assert dispatcher.claim(db, "w2", back) == job
    assert fetch("SELECT attempt_count, lease_owner FROM publication_jobs WHERE id = :i",
                 i=job)[0] == {"attempt_count": 2, "lease_owner": "w2"}
    event = fetch("SELECT details FROM publication_events WHERE job_id = :j "
                  "AND to_status = 'RETRY_WAIT'", j=job)[0]["details"]
    assert event == {"reason": "lease_expired", "lease_owner": "w", "was": "WRITING"}


def test_esgotadas_as_tentativas_o_job_falha(planned, monkeypatch):
    monkeypatch.setattr(settings, "job_max_attempts", 2)
    with session_scope() as db:
        dispatcher.queue_due(db, NOON)
    job = claim_all()[0]
    moment = NOON
    for expected in ("RETRY_WAIT", "FAILED"):
        moment += timedelta(minutes=settings.job_lease_minutes + 1)
        with session_scope() as db:
            dispatcher.reconcile(db, moment)
        assert statuses(f"id = {job}")[job] == expected
        if expected == "RETRY_WAIT":
            moment += timedelta(hours=1)
            with session_scope() as db:
                dispatcher.queue_due(db, moment)
                # Só este job interessa: os outros sites ficam por entregar.
                db.execute(text("UPDATE publication_jobs SET status = 'PLANNED' "
                                "WHERE status = 'QUEUED' AND id <> :i"), {"i": job})
            assert claim_all(moment) == [job]
    assert "lease expirada 2 vezes" in fetch(
        "SELECT blocked_reason FROM publication_jobs WHERE id = :i", i=job)[0]["blocked_reason"]


def test_publicacao_interrompida_fica_por_confirmar(planned):
    job = next(iter(statuses()))
    advance(job, "QUEUED", "CLAIMED", "RESEARCHING", "WRITING", "VALIDATING",
            "TRANSLATING", "AWAITING_APPROVAL", "APPROVED")
    with session_scope() as db:
        jobs.transition(db, job, "PUBLISHING", lease_owner="fast",
                        lease_until_utc=NOON + timedelta(minutes=5))
    with session_scope() as db:
        result = dispatcher.reconcile(db, NOON + timedelta(minutes=6))
    # Não se sabe se o WordPress recebeu o post: nunca se repete às cegas.
    assert result.verify == [job] and result.verify_pending == 1
    assert statuses(f"id = {job}")[job] == "VERIFY_REQUIRED"


def test_slots_fora_de_prazo_expiram(planned):
    ids = list(statuses())
    waiting, approved, published = ids[0], ids[1], ids[2]
    until = ("QUEUED", "CLAIMED", "RESEARCHING", "WRITING", "VALIDATING", "TRANSLATING",
             "AWAITING_APPROVAL")
    advance(waiting, *until)
    advance(approved, *until, "APPROVED")
    advance(published, *until, "APPROVED", "PUBLISHING", "PUBLISHED_CONFIRMED")
    with session_scope() as db:                       # no próprio dia nada expira
        assert dispatcher.reconcile(db, datetime(2026, 10, 5, 23, 0, tzinfo=UTC)).expired == []
    with session_scope() as db:
        result = dispatcher.reconcile(db, late())
    assert len(result.expired) == 29                  # tudo menos o publicado
    final = statuses()
    assert final[published] == "PUBLISHED_CONFIRMED"
    assert final[waiting] == final[approved] == "EXPIRED"


def test_blog_desligado_nao_expira_o_que_la_ficou_aprovado(planned):
    """As amostras aprovadas num blog desligado (à espera da E14) não expiram por o tempo passar."""
    ids = list(statuses())
    advance(ids[0], "QUEUED", "CLAIMED", "RESEARCHING", "WRITING", "VALIDATING", "TRANSLATING",
            "AWAITING_APPROVAL", "APPROVED")
    site = site_of(ids[0])
    with session_scope() as db:
        db.execute(text("UPDATE sites SET enabled = false WHERE site_key = :k"), {"k": site})
    with session_scope() as db:
        result = dispatcher.reconcile(db, late())
    assert ids[0] not in result.expired
    assert statuses()[ids[0]] == "APPROVED"
    # os dos outros blogs, ligados, continuam a expirar
    assert len(result.expired) == len(ids) - len([i for i in ids if site_of(i) == site])


def test_reconciliacao_devolve_pautas_orfas_e_expira_as_vencidas(planned):
    """M5 (8/11): `taken` sem job do site a produzir é órfã — volta à fila ou expira."""
    def idea(site: str, topic: str, expires: datetime | None) -> int:
        with session_scope() as db:
            return db.execute(
                text("""INSERT INTO ideas (site_key, origin, topic, status, expires_at)
                        VALUES (:s, 'rss', :t, 'taken', :e) RETURNING id"""),
                {"s": site, "t": topic, "e": expires}).scalar_one()

    fresh = idea("cloud", "órfã dentro da validade", NOON + timedelta(days=2))
    stale = idea("cloud", "órfã vencida", NOON - timedelta(days=1))
    held = idea("ia", "em uso por um worker agora", None)
    ia_job = fetch("SELECT id FROM publication_jobs WHERE site_key = 'ia'")[0]["id"]
    advance(ia_job, "QUEUED", "CLAIMED")      # o blog `ia` tem um worker a produzir

    with session_scope() as db:
        result = dispatcher.reconcile(db, NOON)
    assert (result.ideas_released, result.ideas_expired) == (1, 1)
    rows = {r["id"]: r["status"] for r in fetch("SELECT id, status FROM ideas")}
    assert rows == {fresh: "new", stale: "expired", held: "taken"}

    # Segunda passagem: nada muda enquanto o worker do `ia` segura a sua pauta.
    with session_scope() as db:
        again = dispatcher.reconcile(db, NOON)
    assert (again.ideas_released, again.ideas_expired) == (0, 0)

    # O job acaba bloqueado sem devolver a pauta (caminho antigo): a vassoura apanha-a.
    advance(ia_job, "BLOCKED")
    with session_scope() as db:
        final = dispatcher.reconcile(db, NOON)
    assert final.ideas_released == 1
    assert fetch("SELECT status FROM ideas WHERE id = :i", i=held)[0]["status"] == "new"


def test_aprovado_que_falhou_a_publicar_volta_a_aprovado_e_nao_e_reproduzido(planned):
    job = next(iter(statuses()))
    advance(job, "QUEUED", "CLAIMED", "RESEARCHING", "WRITING", "VALIDATING",
            "TRANSLATING", "AWAITING_APPROVAL", "APPROVED", "PUBLISHING")
    with session_scope() as db:
        jobs.transition(db, job, "RETRY_WAIT", retry_after_utc=NOON)
        dispatcher.queue_due(db, NOON + timedelta(minutes=1))
    assert statuses(f"id = {job}")[job] == "APPROVED"


# --- Tarefa de produção (até à E6) ---------------------------------------------------------


def test_producao_sem_ideias_bloqueia_com_motivo_claro(planned):
    with session_scope() as db:
        dispatcher.queue_due(db, NOON)
    job = claim_all()[0]
    assert production.produce(job, "w") == "BLOCKED"
    row = fetch("SELECT status, blocked_reason, lease_owner FROM publication_jobs "
                "WHERE id = :i", i=job)[0]
    assert (row["status"], row["lease_owner"]) == ("BLOCKED", None)
    assert row["blocked_reason"] == "sem ideias na fila"


# --- Dois workers (E4.6) ---------------------------------------------------------------------


def test_job_longo_no_content_nao_atrasa_job_do_fast():
    long_job = cache.queue_content.enqueue(time.sleep, 4)
    fast_job = cache.queue_fast.enqueue(time.time)
    env = {**os.environ, "COVERAGE_PROCESS_START": ""}

    def worker(queues: str) -> subprocess.Popen:
        return subprocess.Popen([sys.executable, "-m", "app.jobs.worker", "--burst"],
                                env={**env, "WORKER_QUEUES": queues}, cwd="/app",
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    content = worker("content,low,default")
    time.sleep(1.0)                      # o worker content já está ocupado com o job longo
    fast = worker("fast,high")
    assert fast.wait(timeout=30) == 0
    fast_job.refresh()
    long_job.refresh()
    # O job rápido acabou enquanto o longo ainda corria.
    assert fast_job.get_status() == "finished"
    assert long_job.get_status() in ("started", "queued")
    assert content.wait(timeout=30) == 0
    long_job.refresh()
    assert long_job.get_status() == "finished"
    assert fast_job.ended_at < long_job.ended_at


def test_filas_separadas_e_tempos_limite():
    assert cache.QUEUES["content"].name == "content" and cache.QUEUES["fast"].name == "fast"
    assert cache.queue_fast._default_timeout < cache.queue_content._default_timeout


def test_lock_curto_com_renovacao():
    first, second = cache.ShortLock("fase-x", ttl=2), cache.ShortLock("fase-x", ttl=2)
    assert first.acquire() is True
    assert second.acquire() is False            # já tem dono
    assert second.renew() is False              # ninguém renova o lock de outro
    assert second.release() is False            # nem o liberta
    time.sleep(1.2)
    assert first.renew() is True                # o dono renova antes do TTL…
    time.sleep(1.2)
    assert second.acquire() is False            # …e o lock continua dele
    assert first.release() is True
    assert second.acquire() is True

    # Processo morto: sem renovação, o lock cai sozinho ao fim do TTL curto.
    dead = cache.ShortLock("fase-y", ttl=1)
    assert dead.acquire() is True
    time.sleep(1.3)
    assert cache.ShortLock("fase-y", ttl=1).acquire() is True
    assert dead.renew() is False and dead.release() is False


# --- Rotas para o n8n (E4.8) -----------------------------------------------------------------


def test_rotas_exigem_token(client):
    for action in ("plan-day", "dispatch", "reconcile", "discover", "publish-due", "digest"):
        assert client.post(f"/api/sched/{action}").status_code == 401, action


def test_plan_day_pela_rota_e_idempotente(client, auth):
    with session_scope() as db:
        sites.sync(db)
        db.execute(text("UPDATE sites SET enabled = true"))
    body = {"local_date": TOMORROW.isoformat()}
    assert client.post("/api/sched/plan-day", json=body, headers=auth).json() == {
        "created": 30, "existing": 0, "skipped": 0, "sites": 6}
    assert client.post("/api/sched/plan-day", json=body, headers=auth).json() == {
        "created": 0, "existing": 30, "skipped": 0, "sites": 6}
    # Sem data: "hoje" no fuso do site; os slots que já passaram são saltados (D-034).
    today = client.post("/api/sched/plan-day", headers=auth).json()
    assert today["created"] + today["skipped"] == 30
    actors = {r["actor"] for r in fetch("SELECT actor FROM publication_events")}
    assert actors == {"n8n"}


def test_plan_day_com_todos_os_sites_desligados_nao_cria_nada(client, auth):
    with session_scope() as db:
        sites.sync(db)                                  # como em produção
    assert client.post("/api/sched/plan-day", headers=auth).json() == {
        "created": 0, "existing": 0, "skipped": 0, "sites": 0}


def test_dispatch_pela_rota_entrega_e_enfileira(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "max_active_jobs", 2)
    monkeypatch.setattr(settings, "production_lead_hours", 48)  # tudo já em hora de produção
    with session_scope() as db:
        sites.sync(db)
        db.execute(text("UPDATE sites SET enabled = true"))
    client.post("/api/sched/plan-day", json={"local_date": TOMORROW.isoformat()}, headers=auth)
    result = client.post("/api/sched/dispatch", headers=auth).json()
    assert result["queued"] == 30 and len(result["claimed"]) == 2
    assert cache.queue_content.count == 2
    assert [job.args[0] for job in cache.queue_content.jobs] == result["claimed"]
    assert cache.queue_content.jobs[0].func_name == "app.jobs.production.produce"
    # Cada entrega tem um dono único, que é o dono da lease e vai para o worker.
    owners = [job.args[1] for job in cache.queue_content.jobs]
    assert len(set(owners)) == 2 and all(o.startswith("delivery:") for o in owners)
    assert {r["lease_owner"] for r in fetch(
        "SELECT lease_owner FROM publication_jobs WHERE status = 'CLAIMED'")} == set(owners)
    # Segunda chamada: nada de novo (os dois sites continuam ocupados e N está cheio).
    assert client.post("/api/sched/dispatch", headers=auth).json() == {"queued": 0, "claimed": []}


def test_reconcile_pela_rota(client, auth, planned):
    with session_scope() as db:
        dispatcher.queue_due(db, NOON)
    job = claim_all()[0]
    with session_scope() as db:
        db.execute(text("UPDATE publication_jobs SET lease_until_utc = now() - interval '1 min' "
                        "WHERE id = :i"), {"i": job})
    body = client.post("/api/sched/reconcile", headers=auth).json()
    assert body["requeued"] == [job]
    assert set(body) == {"requeued", "failed", "verify", "expired", "verify_pending", "queued",
                         "ideas_released", "ideas_expired"}


def test_rotas_ainda_sem_logica_dizem_em_que_etapa_chegam(client, auth):
    # Todas as rotas previstas já têm lógica (publish-due: E10.4; digest e approval-batch: E10.5).
    assert client.post("/api/sched/apagar-tudo", headers=auth).status_code == 404


# --- Revisão E4: correções ------------------------------------------------------------------


def test_job_ativo_sem_lease_e_apanhado_pela_reconciliacao(planned):
    # Defesa em profundidade (#1): mesmo que uma lease nula chegue à base, o job não fica preso.
    with session_scope() as db:
        dispatcher.queue_due(db, NOON)
    job = claim_all()[0]
    with session_scope() as db:
        db.execute(text("UPDATE publication_jobs SET lease_until_utc = NULL WHERE id = :i"),
                   {"i": job})
        assert dispatcher.reconcile(db, NOON).requeued == [job]


def test_worker_que_pega_tarde_num_job_reentregue_larga_o(planned):
    with session_scope() as db:
        dispatcher.queue_due(db, NOON)
        first = dispatcher.new_owner()
        job = dispatcher.claim(db, first, NOON)
    # A lease expira com o job ainda na fila do RQ; a reconciliação devolve-o e é reentregue.
    later = NOON + timedelta(minutes=settings.job_lease_minutes + 1)
    with session_scope() as db:
        dispatcher.reconcile(db, later)
        dispatcher.queue_due(db, later + timedelta(hours=1))
        second = dispatcher.new_owner()
        assert dispatcher.claim(db, second, later + timedelta(hours=1)) == job
    # A mensagem velha chega finalmente ao worker: não toca no job.
    assert production.produce(job, first) == "STALE"
    assert statuses(f"id = {job}")[job] == "CLAIMED"
    assert production.produce(job, second) == "BLOCKED"


def test_a_lease_comeca_a_contar_quando_o_worker_pega_no_job(planned):
    with session_scope() as db:
        dispatcher.queue_due(db, NOON)
        owner = dispatcher.new_owner()
        job = dispatcher.claim(db, owner, NOON)
        picked = NOON + timedelta(minutes=15)       # 15 min na fila do RQ
        assert dispatcher.start_work(db, job, owner, picked) is True
    assert fetch("SELECT lease_until_utc FROM publication_jobs WHERE id = :i", i=job)[0][
        "lease_until_utc"] == picked + timedelta(minutes=settings.job_lease_minutes)
    assert dispatcher.new_owner() != dispatcher.new_owner()


def test_falha_a_enfileirar_devolve_o_job_sem_gastar_a_tentativa(client, auth, monkeypatch):
    monkeypatch.setattr(settings, "production_lead_hours", 48)
    with session_scope() as db:
        sites.sync(db)
        db.execute(text("UPDATE sites SET enabled = true"))
    client.post("/api/sched/plan-day", json={"local_date": TOMORROW.isoformat()}, headers=auth)

    def boom(*args, **kwargs):
        raise ConnectionError("redis em baixo")

    monkeypatch.setattr(cache.queue_content, "enqueue", boom)
    result = client.post("/api/sched/dispatch", headers=auth).json()
    assert result["claimed"] == []
    rows = fetch("SELECT status, attempt_count, lease_owner FROM publication_jobs")
    assert {r["status"] for r in rows} == {"QUEUED"}
    assert all(r["attempt_count"] == 0 and r["lease_owner"] is None for r in rows)


def test_slots_demasiado_antigos_nao_entram_na_fila_nem_sao_entregues(planned):
    stale = NOON + timedelta(hours=settings.job_expire_hours + 12)
    with session_scope() as db:
        assert dispatcher.queue_due(db, stale) == 0     # dispatch antes do reconcile
    with session_scope() as db:
        db.execute(text("UPDATE publication_jobs SET status = 'QUEUED'"))
    assert claim_all(stale) == []
    with session_scope() as db:
        assert len(dispatcher.reconcile(db, stale).expired) == 30


def test_plan_day_so_aceita_hoje_ou_amanha(client, auth):
    for bad in ("2020-01-01", (TOMORROW + timedelta(days=5)).isoformat()):
        res = client.post("/api/sched/plan-day", json={"local_date": bad}, headers=auth)
        assert res.status_code == 422, bad


def _reason(job: int) -> str | None:
    return fetch("SELECT blocked_reason FROM publication_jobs WHERE id = :i", i=job)[0][
        "blocked_reason"]


def test_motivo_antigo_some_quando_o_job_chega_a_aprovacao_e_a_publicacao(planned):
    """O motivo continua visível durante a espera; ao passar, apaga-se (fica nos eventos)."""
    job = next(iter(statuses()))
    advance(job, "QUEUED", "CLAIMED", "RESEARCHING")
    with session_scope() as db:
        jobs.transition(db, job, "RETRY_WAIT", blocked_reason="fontes insuficientes: 1 de 2")
        jobs.transition(db, job, "QUEUED")
    assert _reason(job) == "fontes insuficientes: 1 de 2"
    advance(job, "CLAIMED", "RESEARCHING", "WRITING", "VALIDATING", "TRANSLATING",
            "AWAITING_APPROVAL")
    assert _reason(job) is None
    advance(job, "APPROVED", "PUBLISHING")
    with session_scope() as db:
        jobs.transition(db, job, "RETRY_WAIT", blocked_reason="GET /wp/v2/media: HTTP 400")
        jobs.transition(db, job, "APPROVED")
    assert _reason(job) == "GET /wp/v2/media: HTTP 400"
    advance(job, "PUBLISHING", "PUBLISHED_CONFIRMED")
    assert _reason(job) is None
    assert fetch("SELECT count(*) AS n FROM publication_events WHERE job_id = :i "
                 "AND to_status = 'RETRY_WAIT'", i=job)[0]["n"] == 2
