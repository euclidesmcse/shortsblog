"""Publicação na hora certa, feita pelo motor (E10.4): `publish-due`, chamado pelo n8n.

Um job aprovado antes do slot publica à hora do slot; aprovado depois, publica logo (na corrida
seguinte). Não se usa `status=future` do WordPress: depende do WP-Cron (R18). A publicação em si é
`sitepub.publish_job`, no worker `fast`. No modo `draft` (até à E14) cria-se os rascunhos uma só vez
por job; no modo `live` também se retomam as publicações em espera (`RETRY_WAIT` já na hora e
`VERIFY_REQUIRED`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.cache import QUEUES
from app.core.config import settings
from app.core.logging import get_logger
from app.services import jobs, reserve, sitepub

log = get_logger(__name__)

ACTOR = "scheduler"
LIMIT = 5  # pedidos por corrida; o worker `fast` faz um de cada vez
RECENT = timedelta(minutes=2)  # um pedido recente ainda está a correr: não se repete


def enqueue_publish(job_id: int) -> None:
    """Põe a publicação na fila do worker `fast` (a publicação demora: mídias e 4 posts)."""
    QUEUES["fast"].enqueue(sitepub.publish_job, job_id, job_timeout=900)


def due_jobs(db: Session, now: datetime, mode: str) -> list[int]:
    """Jobs a publicar agora: um por blog ligado, o mais antigo primeiro, sem outro a publicar."""
    states = ["APPROVED"] if mode == "draft" else ["APPROVED", "RETRY_WAIT", "VERIFY_REQUIRED"]
    rows = (
        db.execute(
            text(f"""SELECT DISTINCT ON (j.site_key) j.id
                  FROM publication_jobs j JOIN sites s ON s.site_key = j.site_key
                 WHERE s.enabled AND j.status = ANY(:states) AND j.due_at_utc <= :now
                   AND NOT j.reserve  -- reserva guardada (D-115) só sai quando cobre um slot
                   -- aprovado depois do dia dele: não sai junto com o do dia; espera cobrir um
                   -- slot que falhe (`reserve.LATE_SQL`, 22/11: job 372)
                   AND NOT {reserve.LATE_SQL}
                   AND (j.status <> 'RETRY_WAIT' OR j.retry_after_utc IS NULL
                        OR j.retry_after_utc <= :now)
                   -- RETRY_WAIT também é a espera da PRODUÇÃO (fontes, cota de IA): só volta a
                   -- publicar o que já foi aprovado e falhou ao publicar (21/11: o job 372,
                   -- nunca aprovado, recebeu 3 pedidos de publicação; salvou-o a corrida).
                   AND (j.status <> 'RETRY_WAIT' OR EXISTS (
                        SELECT 1 FROM approvals a
                         WHERE a.job_id = j.id AND a.decision = 'approved'))
                   AND NOT EXISTS (SELECT 1 FROM publication_jobs p
                                    WHERE p.site_key = j.site_key AND p.status = 'PUBLISHING')
                   AND NOT EXISTS (SELECT 1 FROM publication_events e
                                    WHERE e.job_id = j.id AND e.event_type = 'publish_requested'
                                      AND e.timestamp_utc > :recent)
                   AND (:live OR NOT EXISTS (SELECT 1 FROM publication_events e
                                              WHERE e.job_id = j.id
                                                AND e.event_type = 'drafts_created'))
                 ORDER BY j.site_key, j.due_at_utc, j.id"""),
            {"states": states, "now": now, "recent": now - RECENT, "live": mode == "live"},
        )
        .scalars()
        .all()
    )
    return [int(i) for i in rows][:LIMIT]


def publish_due(
    db: Session, now: datetime | None = None, mode: str | None = None
) -> dict[str, object]:
    """Pede a publicação do que está na hora. Devolve o modo e os jobs pedidos."""
    moment = now or datetime.now(UTC)
    mode = mode or settings.wp_publish_mode
    requested: list[int] = []
    for job_id in due_jobs(db, moment, mode):
        jobs.record_event(
            db,
            job_id,
            "publish_requested",
            actor=ACTOR,
            details={"mode": mode, "channel": "scheduler"},
        )
        requested.append(job_id)
    db.commit()  # os pedidos ficam registados antes de irem para a fila
    for job_id in requested:
        enqueue_publish(job_id)
    if requested:
        log.info("publish-due (%s): %s", mode, requested)
    return {"mode": mode, "requested": requested}
