"""E18.4b: Jobs — a fila de produção e publicação, com eventos, chamadas de IA e requeue.

Operador e admin. O «reenfileirar» de um job BLOCKED pede o motivo (vai para o evento do job e
para o `audit_log`) e recomeça a produção do zero, sem atalho nos gates (R41).
"""
from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.routes.dashboard import when_local
from app.core.config import settings
from app.core.db import get_db
from app.services import jobs

router = APIRouter(prefix="/dashboard/jobs", tags=["dashboard"])
templates = Jinja2Templates(directory="app/dashboard/templates")
templates.env.filters["when"] = when_local

ACTIVE = ("PLANNED", "QUEUED", "CLAIMED", "RESEARCHING", "WRITING", "VALIDATING", "TRANSLATING",
          "RETRY_WAIT", "APPROVED", "PUBLISHING", "VERIFY_REQUIRED")
GROUPS = {
    "todos": ("Todos", None),
    "ativos": ("Em produção ou à espera", ACTIVE),
    "aprovacao": ("Aguardando aprovação", ("AWAITING_APPROVAL",)),
    "bloqueados": ("Bloqueados", ("BLOCKED",)),
    "publicados": ("Publicados", ("PUBLISHED_CONFIRMED",)),
    "encerrados": ("Rejeitados, falhados, expirados", ("REJECTED", "FAILED", "EXPIRED")),
}
STATUS_CLASS = {"PUBLISHED_CONFIRMED": "ok", "AWAITING_APPROVAL": "warn", "APPROVED": "ok",
                "BLOCKED": "bad", "FAILED": "bad", "REJECTED": "bad", "EXPIRED": "bad",
                "RETRY_WAIT": "warn", "VERIFY_REQUIRED": "bad"}
MAX_ROWS = 300


def _actor(request: Request) -> str:
    return str(request.state.panel_user or "painel")[:120]


def _ctx(request: Request, **extra) -> dict:
    return {"brand": settings.brand_name, "primary": settings.brand_primary_color,
            "accent": settings.brand_accent_color, "user": request.state.panel_user,
            "status_class": STATUS_CLASS, **extra}


def _date(raw: str, default: date) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return default


@router.get("", response_class=HTMLResponse, summary="Jobs")
def page(request: Request, de: str = "", ate: str = "", blog: str = "", grupo: str = "todos",
         msg: str = "", err: str = "", db: Session = Depends(get_db)):
    today = date.today()
    start, end = _date(de, today - timedelta(days=3)), _date(ate, today + timedelta(days=1))
    if end < start:
        start, end = end, start
    group = grupo if grupo in GROUPS else "todos"
    where = ["j.local_date BETWEEN :de AND :ate"]
    params: dict = {"de": start, "ate": end, "n": MAX_ROWS}
    if blog:
        where.append("j.site_key = :blog")
        params["blog"] = blog
    statuses = GROUPS[group][1]
    if statuses:
        where.append("j.status = ANY(:st)")
        params["st"] = list(statuses)
    rows = db.execute(text(f"""
        SELECT j.id, j.site_key, j.local_date, j.slot_number, j.status, j.due_at_utc,
               j.published_at_utc, j.wordpress_url, j.attempt_count, j.writer_family,
               j.blocked_reason, j.updated_at,
               (SELECT title FROM job_translations t
                 WHERE t.job_id = j.id AND t.language = 'pt-BR') AS title,
               (SELECT count(*) FROM provider_usage u WHERE u.job_id = j.id) AS calls,
               (SELECT string_agg(provider || ' ' || n, ' · ' ORDER BY provider)
                  FROM (SELECT provider, count(*) AS n FROM provider_usage u
                         WHERE u.job_id = j.id GROUP BY provider) x) AS by_provider
          FROM publication_jobs j
         WHERE {' AND '.join(where)}
         ORDER BY j.local_date DESC, j.due_at_utc DESC, j.id DESC LIMIT :n"""),  # noqa: S608
        params).mappings().all()
    counts: dict[str, int] = dict(db.execute(text(
        "SELECT status, count(*) FROM publication_jobs WHERE local_date BETWEEN :de AND :ate "
        + ("AND site_key = :blog " if blog else "") + "GROUP BY status"),
        {k: v for k, v in params.items() if k in ("de", "ate", "blog")}).all())
    blogs = [k for k, in db.execute(text(
        "SELECT site_key FROM sites ORDER BY slot_offset_minutes, site_key"))]
    return templates.TemplateResponse(
        request=request, name="jobs.html",
        context=_ctx(request, rows=[dict(r) for r in rows], blogs=blogs, blog=blog,
                     group=group, groups={k: v[0] for k, v in GROUPS.items()},
                     de=start.isoformat(), ate=end.isoformat(), counts=counts,
                     limit=MAX_ROWS, msg=msg, err=err),
    )


