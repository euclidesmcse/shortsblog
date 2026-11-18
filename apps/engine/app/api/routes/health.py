"""Health checks e métricas Prometheus (o stack de monitoria já existe)."""

from __future__ import annotations

import json

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Gauge, generate_latest
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import text

from app.core.cache import redis_client
from app.core.config import settings
from app.core.db import engine, session_scope
from app.services import vault

router = APIRouter(tags=["health"])

# Métricas expostas ao Prometheus já existente no servidor.
G_QUEUE = Gauge("sb_queue_depth", "Jobs na fila", ["queue"])
G_COST = Gauge("sb_llm_cost_usd_30d", "Custo de LLM nos últimos 30 dias (USD)")
G_SCHED_AGE = Gauge(
    "sb_sched_last_call_age_seconds",
    "Segundos desde a última chamada do n8n a cada rota do agendador",
    ["route"],
)
G_SCHED_OK = Gauge("sb_sched_ok", "1 se o n8n chama o motor em dia (ou não armou), 0 se parou")


telemetry_sample = tuple[str, str, dict[str, str], float]


class MultiSiteCollector:
    """E12.1: as métricas dos blogs, dos jobs e dos provedores, lidas da base a cada scrape."""

    def collect(self):
        from app.services import telemetry

        try:
            with session_scope() as db:
                rows = telemetry.samples(db)
        except Exception:  # noqa: BLE001 — /metrics nunca pode falhar
            return
        rows = rows + self._probes_and_alerts()
        families: dict[str, GaugeMetricFamily] = {}
        for name, help_, labels, value in rows:
            family = families.setdefault(name, GaugeMetricFamily(name, help_, labels=list(labels)))
            family.add_metric(list(labels.values()), value)
        yield from families.values()

    @staticmethod
    def _probes_and_alerts() -> list[telemetry_sample]:
        """E12.2/E12.3: estado das sondas (blogs e painel), certificados e alertas em curso."""
        from app.services import alerts, probes

        out: list[telemetry_sample] = []
        try:
            with session_scope() as db:
                found = probes.results(db)
                hosts = probes.hostnames(db)
            for name, result in found.items():
                if result is None:
                    continue
                out.append(
                    (
                        "sb_site_up",
                        "1 se o blog ou o painel responde",
                        {"site": name},
                        1 if result["ok"] else 0,
                    )
                )
                out.append(
                    (
                        "sb_site_latency_seconds",
                        "Latência da última sonda",
                        {"site": name},
                        result["ms"] / 1000,
                    )
                )
            for host, days in probes.cert_days(hosts).items():
                out.append(
                    ("sb_cert_days_left", "Dias de validade do certificado", {"host": host}, days)
                )
            for key in alerts.active():
                out.append(("sb_alert_active", "Alertas em curso", {"alert": key}, 1))
        except Exception:  # noqa: BLE001 — /metrics nunca pode falhar
            pass
        return out


REGISTRY.register(MultiSiteCollector())


@router.get("/health/live", summary="Liveness probe")
def live() -> dict:
    return {"status": "ok", "service": "sb-engine"}


@router.get("/health/scheduler", summary="O n8n está a chamar o motor? (503 se parou)")
def scheduler_health() -> Response:
    from app.services import watchdog

    current = watchdog.status()
    body = {"armed": current["armed"], "stale": current["stale"], "ages_s": current["ages"]}
    down = current["armed"] and not current["ok"]
    code = 503 if down else 200
    return Response(json.dumps(body), status_code=code, media_type="application/json")


@router.get("/health/ready", summary="Readiness probe — checa as dependências")
def ready() -> dict:
    checks: dict[str, str] = {}
    healthy = True

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["postgres"] = f"erro: {exc}"[:120]
        healthy = False

    try:
        redis_client.ping()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = f"erro: {exc}"[:120]
        healthy = False

    try:
        from app.services import readiness

        with session_scope() as db:
            state = readiness.summary(db)
        checks["redacao"] = ("ok: " + ", ".join(state["writer"]["providers"])
                             if state["writer"]["ok"] else "SEM PROVEDOR OK")
        checks["revisao"] = ("ok: " + ", ".join(state["reviewer"]["providers"])
                             if state["reviewer"]["ok"] else "SEM PROVEDOR OK")
        wp = state["wordpress"]
        faltam = "" if wp["ok"] else " (faltam: " + ", ".join(wp["missing"]) + ")"
        checks["wordpress"] = f"{wp['have']}/{wp['total']} sites com credencial{faltam}"
    except Exception as exc:  # noqa: BLE001
        checks["prontidao"] = f"erro: {exc}"[:120]
    checks["mail"] = "configurado" if vault.mail_ready() else "SEM CANAL"

    return {
        "status": "ok" if healthy else "degraded",
        "dry_run": settings.dry_run,
        "checks": checks,
    }


@router.get("/metrics", summary="Métricas Prometheus")
def metrics() -> Response:
    try:
        with session_scope() as db:
            G_COST.set(
                float(
                    db.execute(
                        text(
                            "SELECT COALESCE(SUM(cost_usd), 0) FROM provider_usage "
                            " WHERE created_at >= now() - interval '30 days'"
                        )
                    ).scalar_one()
                )
            )

        from app.core.cache import QUEUES

        for name, queue in QUEUES.items():
            G_QUEUE.labels(queue=name).set(len(queue))
    except Exception:  # noqa: BLE001 — /metrics nunca pode falhar
        pass
    try:
        from app.services import watchdog

        current = watchdog.status()
        for route, age in current["ages"].items():
            if age is not None:
                G_SCHED_AGE.labels(route=route).set(age)
        G_SCHED_OK.set(0 if current["armed"] and not current["ok"] else 1)
    except Exception:  # noqa: BLE001
        pass

    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
