"""Painel "Saúde" (E12.2): blogs, alertas, provedores, orçamentos, fila e custos num só sítio.

Protegido como o resto do painel (`core/panel_auth.py`). Só leitura: lê as sondas e os alertas do
Redis e as métricas da base (`telemetry`), as mesmas que o Prometheus recebe em `/metrics`.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.services import alerts, newsletter, probes, telemetry, vault, watchdog

router = APIRouter(prefix="/dashboard/health", tags=["dashboard"])
templates = Jinja2Templates(directory="app/dashboard/templates")


def _by(rows: list[telemetry.Sample], name: str) -> list[dict]:
    return [{**labels, "value": value} for n, _help, labels, value in rows if n == name]


@router.get("", response_class=HTMLResponse, summary="Saúde do sistema")
def page(request: Request, db: Session = Depends(get_db)):
    rows = telemetry.samples(db)
    now = time.time()
    sites = []
    for name, result in probes.results(db).items():
        sites.append(
            {
                "name": name,
                "result": result,
                "down_for": int(now - result["fail_since"])
                if result and result.get("fail_since")
                else 0,
            }
        )
    firing = [
        {"key": key, **state, "since": int(now - state["first"])}
        for key, state in alerts.active().items()
    ]
    single = {n: v for n, _h, labels, v in rows if not labels}
    return templates.TemplateResponse(
        request=request,
        name="health.html",
        context={
            "brand": settings.brand_name,
            "primary": settings.brand_primary_color,
            "accent": settings.brand_accent_color,
            "user": str(getattr(request.state, "panel_user", "")),
            "sites": sites,
            "alerts": firing,
            "scheduler": watchdog.status(),
            "providers": _by(rows, "sb_provider_status"),
            "budgets": _by(rows, "sb_budget_used_ratio"),
            "stuck": _by(rows, "sb_jobs_stuck"),
            "blocked": _by(rows, "sb_jobs_blocked_today"),
            "costs": _by(rows, "sb_provider_cost_usd"),
            "stages": _by(rows, "sb_stage_avg_seconds"),
            "per_post": single.get("sb_cost_per_post_usd"),
            "calls_per_post": single.get("sb_calls_per_post"),
            "approval_s": single.get("sb_time_to_approval_seconds"),
            "mail_ready": vault.mail_ready(),
            "notify": vault.get("approval_notify_email"),
            "newsletter": newsletter.panel_summary(db),
        },
    )
