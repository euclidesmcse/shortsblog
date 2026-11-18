"""Worker RQ.

E4.6 — há dois workers na stack, cada um com as suas filas (`WORKER_QUEUES`):

    content → content, low, default   produção longa (pesquisa, redação, tradução)
    fast    → fast, high              publicação, aprovações, emails

Assim um artigo que leva 20 minutos a produzir não atrasa uma publicação. Sem
`WORKER_QUEUES` o worker consome as filas herdadas (high → default → low).
"""
from __future__ import annotations

import os
import sys

from rq import Worker

from app.core.cache import QUEUES, rq_redis
from app.core.config import settings
from app.core.logging import get_logger, setup_logging

setup_logging()
log = get_logger(__name__)

DEFAULT_QUEUES = "high,default,low"


def queue_names() -> list[str]:
    names = [n.strip() for n in os.getenv("WORKER_QUEUES", DEFAULT_QUEUES).split(",") if n.strip()]
    unknown = [n for n in names if n not in QUEUES]
    if unknown or not names:
        raise SystemExit(f"WORKER_QUEUES inválido: {unknown or 'vazio'} (filas: {sorted(QUEUES)})")
    return names


def main(burst: bool = False) -> None:
    names = queue_names()
    log.info("Worker ShortsBlog iniciando (filas=%s, dry_run=%s)", ",".join(names),
             settings.dry_run)
    worker = Worker([QUEUES[name] for name in names], connection=rq_redis)
    # Cada worker corre o agendador interno do RQ para as suas próprias filas.
    worker.work(with_scheduler=not burst, burst=burst)


if __name__ == "__main__":
    main(burst="--burst" in sys.argv[1:])