@router.get("/{job_id}", response_class=HTMLResponse, summary="Um job: eventos e chamadas")
def detail(job_id: int, request: Request, msg: str = "", err: str = "",
           db: Session = Depends(get_db)):
    job = db.execute(text(
        """SELECT j.*, (SELECT title FROM job_translations t
                         WHERE t.job_id = j.id AND t.language = 'pt-BR') AS title,
                  i.topic AS idea_topic, i.origin AS idea_origin
             FROM publication_jobs j LEFT JOIN ideas i ON i.id = j.idea_id
            WHERE j.id = :i"""), {"i": job_id}).mappings().first()
    if job is None:
        missing = urlencode({"err": f"Job {job_id} não existe."})
        return RedirectResponse(f"/dashboard/jobs?{missing}", status_code=303)
    events = db.execute(text(
        """SELECT timestamp_utc, event_type, from_status, to_status, actor, details
             FROM publication_events WHERE job_id = :i ORDER BY id"""), {"i": job_id}).mappings()
    usage = db.execute(text(
        """SELECT provider, role, purpose, count(*) AS calls,
                  count(*) FILTER (WHERE outcome <> 'ok') AS failures,
                  sum(input_tokens) AS tin, sum(output_tokens) AS tout
             FROM provider_usage WHERE job_id = :i
            GROUP BY provider, role, purpose ORDER BY count(*) DESC"""), {"i": job_id}).mappings()
    return templates.TemplateResponse(
        request=request, name="job_detail.html",
        context=_ctx(request, job=dict(job), events=[dict(e) for e in events],
                     usage=[dict(u) for u in usage], msg=msg, err=err),
    )


@router.post("/{job_id}/requeue", summary="Devolve um job BLOCKED à fila (com motivo)")
def requeue(job_id: int, request: Request, motivo: str = Form(""), back: str = Form(""),
            db: Session = Depends(get_db)):
    target = back if back.startswith("/dashboard/jobs") else f"/dashboard/jobs/{job_id}"
    sep = "&" if "?" in target else "?"
    try:
        jobs.requeue_blocked(db, job_id, _actor(request), motivo)
    except jobs.MissingReason:
        return RedirectResponse(f"{target}{sep}" + urlencode(
            {"err": f"Job {job_id}: diga o motivo para devolvê-lo à fila."}), status_code=303)
    except jobs.JobNotFound:
        return RedirectResponse(f"{target}{sep}" + urlencode({"err": f"Job {job_id} não existe."}),
                                status_code=303)
    except jobs.InvalidTransition:
        return RedirectResponse(f"{target}{sep}" + urlencode(
            {"err": f"Job {job_id} já não está BLOCKED; nada foi mudado."}), status_code=303)
    db.commit()
    return RedirectResponse(f"{target}{sep}" + urlencode(
        {"msg": f"Job {job_id} devolvido à fila; o próximo dispatch volta a produzi-lo."}),
        status_code=303)
