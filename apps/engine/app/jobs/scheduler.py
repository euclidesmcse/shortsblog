"""Scheduler interno (APScheduler).

Este processo corre sempre as tarefas de casa do motor: o probe dos provedores de IA, o vigia do
agendador e a monitorização dos blogs.

Os horários do ShortsBlog (planear o dia, entregar, publicar, reconciliar, ler os feeds, lotes de
aprovação, resumo e newsletter) são as rotas `/api/sched/*`. Quem as dispara depende da instalação:

* `INTERNAL_SCHEDULER_ENABLED=true` (o pacote da E19.2): este processo chama as mesmas funções nos
  mesmos horários do n8n (`ROUTES`) e regista cada chamada no vigia, como faria a rota.
* `INTERNAL_SCHEDULER_ENABLED=false` (a nossa produção): o n8n chama as rotas; aqui só correm as
  tarefas de casa, num laço simples. **Nunca ligar os dois**: o trabalho é idempotente, mas cada
  horário correria duas vezes.

Ativo ou não, o processo grava um heartbeat a cada minuto em `HEARTBEAT_FILE` — é o que o
healthcheck do container lê (`is_healthy`).
"""
from __future__ import annotations

import os
import pathlib
import sys
import time
from datetime import datetime

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.base import BaseTrigger
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.logging import get_logger, setup_logging

setup_logging()
log = get_logger(__name__)

ENABLED = settings.internal_scheduler_enabled
HEARTBEAT_FILE = pathlib.Path(os.getenv("SCHEDULER_HEARTBEAT_FILE", "/tmp/scheduler.heartbeat"))
HEARTBEAT_EVERY = 60
HEARTBEAT_MAX_AGE = 180


def beat() -> None:
    HEARTBEAT_FILE.write_text(str(time.time()))


def is_healthy(now: float | None = None) -> bool:
    """Heartbeat recente = processo vivo e a agendar (ou a esperar, se desativado)."""
    try:
        last = float(HEARTBEAT_FILE.read_text())
    except (OSError, ValueError):
        return False
    return (now if now is not None else time.time()) - last < HEARTBEAT_MAX_AGE


def probe_ai() -> None:
    """E3.6: saúde dos provedores de IA. Corre sempre, mesmo com as fases desligadas."""
    try:
        from app.services.ai import health

        health.probe_all()
    except Exception:  # noqa: BLE001 — o probe nunca pode derrubar o scheduler
        log.exception("Probe dos provedores de IA falhou")


def run_watchdog() -> None:
    """E11.4: avisa por e-mail se os horários pararem (n8n ou agendador interno). Corre sempre."""
    try:
        from app.services import watchdog

        watchdog.check()
    except Exception:  # noqa: BLE001 — o vigia nunca pode derrubar o scheduler
        log.exception("Vigia do agendador falhou")


def run_monitoring() -> None:
    """E12.2/E12.3: sonda os blogs e o painel e avalia os alertas, de minuto a minuto."""
    try:
        from app.core.db import session_scope
        from app.services import alerts, probes

        with session_scope() as db:
            probes.run(db)
        alerts.evaluate()
    except Exception:  # noqa: BLE001 — a monitorização nunca pode derrubar o scheduler
        log.exception("Monitorização falhou")


# Os horários do n8n (n8n/workflows/SB-*.json), no fuso do motor (`TZ`). A rota é o nome que o
# vigia conhece; a função é a mesma que a rota HTTP chama. O fuso vai explícito em cada gatilho
# (sem ele o APScheduler usa o fuso do sistema, não o do scheduler).
_TZ = settings.tz
ROUTES: list[tuple[str, str, BaseTrigger]] = [
    ("plan-day", "plan_day", CronTrigger(hour=0, minute=5, timezone=_TZ)),
    ("dispatch", "dispatch", CronTrigger(minute="*", timezone=_TZ)),
    ("publish-due", "publish_due", CronTrigger(minute="*", timezone=_TZ)),
    ("reconcile", "reconcile", CronTrigger(minute="*/5", timezone=_TZ)),
    ("discover", "discover", CronTrigger(minute=0, timezone=_TZ)),
    # D-066: 06:30, 10:00, 13:00, 16:00 e 20:00
    ("approval-batch", "approval_batch",
     OrTrigger([CronTrigger(hour=6, minute=30, timezone=_TZ),
                CronTrigger(hour="10,13,16,20", minute=0, timezone=_TZ)])),
    ("digest", "digest", CronTrigger(hour=21, minute=0, timezone=_TZ)),
    ("newsletter", "newsletter_daily", CronTrigger(hour=7, minute=0, timezone=_TZ)),
]


def run_route(route: str, func_name: str) -> None:
    """Corre um horário como o n8n o faria: regista no vigia e chama a função da rota."""
    try:
        from app.api.routes import sched
        from app.services import watchdog

        watchdog.record(route)
        result = getattr(sched, func_name)()
        log.info("Agendador interno: %s → %s", route, result)
    except Exception:  # noqa: BLE001 — um horário que falha não pode derrubar os outros
        log.exception("Agendador interno: %s falhou", route)


def main() -> None:
    beat()
    if not ENABLED:
        log.info("Scheduler interno desativado (INTERNAL_SCHEDULER_ENABLED=false)")
        # Mantém o container vivo sem consumir CPU, com heartbeat para o healthcheck.
        next_probe = 0.0
        while True:
            if time.monotonic() >= next_probe:
                probe_ai()
                next_probe = time.monotonic() + settings.ai_probe_interval_s
                beat()
            run_watchdog()
            run_monitoring()
            time.sleep(HEARTBEAT_EVERY)
            beat()

    scheduler = BlockingScheduler(timezone=settings.tz)
    scheduler.add_job(beat, "interval", seconds=HEARTBEAT_EVERY, id="sb-heartbeat",
                      replace_existing=True, max_instances=1)

    scheduler.add_job(run_watchdog, "interval", seconds=60, id="sb-watchdog",
                      replace_existing=True, max_instances=1)
    scheduler.add_job(run_monitoring, "interval", seconds=60, id="sb-monitoring",
                      replace_existing=True, max_instances=1)
    scheduler.add_job(probe_ai, "interval", seconds=settings.ai_probe_interval_s,
                      id="sb-ai-probe", replace_existing=True, max_instances=1,
                      next_run_time=datetime.now())

    for route, func_name, trigger in ROUTES:
        scheduler.add_job(run_route, trigger, args=[route, func_name], id=f"sb-route-{route}",
                          replace_existing=True, max_instances=1, coalesce=True,
                          misfire_grace_time=120)
        log.info("Agendado: %s → %s", route, trigger)

    log.info("Scheduler interno iniciado com os horários do ShortsBlog (tz=%s)", settings.tz)
    scheduler.start()


if __name__ == "__main__":
    if sys.argv[1:] == ["--healthcheck"]:
        sys.exit(0 if is_healthy() else 1)
    main()
