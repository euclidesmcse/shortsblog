"""Painel "Aprovações" (E10.1): lê e decide os artigos em AWAITING_APPROVAL.

Protegido como o resto do painel (`core/panel_auth.py`). A decisão grava-se em
`approvals` com o utilizador da sessão (nunca com um valor do formulário) e muda o
estado por `jobs.transition`, que exige essa linha para entrar em APPROVED (D-006).
Um clique vale para os 4 idiomas. A publicação é da E9: aqui o job só fica APPROVED.
Em modo `live`, o agendador publica o aprovado à hora do slot (D-066); o painel só
publica antes disso com a confirmação explícita «Publicar antes do horário» (`early`).
"""
from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.routes.dashboard import when_local
from app.core.config import settings
from app.core.db import get_db
from app.services import decisions, jobs, publishing, reserve

router = APIRouter(prefix="/dashboard/review", tags=["dashboard"])


def _hold_until(due: datetime) -> str:
    """Até quando um post atrasado espera um horário para cobrir (`late_hold_hours`)."""
    return when_local(due + timedelta(hours=settings.late_hold_hours))
templates = Jinja2Templates(directory="app/dashboard/templates")
templates.env.filters["when"] = when_local

LANGUAGES = ("pt-BR", "en", "es", "zh-CN")
LANGUAGE_LABELS = {"pt-BR": "Português", "en": "English", "es": "Español", "zh-CN": "中文"}


def _user(request: Request) -> str:
    return str(getattr(request.state, "panel_user", None) or "painel")[:120]


def _back(path: str = "", *, msg: str = "", err: str = "") -> RedirectResponse:
    query = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    return RedirectResponse(f"/dashboard/review{path}" + (f"?{query}" if query else ""),
                            status_code=303)


def _context(request: Request, **extra: object) -> dict[str, object]:
    return {"brand": settings.brand_name, "primary": settings.brand_primary_color,
            "accent": settings.brand_accent_color, "user": _user(request),
            "msg": request.query_params.get("msg", ""), "err": request.query_params.get("err", ""),
            **extra}


@router.get("", response_class=HTMLResponse, summary="Fila de aprovação")
def queue(request: Request, db: Session = Depends(get_db)):
    waiting = db.execute(
        text("""SELECT j.id, j.site_key, j.local_date, j.updated_at, j.review_report, j.reserve,
                       NOT j.reserve AND j.local_date < (now() AT TIME ZONE s.timezone)::date
                           AS late,
                       (SELECT title FROM job_translations t
                         WHERE t.job_id = j.id AND t.language = 'pt-BR') AS title
                  FROM publication_jobs j JOIN sites s ON s.site_key = j.site_key
                 WHERE j.status = 'AWAITING_APPROVAL'
                 ORDER BY j.site_key, j.updated_at DESC""")
    ).mappings().all()
    approved = db.execute(
        text(f"""SELECT j.id, j.site_key, j.due_at_utc, j.reserve,
                       {reserve.LATE_SQL} AS late,
                       (SELECT title FROM job_translations t
                         WHERE t.job_id = j.id AND t.language = 'pt-BR') AS title,
                       EXISTS (SELECT 1 FROM publication_events e
                                WHERE e.job_id = j.id AND e.event_type = 'drafts_created') AS drafts
                  FROM publication_jobs j JOIN sites s ON s.site_key = j.site_key
                 WHERE j.status = 'APPROVED'
                 ORDER BY j.site_key, j.updated_at DESC"""), {"now": datetime.now(UTC)}
    ).mappings().all()
    decided = db.execute(
        text("""SELECT a.job_id, a.decision, a.decided_by, a.decided_at, j.site_key,
                       (SELECT title FROM job_translations t
                         WHERE t.job_id = j.id AND t.language = 'pt-BR') AS title
                  FROM approvals a JOIN publication_jobs j ON j.id = a.job_id
                 ORDER BY a.decided_at DESC LIMIT 15""")
    ).mappings().all()
    return templates.TemplateResponse(
        request=request, name="review_queue.html",
        context=_context(request, waiting=[dict(w) for w in waiting],
                         approved=[dict(a) for a in approved],
                         publish_mode=settings.wp_publish_mode, now=datetime.now(UTC),
                         decided=[dict(d) for d in decided]))


