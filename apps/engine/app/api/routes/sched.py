"""Rotas de agendamento — é por aqui que o n8n comanda o motor multi-site (E4.8).

O n8n só agenda: chama estas rotas à hora certa, com o token de máquina. Toda a
lógica (o que planear, o que entregar, o que reconciliar) está no motor e é
idempotente, por isso uma chamada repetida ou atrasada não estraga nada.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.core.cache import QUEUES, queue_content
from app.core.db import session_scope
from app.core.logging import get_logger
from app.core.security import require_api_token
from app.jobs import discovery as discovery_task
from app.jobs import production
from app.services import discovery, dispatcher, newsletter, planner, publishing, watchdog
from app.services.notify import digests

log = get_logger(__name__)
def _heartbeat(request: Request) -> None:
    """E11.4: cada chamada do n8n fica registada (o vigia avisa se elas pararem)."""
    try:
        watchdog.record(request.url.path.rstrip("/").rsplit("/", 1)[-1])
    except Exception:  # noqa: BLE001 — o registo nunca pode impedir o agendamento
        log.warning("Não foi possível registar a chamada do agendador")


router = APIRouter(prefix="/api/sched", tags=["sched"],
                   dependencies=[Depends(require_api_token), Depends(_heartbeat)])

# Rotas previstas no plano cuja lógica chega em etapas posteriores.
NOT_YET: dict[str, str] = {}


class PlanDayRequest(BaseModel):
    # Data editorial a planear; sem ela, "hoje" no fuso de cada site.
    local_date: date | None = None


@router.post("/plan-day", summary="Cria os slots do dia para os sites ligados")
def plan_day(body: PlanDayRequest | None = None) -> dict:
    local_date = body.local_date if body else None
    if local_date is not None:
        # Só hoje ou amanhã: planear o passado não faz sentido (os slots já passaram)
        # e planear muito à frente fixaria horários antes de se saber a configuração.
        today = datetime.now(UTC).date()
        if not today - timedelta(days=1) <= local_date <= today + timedelta(days=2):
            raise HTTPException(422, "local_date tem de ser hoje ou amanhã")
    with session_scope() as db:
        result = planner.plan_day(db, local_date, actor="n8n")
    return {"created": result.created, "existing": result.existing,
            "skipped": result.skipped, "sites": result.sites}


@router.post("/dispatch", summary="Põe na fila o que chegou à hora e entrega aos workers")
def dispatch() -> dict:
    with session_scope() as db:
        queued = dispatcher.queue_due(db)
    claimed: list[int] = []
    while True:
        # Uma transação por entrega: o bloqueio do dispatcher dura só esse instante.
        owner = dispatcher.new_owner()
        with session_scope() as db:
            job_id = dispatcher.claim(db, owner=owner)
        if job_id is None:
            break
        try:
            queue_content.enqueue(production.produce, job_id, owner)
        except Exception:  # noqa: BLE001 — Redis em baixo: o job não pode ficar órfão
            log.exception("Falha ao enfileirar o job %d; devolvido à fila", job_id)
            with session_scope() as db:
                dispatcher.unclaim(db, job_id, owner)
            break
        claimed.append(job_id)
    return {"queued": queued, "claimed": claimed}


@router.post("/reconcile",
             summary="Leases expiradas, publicações por confirmar, jobs fora de prazo")
def reconcile() -> dict:
    with session_scope() as db:
        result = dispatcher.reconcile(db)
        requeued = dispatcher.queue_due(db)
    return {**result.as_dict(), "queued": requeued}


@router.post("/discover", summary="Lê os feeds dos sites ligados (corre no worker)")
def discover() -> dict:
    # Ler dezenas de feeds com boa educação leva minutos: vai para a fila longa.
    job = queue_content.enqueue(discovery_task.run)
    return {"enqueued": job.id}


class ManualIdea(BaseModel):
    site_key: str
    topic: str
    angle: str = ""
    url: str | None = None
    created_by: str


@router.post("/ideas", summary="Acrescenta uma ideia manual (prioritária, mesmos gates)")
def add_idea(body: ManualIdea) -> dict:
    try:
        with session_scope() as db:
            outcome, idea_id = discovery.add_manual(
                db, body.site_key, body.topic, angle=body.angle, url=body.url,
                created_by=body.created_by)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    return {"result": outcome, "idea_id": idea_id}


@router.post("/publish-due", summary="Pede a publicação do que está aprovado e na hora")
def publish_due() -> dict:
    with session_scope() as db:
        return publishing.publish_due(db)


@router.post("/approval-batch", summary="E-mail com o que espera aprovação (lotes)")
def approval_batch() -> dict:
    return digests.approval_batch()


@router.post("/digest", summary="Resumo diário por e-mail (21:00)")
def digest() -> dict:
    return digests.daily_digest()


@router.post("/newsletter", summary="Envio diário da newsletter (E17.5), no worker fast")
def newsletter_daily() -> dict:
    job = QUEUES["fast"].enqueue(newsletter.send_daily, job_timeout=1800)
    return {"queued": True, "rq_job": job.id}


@router.post("/{action}", summary="Rotas previstas, ainda sem lógica")
def not_yet(action: str) -> dict:
    if action not in NOT_YET:
        raise HTTPException(404, f"Ação desconhecida: {action}")
    raise HTTPException(501, f"{action}: ainda não implementado (chega na {NOT_YET[action]})")
