"""Página de confirmação dos links de decisão enviados por e-mail (E10.3).

`GET /a/<token>` só mostra (não decide: os scanners de e-mail seguem links GET); `POST /a/<token>`
decide. Pelo domínio público passa só quem tem o Cloudflare Access (o painel exige-o a tudo);
a decisão fica com o
e-mail do Access e o canal `email`. Sem Access (pedido pela rede interna) a decisão é recusada.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.services import approval_links, decisions

router = APIRouter(prefix="/a", tags=["dashboard"])
templates = Jinja2Templates(directory="app/dashboard/templates")
DECISION = {"approve": "approved", "reject": "rejected"}


def _page(
    request: Request, name: str = "link_confirm.html", status: int = 200, **ctx: object
) -> HTMLResponse:
    context = {
        "brand": settings.brand_name,
        "primary": settings.brand_primary_color,
        "accent": settings.brand_accent_color,
        **ctx,
    }
    response = templates.TemplateResponse(
        request=request, name=name, context=context, status_code=status
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"  # o token está no endereço
    return response


def _invalid(request: Request) -> HTMLResponse:
    return _page(request, "link_confirm.html", 404, state="invalid")


def _summary(db: Session, job_id: int) -> dict:
    row = (
        db.execute(
            text("""SELECT j.id, j.site_key, j.status, j.review_report,
                       (SELECT title FROM job_translations t
                         WHERE t.job_id = j.id AND t.language = 'pt-BR') AS title
                  FROM publication_jobs j WHERE j.id = :i"""),
            {"i": job_id},
        )
        .mappings()
        .first()
    )
    return dict(row) if row else {}


@router.get("/{token}", response_class=HTMLResponse, summary="Confirmar uma decisão (não decide)")
def show(token: str, request: Request, db: Session = Depends(get_db)):
    try:
        link = approval_links.lookup(db, token)
    except approval_links.InvalidLink:
        return _invalid(request)
    job = _summary(db, link.job_id)
    if job.get("status") != "AWAITING_APPROVAL":
        return _page(request, state="decided", job=job)
    return _page(request, state="ready", job=job, action=link.action, token=token)


@router.post("/{token}", response_class=HTMLResponse, summary="Decidir")
def decide(token: str, request: Request, note: str = Form(""), db: Session = Depends(get_db)):
    user = getattr(request.state, "access_email", None)
    if not user:
        return _page(request, state="denied", status=403)
    try:
        link = approval_links.lookup(db, token)
    except approval_links.InvalidLink:
        return _invalid(request)
    if not approval_links.consume(db, link, user):
        db.rollback()
        return _invalid(request)  # alguém se adiantou: uso único
    error = decisions.decide(
        db, link.job_id, DECISION[link.action], user=user, channel="email", note=note.strip()[:500]
    )
    if error:
        db.rollback()
        return _page(request, state="decided", job=_summary(db, link.job_id))
    db.commit()
    return _page(request, state="done", job=_summary(db, link.job_id), action=link.action)
