"""E18.1d (D-104): gestão de usuários do portal — só `admin` (o `panel_guard` barra o resto).

Senhas provisórias aparecem UMA vez, na resposta do próprio POST (nunca em URL, log ou
auditoria); o usuário é obrigado a trocá-las no 1.º acesso.
"""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.logging import get_logger
from app.services import users

log = get_logger(__name__)
router = APIRouter(prefix="/dashboard/users", tags=["dashboard"])
templates = Jinja2Templates(directory="app/dashboard/templates")

ROLE_LABELS = {"admin": "Admin", "operador": "Operador"}


def _actor(request: Request) -> str:
    return str(request.state.panel_user or "painel")[:120]


def _page(request: Request, db: Session, *, msg: str = "", err: str = "",
          revealed: dict | None = None, status: int = 200) -> Response:
    allowed = settings.allowed_panel_emails
    response = templates.TemplateResponse(
        request=request, name="users.html", status_code=status,
        context={"brand": settings.brand_name, "primary": settings.brand_primary_color,
                 "accent": settings.brand_accent_color, "user": request.state.panel_user,
                 "me": request.state.panel_user_id, "users": users.list_users(db),
                 "roles": ROLE_LABELS, "msg": msg, "err": err, "revealed": revealed,
                 "audit": users.recent_audit(db, 30),
                 # Na nossa instalação o Access só deixa passar os emails da lista.
                 "access_list": sorted(allowed) if settings.panel_require_access else [],
                 "min_length": users.MIN_PASSWORD},
    )
    # A senha provisória aparece nesta resposta: nada de cache (revisão intermediária #6).
    response.headers["Cache-Control"] = "no-store"
    return response


def _back(msg: str = "", err: str = "") -> RedirectResponse:
    query = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    return RedirectResponse(f"/dashboard/users{'?' + query if query else ''}", status_code=303)


@router.get("", response_class=HTMLResponse, summary="Usuários do portal")
def page(request: Request, msg: str = "", err: str = "", db: Session = Depends(get_db)):
    return _page(request, db, msg=msg, err=err)


@router.post("", summary="Cria um usuário com senha provisória")
def create(request: Request, email: str = Form(...), name: str = Form(""),
           role: str = Form("operador"), db: Session = Depends(get_db)) -> Response:
    password = users.temporary_password(email.strip().lower())
    try:
        users.create(db, email=email, name=name, role=role, password=password,
                     actor=_actor(request), must_change=True)
        db.commit()
    except users.UserError as exc:
        db.rollback()
        return _back(err=str(exc))
    log.info("Painel: usuário criado por %s (papel %s)", _actor(request), role)
    return _page(request, db, msg="Usuário criado.",
                 revealed={"email": users.normalize_email(email), "password": password})


@router.post("/{user_id}/reset", summary="Gera uma senha provisória nova")
def reset(user_id: int, request: Request, db: Session = Depends(get_db)) -> Response:
    try:
        target = users.get(db, user_id)
        password = users.reset_password(db, user_id, _actor(request))
        db.commit()
    except users.UserError as exc:
        db.rollback()
        return _back(err=str(exc))
    return _page(request, db, msg="Senha redefinida; as sessões desse usuário foram encerradas.",
                 revealed={"email": target["email"] if target else "", "password": password})


@router.post("/{user_id}/active", summary="Ativa ou desativa um usuário")
def active(user_id: int, request: Request, active: bool = Form(...),
           db: Session = Depends(get_db)) -> Response:
    if user_id == request.state.panel_user_id and not active:
        return _back(err="Você não pode desativar a sua própria conta.")
    try:
        users.set_active(db, user_id, active, _actor(request))
        db.commit()
    except users.UserError as exc:
        db.rollback()
        return _back(err=str(exc))
    return _back(msg="Usuário ativado." if active else "Usuário desativado e sessões encerradas.")


@router.post("/{user_id}/role", summary="Muda o papel de um usuário")
def role(user_id: int, request: Request, role: str = Form(...),
         db: Session = Depends(get_db)) -> Response:
    if user_id == request.state.panel_user_id and role != "admin":
        return _back(err="Você não pode tirar o seu próprio papel de admin.")
    try:
        users.set_role(db, user_id, role, _actor(request))
        db.commit()
    except users.UserError as exc:
        db.rollback()
        return _back(err=str(exc))
    return _back(msg="Papel atualizado; as sessões desse usuário foram encerradas.")
