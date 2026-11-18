"""Máquina de estados dos jobs de publicação (E4.4).

Um job é um slot de um site num dia. Só muda de estado por `transition`, que
valida a passagem, bloqueia a linha e regista o evento em `publication_events`.
Nenhum outro código faz `UPDATE … SET status`.

`READY_TO_PUBLISH` da especificação não existe: foi substituído por
`AWAITING_APPROVAL` → `APPROVED` (D-006: aprovação humana sempre). Não há
caminho para `PUBLISHING` que não passe por `APPROVED`.
"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

STATES = (
    "PLANNED", "QUEUED", "CLAIMED", "RESEARCHING", "WRITING", "VALIDATING", "TRANSLATING",
    "AWAITING_APPROVAL", "APPROVED", "PUBLISHING", "VERIFY_REQUIRED", "RETRY_WAIT",
    "PUBLISHED_CONFIRMED", "BLOCKED", "FAILED", "REJECTED", "EXPIRED",
)

# Estados em que um worker está a produzir o artigo (têm lease).
PRODUCING = ("CLAIMED", "RESEARCHING", "WRITING", "VALIDATING", "TRANSLATING")
# Ocupam o site: contam para "1 job ativo por site" (E4.5).
ACTIVE = (*PRODUCING, "PUBLISHING")
# Fim de linha.
TERMINAL = ("PUBLISHED_CONFIRMED", "FAILED", "REJECTED", "EXPIRED")

_PRODUCTION_EXITS = ("RETRY_WAIT", "BLOCKED", "FAILED")
# Chegar aqui prova que o problema anterior passou: o `blocked_reason` antigo é apagado.
_CLEARS_REASON = ("AWAITING_APPROVAL", "PUBLISHED_CONFIRMED")

TRANSITIONS: dict[str, tuple[str, ...]] = {
    "PLANNED": ("QUEUED", "EXPIRED"),
    "QUEUED": ("CLAIMED", "EXPIRED"),
    "CLAIMED": ("RESEARCHING", "QUEUED", *_PRODUCTION_EXITS),
    # O laço sobre si próprio é a pauta de resgate (D-101): a fila esgotou-se sem pauta
    # viável e o job recomeça a pesquisa com os temas intemporais que o resgate guardou.
    "RESEARCHING": ("WRITING", "RESEARCHING", *_PRODUCTION_EXITS),
    # Volta à pesquisa quando o redator recusa o tema: outra pauta, mesmo job (D-085).
    "WRITING": ("VALIDATING", "RESEARCHING", *_PRODUCTION_EXITS),
    # Volta à pesquisa quando a revisão esgota as reescritas: outra pauta, mesmo job (D-093).
    "VALIDATING": ("TRANSLATING", "WRITING", "RESEARCHING", *_PRODUCTION_EXITS),
    # Volta à pesquisa quando um gate de tradução não tem saída: outra pauta, mesmo job (D-100).
    "TRANSLATING": ("AWAITING_APPROVAL", "RESEARCHING", *_PRODUCTION_EXITS),
    # Só uma decisão humana tira um job daqui (ou o prazo, que o expira).
    "AWAITING_APPROVAL": ("APPROVED", "REJECTED", "EXPIRED"),
    "APPROVED": ("PUBLISHING", "EXPIRED"),
    "PUBLISHING": ("PUBLISHED_CONFIRMED", "VERIFY_REQUIRED", "RETRY_WAIT", "FAILED"),
    # Não se sabe se o WordPress publicou: a reconciliação confirma ou repete.
    "VERIFY_REQUIRED": ("PUBLISHED_CONFIRMED", "PUBLISHING", "FAILED"),
    # Volta à fila para nova tentativa; aprovado continua aprovado.
    "RETRY_WAIT": ("QUEUED", "APPROVED", "FAILED", "EXPIRED"),
    # Bloqueado por um gate (sem fontes, duplicado…): só à mão volta à fila.
    "BLOCKED": ("QUEUED", "REJECTED", "EXPIRED"),
    "PUBLISHED_CONFIRMED": (),
    "FAILED": (),
    "REJECTED": (),
    "EXPIRED": (),
}


class JobNotFound(LookupError):
    pass


class LeaseLost(RuntimeError):
    """O job já não pertence a quem tentou mexer-lhe (lease expirada e reentregue)."""


class InvalidTransition(ValueError):
    def __init__(self, job_id: int, current: str, target: str) -> None:
        super().__init__(f"job {job_id}: transição inválida {current} → {target}")
        self.job_id, self.current, self.target = job_id, current, target


def record_event(db: Session, job_id: int, event_type: str, *, from_status: str | None = None,
                 to_status: str | None = None, actor: str = "system",
                 execution_id: str | None = None, details: dict[str, Any] | None = None) -> None:
    db.execute(
        text("""INSERT INTO publication_events
                  (job_id, event_type, from_status, to_status, actor, execution_id, details)
                VALUES (:j, :t, :f, :to, :a, :e, CAST(:d AS JSONB))"""),
        {"j": job_id, "t": event_type, "f": from_status, "to": to_status, "a": actor,
         "e": execution_id, "d": json.dumps(details or {}, ensure_ascii=False)},
    )


def transition(db: Session, job_id: int, target: str, *, actor: str = "system",
               expected: str | tuple[str, ...] | None = None, owner: str | None = None,
               execution_id: str | None = None, details: dict[str, Any] | None = None,
               **fields: Any) -> str:
    """Muda o estado de um job. Devolve o estado anterior.

    `expected`: estado(s) em que o chamador acha que o job está; se já não
    estiver, a transição é recusada (outro processo adiantou-se).
    `owner`: quem diz deter a lease. Um worker passa sempre o seu; se a lease já
    for de outro (expirou e o job foi reentregue), a transição falha com
    `LeaseLost` e o worker tem de largar o job.

    Regras garantidas aqui, no ponto único, e não por convenção de quem chama:
    - entrar num estado ativo exige lease (prazo e dono): sem ela a reconciliação
      nunca apanharia o job e o site ficaria ocupado para sempre;
    - entrar em `APPROVED` exige uma decisão humana registada em `approvals`
      (D-006), gravada na mesma transação.
    `fields`: colunas do job a gravar na mesma operação (`blocked_reason`,
    `lease_until_utc`, `wordpress_post_id`…).
    """
    if target not in TRANSITIONS:
        raise ValueError(f"estado desconhecido: {target!r}")
    unknown = set(fields) - _SETTABLE
    if unknown:
        raise ValueError(f"colunas não alteráveis numa transição: {', '.join(sorted(unknown))}")

    row = db.execute(
        text("SELECT status, lease_until_utc, lease_owner FROM publication_jobs "
             "WHERE id = :i FOR UPDATE"),
        {"i": job_id},
    ).first()
    if row is None:
        raise JobNotFound(f"job {job_id} não existe")
    current, lease_until, lease_owner = row
    if owner is not None and lease_owner != owner:
        raise LeaseLost(f"job {job_id}: a lease é de {lease_owner!r}, não de {owner!r}")
    allowed = (expected,) if isinstance(expected, str) else expected
    if (allowed is not None and current not in allowed) or target not in TRANSITIONS[current]:
        raise InvalidTransition(job_id, current, target)

    if target in ACTIVE:
        if fields.get("lease_until_utc", lease_until) is None \
                or not fields.get("lease_owner", lease_owner):
            raise ValueError(f"job {job_id}: {target} exige lease (lease_until_utc e lease_owner)")
    else:
        # Fora dos estados ativos não há lease.
        fields.setdefault("lease_until_utc", None)
        fields.setdefault("lease_owner", None)

    if target in _CLEARS_REASON:
        # O motivo de um bloqueio ou falha anterior já foi superado; a história fica nos eventos.
        fields.setdefault("blocked_reason", None)

    if target == "APPROVED" and not db.execute(
        text("SELECT 1 FROM approvals WHERE job_id = :i AND decision = 'approved' LIMIT 1"),
        {"i": job_id},
    ).first():
        raise ValueError(f"job {job_id}: APPROVED exige uma aprovação registada (D-006)")
    assignments = "".join(f", {column} = :{column}" for column in fields)
    db.execute(
        text(f"UPDATE publication_jobs SET status = :status, updated_at = now(){assignments} "
             "WHERE id = :id"),  # noqa: S608 — nomes de coluna validados em _SETTABLE
        {"status": target, "id": job_id, **fields},
    )
    record_event(db, job_id, "status_changed", from_status=current, to_status=target,
                 actor=actor, execution_id=execution_id, details=details)
    return str(current)


_SETTABLE = frozenset({
    "idea_id", "attempt_count", "lease_until_utc", "lease_owner", "retry_after_utc",
    "blocked_reason", "writer_family", "wordpress_post_id", "wordpress_url",
    "published_at_utc",
})


class MissingReason(ValueError):
    """Reenfileirar sem dizer porquê (E18.4b: o motivo é obrigatório)."""


def requeue_blocked(db: Session, job_id: int, actor: str, reason: str) -> None:
    """BLOCKED → QUEUED pelo portal: a produção recomeça do zero (pesquisa, gates, revisão e
    aprovação de novo; nenhum gate é encurtado, R41). O motivo é obrigatório e vai para o evento
    do job e para o `audit_log` (E18.4b)."""
    from app.services import users

    reason = " ".join(reason.split())
    if len(reason) < 3:
        raise MissingReason("diga o motivo para devolver o job à fila")
    transition(db, job_id, "QUEUED", actor=actor, expected="BLOCKED",
               details={"motivo": reason[:500]}, blocked_reason=None, retry_after_utc=None)
    users.audit(db, actor, "job.requeue", f"job:{job_id}", {"motivo": reason[:500]})
