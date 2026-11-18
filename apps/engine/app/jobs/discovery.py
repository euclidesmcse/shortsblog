"""Tarefa de descoberta (fila `content`): lê os feeds dos sites ligados.

Um site de cada vez, com um lock de TTL curto renovado a cada site: duas
descobertas em simultâneo não leem os mesmos feeds a dobrar, e se o processo
morrer o lock cai sozinho em minutos.
"""
from __future__ import annotations

from typing import Any

from app.core.cache import ShortLock
from app.core.db import session_scope
from app.core.logging import get_logger
from app.services import discovery, sites

log = get_logger(__name__)

LOCK_TTL_S = 600


def run() -> dict[str, Any]:
    lock = ShortLock("discovery", ttl=LOCK_TTL_S)
    if not lock.acquire():
        log.info("Descoberta já em curso noutro worker: ignorada")
        return {"skipped": "already_running"}
    results: dict[str, Any] = {}
    try:
        with session_scope() as db:
            keys = [s["site_key"] for s in sites.enabled(db)]
        for key in keys:
            with session_scope() as db:
                results[key] = discovery.discover_site(db, key).as_dict()
            if not lock.renew():
                log.warning("Descoberta: lock perdido depois de %s; a parar", key)
                break
    finally:
        lock.release()
    log.info("Descoberta: %s", {k: (v["new"], v["failed"]) for k, v in results.items()})
    return results
