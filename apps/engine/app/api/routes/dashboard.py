"""Painel operacional.

Protegido por `core/panel_auth.py` (D-018): Cloudflare Access + sessão do login
próprio. As aprovações ficam registadas com o utilizador da sessão (o email do
Access), nunca com um valor vindo do formulário.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.logging import get_logger
from app.services import readiness, setup, vault

log = get_logger(__name__)

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="app/dashboard/templates")


_PUBLISHED = "PUBLISHED_CONFIRMED"
_PENDING = ("AWAITING_APPROVAL", "APPROVED", "PUBLISHING", "VERIFY_REQUIRED")
_STUCK = ("BLOCKED", "RETRY_WAIT", "FAILED", "EXPIRED")


def panel_zones(spec: str | None = None) -> list[tuple[ZoneInfo, str]]:
    """Fusos do portal (`settings.panel_timezones`, ou o `TZ` da instalação), com rótulo.

    Rótulo padrão: a cidade do identificador IANA (`America/Sao_Paulo` → `Sao Paulo`). Fuso
    desconhecido é ignorado com aviso; sem nenhum válido, UTC (a tela nunca quebra por isso)."""
    raw = settings.panel_timezones if spec is None else spec
    zones: list[tuple[ZoneInfo, str]] = []
    for item in (raw or settings.tz or "UTC").split(","):
        name, _, label = item.strip().partition("=")
        if not name:
            continue
        try:
            zone = ZoneInfo(name.strip())
        except (ZoneInfoNotFoundError, ValueError):
            log.warning("PANEL_TIMEZONES: fuso desconhecido ignorado: %s", name)
            continue
        zones.append((zone, label.strip() or name.strip().rsplit("/", 1)[-1].replace("_", " ")))
    return zones or [(ZoneInfo("UTC"), "UTC")]


def _dashboard_data(db: Session, days: int) -> dict:
    """Números do painel tirados dos jobs de publicação (o fluxo do ShortsBlog)."""
    since = {"d": days}
    totals = {
        "published": db.execute(
            text("SELECT COUNT(*) FROM publication_jobs WHERE status = :s "
                 "AND published_at_utc >= now() - make_interval(days => :d)"),
            {**since, "s": _PUBLISHED},
        ).scalar_one(),
        "awaiting": db.execute(
            text("SELECT COUNT(*) FROM publication_jobs WHERE status = 'AWAITING_APPROVAL'")
        ).scalar_one(),
        "stuck": db.execute(
            text("SELECT COUNT(*) FROM publication_jobs WHERE status = ANY(:st) "
                 "AND updated_at >= now() - make_interval(days => :d)"),
            {**since, "st": list(_STUCK)},
        ).scalar_one(),
        "ai_calls": db.execute(
            text("SELECT COUNT(*) FROM provider_usage "
                 "WHERE created_at >= now() - make_interval(days => :d)"), since
        ).scalar_one(),
        "cost_usd": float(db.execute(
            text("SELECT COALESCE(SUM(cost_usd), 0) FROM provider_usage "
                 " WHERE created_at >= now() - make_interval(days => :d)"), since
        ).scalar_one()),
    }
    providers = db.execute(
        text("SELECT provider, COUNT(*) AS calls, "
             "       COUNT(*) FILTER (WHERE outcome = 'failed') AS failed "
             "  FROM provider_usage WHERE created_at >= now() - make_interval(days => :d) "
             " GROUP BY 1 ORDER BY 2 DESC"), since
    ).mappings().all()
    blogs = db.execute(
        text("""SELECT s.site_key, s.name, s.base_url, s.enabled, s.daily_target,
                  (SELECT COUNT(*) FROM publication_jobs j WHERE j.site_key = s.site_key
                     AND j.status = :pub
                     AND j.published_at_utc >= now() - interval '7 days') AS week,
                  (SELECT COUNT(*) FROM publication_jobs j WHERE j.site_key = s.site_key
                     AND j.status = :pub
                     AND j.published_at_utc >= now() - make_interval(days => :d)) AS period,
                  (SELECT COUNT(*) FROM publication_jobs j WHERE j.site_key = s.site_key
                     AND j.status = :pub) AS total,
                  (SELECT COUNT(*) FROM ideas i WHERE i.site_key = s.site_key
                     AND i.status = 'new') AS ideas_new,
                  last.wordpress_url AS last_url,
                  last.published_at_utc AS last_at,
                  (SELECT j.due_at_utc FROM publication_jobs j
                    WHERE j.site_key = s.site_key AND j.status <> ALL(:done)
                      AND j.due_at_utc >= now() - interval '1 day'
                    ORDER BY j.due_at_utc LIMIT 1) AS next_due
             FROM sites s
             LEFT JOIN LATERAL (
                  SELECT j.wordpress_url, j.published_at_utc FROM publication_jobs j
                   WHERE j.site_key = s.site_key AND j.status = :pub
                   ORDER BY j.published_at_utc DESC LIMIT 1) last ON true
            ORDER BY s.enabled DESC, s.slot_offset_minutes, s.site_key"""),
        {**since, "pub": _PUBLISHED, "done": [_PUBLISHED, "REJECTED", "FAILED", "EXPIRED"]},
    ).mappings().all()
    return {"totals": totals, "providers": [dict(p) for p in providers],
            "blogs": [dict(b) for b in blogs]}


def _jobs(db: Session, where: str, limit: int) -> list[dict]:
    rows = db.execute(
        text(f"""SELECT j.id, j.site_key, j.local_date, j.status, j.due_at_utc,
                        j.published_at_utc, j.wordpress_url, j.blocked_reason, j.updated_at,
                        (SELECT title FROM job_translations t
                          WHERE t.job_id = j.id AND t.language = 'pt-BR') AS title
                   FROM publication_jobs j WHERE {where}
                  ORDER BY j.due_at_utc DESC, j.id DESC LIMIT :n"""),
        {"n": limit},
    ).mappings().all()
    return [dict(r) for r in rows]


def when_local(moment: datetime | None) -> str:
    """`dd/mm HH:MM <rótulo>` no 1.º fuso do portal e ` · HH:MM <rótulo>` nos outros (ou `—`)."""
    if moment is None:
        return "—"
    (first, label), *others = panel_zones()
    text = f"{moment.astimezone(first):%d/%m %H:%M} {label}"
    return text + "".join(f" · {moment.astimezone(z):%H:%M} {lab}" for z, lab in others)


templates.env.filters["when"] = when_local


@router.get("/", response_class=HTMLResponse, summary="Sala de controlo")
def home(request: Request, days: int = 30, db: Session = Depends(get_db)):
    days = max(1, min(days, 365))
    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "data": _dashboard_data(db, days),
            "pending": _jobs(db, "j.status = 'AWAITING_APPROVAL'", 25),
            "recent": _jobs(db, "true", 15),
            "brand": settings.brand_name,
            "primary": settings.brand_primary_color,
            "accent": settings.brand_accent_color,
            "days": days,
            "user": _user(request),
            "config": {
                "dry_run": settings.dry_run,
                "human_gate": settings.require_human_approval,
                "ready": readiness.summary(db),
                "mail": vault.mail_ready(),
                "publish_mode": settings.wp_publish_mode,
                "first_slot": vault.slot_times()[0].strftime("%H:%M"),
            },
            # E19.5: aviso do assistente de 1.º arranque (só o admin pode fazer os passos).
            "setup_pending": (getattr(request.state, "panel_role", None) == "admin"
                              and setup.pending(db)),
        },
    )


def _user(request: Request) -> str:
    return str(getattr(request.state, "panel_user", None) or "painel")[:120]
