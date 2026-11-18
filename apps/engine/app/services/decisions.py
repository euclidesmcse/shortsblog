"""A decisão humana sobre um artigo (D-006): aprovar ou rejeitar, em `approvals` e no estado do job.

Partilhada pelo painel (canal `panel`) e pelo link do e-mail (canal `email`). O estado muda por
`jobs.transition`, que exige a linha em `approvals` para entrar em APPROVED.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services import jobs

STALE = "Este job já não está à espera de aprovação (outra pessoa decidiu ou expirou)."


def decide(
    db: Session, job_id: int, decision: str, *, user: str, channel: str, note: str = ""
) -> str:
    """Grava a decisão e muda o estado. Devolve '' se correu bem, ou o erro (sem gravar nada)."""
    if decision not in ("approved", "rejected") or channel not in ("panel", "email"):
        raise ValueError("decisão ou canal desconhecido")
    try:
        db.execute(
            text(
                "INSERT INTO approvals (job_id, decision, decided_by, channel, note) "
                "VALUES (:j, :d, :u, :c, :n)"
            ),
            {"j": job_id, "d": decision, "u": user, "c": channel, "n": note},
        )
        extra = (
            {} if decision == "approved" else {"blocked_reason": note or f"rejeitado ({channel})"}
        )
        jobs.transition(
            db,
            job_id,
            "APPROVED" if decision == "approved" else "REJECTED",
            actor=user,
            expected="AWAITING_APPROVAL",
            details={"channel": channel, "note": note},
            **extra,
        )
    except (jobs.InvalidTransition, jobs.JobNotFound):
        db.rollback()
        return STALE
    return ""