@router.get("/{job_id}", response_class=HTMLResponse, summary="Leitura de um artigo")
def detail(job_id: int, request: Request, db: Session = Depends(get_db)):
    job = db.execute(text("SELECT * FROM publication_jobs WHERE id = :i"),
                     {"i": job_id}).mappings().first()
    if job is None:
        return HTMLResponse("<h1>404</h1><p>Job não encontrado.</p>", status_code=404)
    texts = {r["language"]: dict(r) for r in db.execute(
        text("SELECT language, title, excerpt, html, status FROM job_translations "
             "WHERE job_id = :i"), {"i": job_id}).mappings()}
    evidence = db.execute(
        text("SELECT source_name, source_url, source_published_at, claims_supported, "
             "usage_rights_note "
             "FROM article_evidence WHERE job_id = :i ORDER BY id"),
        {"i": job_id}).mappings().all()
    images = []
    for row in db.execute(
            text("SELECT local_path, provider, license, license_proof_url, author, credit, "
                 "ai_generated, alt_texts, captions, source_url FROM image_assets "
                 "WHERE job_id = :i ORDER BY id"), {"i": job_id}).mappings():
        image = dict(row)
        image["filename"] = os.path.basename(image["local_path"] or "")
        images.append(image)
    publication = db.execute(
        text("""SELECT event_type, timestamp_utc, details FROM publication_events
                 WHERE job_id = :i AND event_type IN ('drafts_created', 'draft_failed',
                                                      'site_suspended', 'publish_requested')
                 ORDER BY id DESC LIMIT 5"""), {"i": job_id}).mappings().all()
    return templates.TemplateResponse(
        request=request, name="review_job.html",
        context=_context(request, job=dict(job), texts=texts, languages=LANGUAGES,
                         labels=LANGUAGE_LABELS, evidence=[dict(e) for e in evidence],
                         images=images, report=job["review_report"] or {},
                         publication=[dict(e) for e in publication],
                         late_until=(_hold_until(job["due_at_utc"])
                                     if reserve.is_late(db, job_id) else ""),
                         publish_mode=settings.wp_publish_mode, now=datetime.now(UTC)))


def _decide(db: Session, job_id: int, request: Request, decision: str, note: str) -> str:
    """Grava a decisão (canal `panel`). Devolve '' se correu bem, ou a mensagem de erro."""
    error = decisions.decide(db, job_id, decision, user=_user(request), channel="panel", note=note)
    if not error:
        db.commit()
    return error


@router.post("/{job_id}/approve", summary="Aprova (vale para os 4 idiomas)")
def approve(job_id: int, request: Request, note: str = Form(""),
            db: Session = Depends(get_db)):
    error = _decide(db, job_id, request, "approved", note.strip()[:500])
    if error:
        return _back(err=error)
    due, kept = db.execute(text("SELECT due_at_utc, reserve FROM publication_jobs WHERE id = :i"),
                           {"i": job_id}).one()
    when = f" Publica sozinho em {when_local(due)}." if settings.wp_publish_mode == "live" else ""
    if kept:  # D-115: a reserva aprovada só sai quando cobre um slot
        when = " Fica guardado como reserva: publica no dia em que o post do blog não sair."
    elif reserve.is_late(db, job_id):
        when = (" O dia dele já passou: não sai junto com o post de hoje; publica no próximo "
                f"horário do blog que falhar, até {_hold_until(due)}.")
    return _back(msg=f"Job {job_id} aprovado.{when}")


@router.post("/{job_id}/reject", summary="Rejeita")
def reject(job_id: int, request: Request, note: str = Form(""),
           db: Session = Depends(get_db)):
    error = _decide(db, job_id, request, "rejected", note.strip()[:500])
    return _back(err=error) if error else _back(msg=f"Job {job_id} rejeitado.")


@router.post("/{job_id}/requeue", summary="Devolve um job BLOCKED à fila")
def requeue(job_id: int, request: Request, note: str = Form(""),
            db: Session = Depends(get_db)):
    """Único caminho do painel para BLOCKED→QUEUED. A produção recomeça do zero
    (pesquisa, gates, revisão e aprovação de novo); nenhum gate é encurtado (R41)."""
    try:
        jobs.requeue_blocked(db, job_id, _user(request), note)
    except jobs.MissingReason:
        return _back(f"/{job_id}", err="Diga o motivo para devolver o job à fila.")
    except jobs.JobNotFound:
        return _back(err=f"Job {job_id} não existe.")
    except jobs.InvalidTransition:
        return _back(f"/{job_id}", err=f"Job {job_id} já não está BLOCKED; nada foi mudado.")
    db.commit()
    return _back(f"/{job_id}",
                 msg=f"Job {job_id} devolvido à fila; o próximo dispatch volta a produzi-lo.")


