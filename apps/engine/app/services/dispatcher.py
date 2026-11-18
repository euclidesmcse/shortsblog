"""Dispatcher e reconciliação dos jobs de publicação (E4.5, E4.7).

Dispatcher: passa para a fila os jobs cuja hora de produção chegou e entrega-os
aos workers, um de cada vez por site e no máximo `max_active_jobs` no total.
A entrega (*claim*) é atómica: `SELECT … FOR UPDATE SKIP LOCKED` dentro de um
bloqueio consultivo do Postgres, de modo que dois dispatchers em simultâneo
nunca entregam o mesmo job nem dois jobs do mesmo site.

Cada job entregue leva uma *lease* com prazo. O worker renova-a enquanto
trabalha; se morrer, a lease expira e a reconciliação devolve o job à fila
(ou dá-o como falhado ao fim de `job_max_attempts` tentativas).
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.services import jobs

log = get_logger(__name__)

# Chave do bloqueio consultivo que serializa os dispatchers (valor arbitrário, fixo).
_DISPATCH_LOCK = 4_711_001


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


# --- Fila ---------------------------------------------------------------------------------


def queue_due(db: Session, now: datetime | None = None) -> int:
    """PLANNED cuja hora de produção chegou, e RETRY_WAIT cuja espera acabou, vão para a fila.

    Um job que já tinha sido aprovado e falhou a publicar volta a APPROVED (não é
    produzido outra vez).
    """
    moment = _now(now)
    moved = 0
    planned = db.execute(
        text("""SELECT j.id FROM publication_jobs j JOIN sites s ON s.site_key = j.site_key
                 WHERE j.status = 'PLANNED' AND j.produce_after_utc <= :now AND s.enabled
                   AND j.due_at_utc >= :oldest
                 ORDER BY j.due_at_utc FOR UPDATE OF j SKIP LOCKED"""),
        {"now": moment, "oldest": _too_old(moment)},
    ).scalars().all()
    for job_id in planned:
        jobs.transition(db, job_id, "QUEUED", actor="dispatcher", expected="PLANNED")
        moved += 1

    waiting = db.execute(
        text("""SELECT j.id,
                       EXISTS (SELECT 1 FROM approvals a
                                WHERE a.job_id = j.id AND a.decision = 'approved') AS approved
                  FROM publication_jobs j
                 WHERE j.status = 'RETRY_WAIT'
                   AND (j.retry_after_utc IS NULL OR j.retry_after_utc <= :now)
                 ORDER BY j.due_at_utc FOR UPDATE OF j SKIP LOCKED"""),
        {"now": moment},
    ).all()
    for job_id, approved in waiting:
        jobs.transition(db, job_id, "APPROVED" if approved else "QUEUED", actor="dispatcher",
                        expected="RETRY_WAIT", retry_after_utc=None)
        moved += 1
    return moved


def new_owner() -> str:
    """Identificador único de uma entrega: é o dono da lease desse job."""
    return f"delivery:{uuid.uuid4().hex[:16]}"


def _too_old(moment: datetime) -> datetime:
    """Slots anteriores a isto já não se produzem: a reconciliação expira-os."""
    return moment - timedelta(hours=settings.job_expire_hours)


def claim(db: Session, owner: str, now: datetime | None = None) -> int | None:
    """Entrega o próximo job da fila a `owner`. Devolve o id, ou None se não há nada a entregar.

    Regras: 1 job ativo por site; `max_active_jobs` no total; o mais urgente primeiro.
    Tem de correr numa transação própria (o bloqueio dura até ao commit).
    """
    moment = _now(now)
    if not db.execute(text("SELECT pg_try_advisory_xact_lock(:k)"),
                      {"k": _DISPATCH_LOCK}).scalar():
        return None  # outro dispatcher está a entregar; na próxima volta entra este
    active = list(jobs.ACTIVE)
    busy = db.execute(
        text("SELECT count(*) FROM publication_jobs WHERE status = ANY(:active)"),
        {"active": active},
    ).scalar_one()
    if busy >= settings.max_active_jobs:
        return None
    job_id = db.execute(
        text("""SELECT j.id FROM publication_jobs j JOIN sites s ON s.site_key = j.site_key
                 WHERE j.status = 'QUEUED' AND s.enabled AND j.due_at_utc >= :oldest
                   AND NOT EXISTS (SELECT 1 FROM publication_jobs o
                                    WHERE o.site_key = j.site_key AND o.status = ANY(:active))
                 ORDER BY j.due_at_utc, j.id
                 LIMIT 1 FOR UPDATE OF j SKIP LOCKED"""),
        {"active": active, "oldest": _too_old(moment)},
    ).scalar()
    if job_id is None:
        return None
    attempts = db.execute(text("SELECT attempt_count FROM publication_jobs WHERE id = :i"),
                          {"i": job_id}).scalar_one()
    jobs.transition(
        db, job_id, "CLAIMED", actor=owner, expected="QUEUED",
        lease_owner=owner, attempt_count=attempts + 1,
        lease_until_utc=moment + timedelta(minutes=settings.job_lease_minutes),
    )
    return int(job_id)


def unclaim(db: Session, job_id: int, owner: str) -> None:
    """A entrega não chegou ao worker (falhou o enfileiramento): o job volta à fila
    e a tentativa não conta."""
    attempts = db.execute(text("SELECT attempt_count FROM publication_jobs WHERE id = :i"),
                          {"i": job_id}).scalar_one()
    jobs.transition(db, job_id, "QUEUED", actor="dispatcher", expected="CLAIMED", owner=owner,
                    attempt_count=max(attempts - 1, 0),
                    details={"reason": "enqueue_failed"})


def start_work(db: Session, job_id: int, owner: str, now: datetime | None = None) -> bool:
    """O worker pegou no job: a lease começa a contar agora, não quando foi entregue.

    False se o job já não é desta entrega (expirou na fila e foi reentregue): o
    worker não lhe pode tocar.
    """
    return renew_lease(db, job_id, owner, now)


def renew_lease(db: Session, job_id: int, owner: str, now: datetime | None = None) -> bool:
    """O worker continua vivo: estende a lease. False se o job já não é dele."""
    updated = db.execute(
        text("""UPDATE publication_jobs
                   SET lease_until_utc = :until, updated_at = now()
                 WHERE id = :i AND lease_owner = :o AND status = ANY(:active)
                RETURNING id"""),
        {"until": _now(now) + timedelta(minutes=settings.job_lease_minutes), "i": job_id,
         "o": owner, "active": list(jobs.ACTIVE)},
    ).first()
    return updated is not None


# --- Reconciliação (E4.7) ------------------------------------------------------------------


@dataclass
class ReconcileResult:
    requeued: list[int] = field(default_factory=list)       # lease expirada → nova tentativa
    failed: list[int] = field(default_factory=list)         # esgotou as tentativas
    verify: list[int] = field(default_factory=list)         # publicação por confirmar
    expired: list[int] = field(default_factory=list)        # passou o prazo sem publicar
    verify_pending: int = 0                                 # à espera de confirmação (E9)
    ideas_released: int = 0                                 # pautas órfãs devolvidas à fila (M5)
    ideas_expired: int = 0                                  # pautas órfãs já fora da validade

    def as_dict(self) -> dict[str, object]:
        return {"requeued": self.requeued, "failed": self.failed, "verify": self.verify,
                "expired": self.expired, "verify_pending": self.verify_pending,
                "ideas_released": self.ideas_released, "ideas_expired": self.ideas_expired}


def reconcile(db: Session, now: datetime | None = None) -> ReconcileResult:
    """Arruma o que ficou a meio.

    Leases expiradas, publicações por confirmar e jobs fora de prazo.
    """
    moment = _now(now)
    result = ReconcileResult()

    stale = db.execute(
        text("""SELECT id, status, attempt_count, lease_owner FROM publication_jobs
                 WHERE status = ANY(:active)
                   AND (lease_until_utc < :now OR lease_until_utc IS NULL)
                 ORDER BY id FOR UPDATE SKIP LOCKED"""),
        {"active": list(jobs.ACTIVE), "now": moment},
    ).all()
    for job_id, status, attempts, owner in stale:
        details = {"reason": "lease_expired", "lease_owner": owner, "was": status}
        if status == "PUBLISHING":
            # O worker morreu a publicar: não se sabe se o WordPress recebeu o post.
            jobs.transition(db, job_id, "VERIFY_REQUIRED", actor="reconciler", details=details)
            result.verify.append(job_id)
        elif attempts >= settings.job_max_attempts:
            jobs.transition(db, job_id, "FAILED", actor="reconciler", details=details,
                            blocked_reason=f"lease expirada {attempts} vezes")
            result.failed.append(job_id)
        else:
            backoff = timedelta(minutes=settings.job_retry_minutes * attempts)
            jobs.transition(db, job_id, "RETRY_WAIT", actor="reconciler", details=details,
                            retry_after_utc=moment + backoff)
            result.requeued.append(job_id)

    # Fora de prazo. Só nos blogs ligados: um blog desligado (amostras, testes) não é planeado nem
    # entregue, e o que lá ficou aprovado à espera da E14 não pode expirar por o relógio andar.
    # - por produzir (PLANNED, QUEUED, BLOCKED): o slot passou há mais de `job_expire_hours`;
    # - à espera de aprovação: 72 h depois do slot (`approval_expire_hours`, E10.4);
    # - aprovado ou a repetir (APPROVED, RETRY_WAIT): `job_expire_hours` sem progresso, contado
    #   desde a última mudança e não desde o slot: aprovar às 60 h não pode expirar antes de
    #   publicar.
    overdue = db.execute(
        text("""SELECT j.id FROM publication_jobs j JOIN sites s ON s.site_key = j.site_key
                 WHERE s.enabled AND (
                       (j.status = ANY(:slot_bound) AND j.due_at_utc < :limit)
                    OR (j.status = 'AWAITING_APPROVAL' AND j.due_at_utc < :approval_limit)
                    OR (j.status = ANY(:no_progress) AND j.updated_at < :limit))
                 ORDER BY j.id FOR UPDATE OF j SKIP LOCKED"""),
        {"slot_bound": ["PLANNED", "QUEUED", "BLOCKED"], "no_progress": ["APPROVED", "RETRY_WAIT"],
         "limit": moment - timedelta(hours=settings.job_expire_hours),
         "approval_limit": moment - timedelta(hours=settings.approval_expire_hours)},
    ).scalars().all()
    for job_id in overdue:
        jobs.transition(db, job_id, "EXPIRED", actor="reconciler",
                        details={"reason": "past_due"})
        result.expired.append(job_id)

    # Pautas órfãs (M5, 8/11): uma ideia fica `taken` da escolha até virar `used` (na espera de
    # aprovação) ou ser descartada/devolvida — mas um job que acabava BLOCKED ou expirava levava
    # a pauta consigo, presa para sempre. Sem nenhum job do site a produzir, não há mão nenhuma
    # segurando as `taken` desse site: dentro da validade voltam à fila; fora dela expiram.
    # A guarda por site existe porque o job só grava `idea_id` ao entrar em WRITING: entre a
    # escolha e esse passo a pauta está em uso sem nada na base a apontar para ela.
    swept = db.execute(
        text("""UPDATE ideas i
                   SET status = CASE WHEN i.expires_at IS NULL OR i.expires_at >= :now
                                     THEN 'new' ELSE 'expired' END
                 WHERE i.status = 'taken'
                   AND NOT EXISTS (SELECT 1 FROM publication_jobs j
                                    WHERE j.site_key = i.site_key
                                      AND j.status = ANY(:producing))
                RETURNING (i.expires_at IS NULL OR i.expires_at >= :now)"""),
        {"now": moment, "producing": list(jobs.PRODUCING)},
    ).scalars().all()
    result.ideas_released = sum(1 for fresh in swept if fresh)
    result.ideas_expired = len(swept) - result.ideas_released

    result.verify_pending = db.execute(
        text("SELECT count(*) FROM publication_jobs WHERE status = 'VERIFY_REQUIRED'")
    ).scalar_one()
    if result.requeued or result.failed or result.verify or result.expired or swept:
        log.warning("Reconciliação: %s", result.as_dict())
    return result