def _enqueue_publish(job_id: int) -> None:
    """A publicação demora (mídias e 4 posts): corre no worker `fast`, não no pedido."""
    publishing.enqueue_publish(job_id)


def _request_publish(db: Session, job_id: int, user: str, channel: str,
                     early: bool = False) -> str:
    """Regista o pedido de publicação e põe-no na fila. Devolve '' ou a mensagem de erro.

    Em modo `live`, antes da hora do slot só com `early` (confirmação explícita no painel):
    um clique distraído em «Publicar» antecipava o post (jobs 41–47 e 49).
    """
    row = db.execute(text("SELECT status, due_at_utc, reserve FROM publication_jobs "
                          "WHERE id = :i"), {"i": job_id}).first()
    if row is None:
        return f"Job {job_id} não existe."
    status, due, kept = row
    if status != "APPROVED":
        return f"Só se publica um job APPROVED (o {job_id} está {status})."
    if kept:  # D-115: sem slot, publicar a reserva seria um post a mais no dia
        return (f"Job {job_id} é a reserva do blog: publica sozinho no dia em que o post "
                "do blog não sair.")
    if reserve.is_late(db, job_id):  # aprovado depois do dia dele (22/11, job 372)
        return (f"Job {job_id} é de um dia que já passou: publica sozinho no próximo horário "
                f"do blog que falhar, até {_hold_until(due)}.")
    before_slot = settings.wp_publish_mode == "live" and due > datetime.now(UTC)
    if before_slot and not early:
        return (f"Job {job_id} publica sozinho em {when_local(due)}; para antecipar, "
                "use «Publicar antes do horário» na página do artigo.")
    recent = db.execute(
        text("""SELECT 1 FROM publication_events
                 WHERE job_id = :i AND event_type = 'publish_requested'
                   AND timestamp_utc > now() - interval '2 minutes' LIMIT 1"""),
        {"i": job_id}).first()
    if recent:  # dois cliques seguidos criariam posts em duplicado (em modo draft)
        return f"Job {job_id}: já há um pedido recente (menos de 2 minutos)."
    jobs.record_event(db, job_id, "publish_requested", actor=user,
                      details={"mode": settings.wp_publish_mode, "channel": channel,
                               "early": before_slot})
    db.commit()
    _enqueue_publish(job_id)
    return ""


@router.post("/{job_id}/publish", summary="Publica no WordPress (rascunhos ou live)")
def publish(job_id: int, request: Request, early: str = Form(""),
            db: Session = Depends(get_db)):
    error = _request_publish(db, job_id, _user(request), "panel", early=early == "1")
    if error:
        return _back(f"/{job_id}", err=error)
    live = settings.wp_publish_mode == "live"
    what = "Publicação pedida" if live else "Criação dos rascunhos pedida"
    return _back(f"/{job_id}", msg=f"{what} para o job {job_id}. Atualize a página em instantes.")


BATCH_ACTIONS = ("approve", "reject", "publish")
BATCH_MAX = 50


@router.post("/batch", summary="Decide ou pede rascunhos de vários jobs de uma vez")
def batch(request: Request, action: str = Form(...), ids: list[int] = Form(default=[]),
          db: Session = Depends(get_db)):
    """Cada job leva a sua própria decisão (e linha em `approvals`): é um atalho de cliques.

    Os que já não estão no estado certo são saltados e listados, sem travar os outros.
    """
    if action not in BATCH_ACTIONS:
        return _back(err="Ação desconhecida.")
    chosen = list(dict.fromkeys(ids))
    if not chosen:
        return _back(err="Selecione pelo menos um artigo.")
    if len(chosen) > BATCH_MAX:
        return _back(err=f"No máximo {BATCH_MAX} artigos de cada vez.")
    done: list[int] = []
    skipped: list[str] = []
    for job_id in chosen:
        if action == "publish":
            error = _request_publish(db, job_id, _user(request), "panel-batch")
        else:
            error = _decide(db, job_id, request,
                            "approved" if action == "approve" else "rejected", "")
        if error:
            db.rollback()
            skipped.append(error)
        else:
            done.append(job_id)
    verb = {"approve": "aprovados", "reject": "rejeitados", "publish": "com pedido de rascunhos"}
    msg = f"{len(done)} {verb[action]}: " + ", ".join(f"#{j}" for j in done) if done else ""
    return _back(msg=msg, err=" · ".join(skipped)[:900])
