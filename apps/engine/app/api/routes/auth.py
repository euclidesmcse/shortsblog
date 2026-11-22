"""Login do portal (E18.1, D-104): usuários próprios com email e senha.

Sem nenhum usuário cadastrado, o `PANEL_PASSWORD` antigo entra uma única vez e cria o 1.º
admin (que troca a senha logo a seguir); depois disso só valem os usuários da tabela `users`.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core import panel_auth
from app.core.config import settings
from app.core.db import get_db
from app.core.logging import get_logger
from app.services import setup, users

log = get_logger(__name__)
router = APIRouter(tags=["auth"])
templates = Jinja2Templates(directory="app/dashboard/templates")


def _safe_next(target: str) -> str:
    """Só caminhos locais: impede redirecionar para outro site depois do login.

    Rejeita `//host`, `/\\host` (os browsers tratam `\\` como `/`), esquemas e
    caracteres de controlo.
    """
    parts = urlsplit(target)
    if (not target.startswith("/") or target.startswith(("//", "/\\"))
            or "\\" in target or parts.scheme or parts.netloc
            or any(ord(c) < 32 or ord(c) == 127 for c in target)):
        return "/"
    return target


def _brand() -> dict:
    return {"brand": settings.brand_name, "primary": settings.brand_primary_color,
            "accent": settings.brand_accent_color}


def _page(request: Request, db: Session, next_url: str, error: str = "", status: int = 200,
          email: str = "") -> Response:
    bootstrap = users.count(db) == 0
    return templates.TemplateResponse(
        request=request, name="login.html", status_code=status,
        context={**_brand(), "next": next_url, "error": error,
                 "access_email": request.state.access_email,
                 "email": email or request.state.access_email or "",
                 "bootstrap": bootstrap,
                 "enabled": not bootstrap or bool(settings.panel_password)},
    )


def _start_session(request: Request, response: Response, user: dict) -> None:
    response.set_cookie(
        panel_auth.SESSION_COOKIE,
        panel_auth.make_session(user["id"], user["session_version"]),
        max_age=settings.panel_session_hours * 3600, httponly=True,
        # E19.2: Secure só com o portal em https; no pacote em http (teste local) o navegador
        # descartaria o cookie e o login não se manteria. R69: também quando o pedido chegou em
        # https por outro nome (o proxy de confiança diz pelo X-Forwarded-Proto).
        secure=(settings.public_base_url.startswith("https://")
                and (panel_auth.is_public_request(request) or panel_auth.forwarded_https(request))),
        samesite="strict",
    )


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
def login_form(request: Request, next: str = "/", expired: int = 0,
               db: Session = Depends(get_db)) -> Response:
    notice = ("A sessão do painel expirou e a última ação NÃO foi aplicada. "
              "Entre de novo e refaça.") if expired else ""
    return _page(request, db, _safe_next(next), notice)


@router.post("/login", include_in_schema=False)
def login(request: Request, password: str = Form(...), email: str = Form(""),
          next: str = Form("/"), db: Session = Depends(get_db)) -> Response:
    ip = panel_auth.client_ip(request)
    # Pelo Access, o email é o que a Cloudflare verificou: não se entra com outro.
    access_email = request.state.access_email
    if access_email and email and email.strip().lower() != access_email:
        return _page(request, db, _safe_next(next), users.FAILED, status=401, email=email)
    login_email = email or access_email or ""
    try:
        if users.count(db) == 0:
            user = users.bootstrap_admin(db, login_email, password, ip)
        else:
            user = users.authenticate(db, login_email, password, ip)
        users.audit(db, user["email"], "user.login", user["email"])
        db.commit()
    except users.UserError as exc:
        db.commit()  # grava o contador de falhas e o bloqueio
        return _page(request, db, _safe_next(next), str(exc), status=401, email=login_email)
    target = "/conta/senha?forced=1" if user["must_change_password"] else _safe_next(next)
    response = RedirectResponse(target, status_code=303)
    _start_session(request, response, user)
    return response


@router.post("/logout", include_in_schema=False)
def logout(request: Request, db: Session = Depends(get_db)) -> Response:
    """Sai e REVOGA a sessão no servidor: a versão sobe, e um cookie copiado deixa de valer
    (revisão intermediária da E18, #3)."""
    session = panel_auth.read_session(request.cookies.get(panel_auth.SESSION_COOKIE))
    if session:
        user_id, version = session
        email = db.execute(text(
            "UPDATE users SET session_version = session_version + 1, updated_at = now() "
            "WHERE id = :i AND session_version = :v RETURNING email"),
            {"i": user_id, "v": version}).scalar()
        if email:
            users.audit(db, email, "user.logout", email)
        db.commit()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(panel_auth.SESSION_COOKIE)
    return response


# --- Minha senha ----------------------------------------------------------------
def _password_page(request: Request, error: str = "", msg: str = "",
                   status: int = 200) -> Response:
    response = templates.TemplateResponse(
        request=request, name="account_password.html", status_code=status,
        context={**_brand(), "user": request.state.panel_user, "error": error, "msg": msg,
                 "role": request.state.panel_role,
                 "forced": request.query_params.get("forced") == "1",
                 "min_length": users.MIN_PASSWORD},
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("/conta/senha", response_class=HTMLResponse, include_in_schema=False)
def password_form(request: Request, msg: str = "") -> Response:
    return _password_page(request, msg=msg)


@router.post("/conta/senha", include_in_schema=False)
def password_change(request: Request, current: str = Form(...), new: str = Form(...),
                    confirm: str = Form(...), db: Session = Depends(get_db)) -> Response:
    if new != confirm:
        return _password_page(request, error="A confirmação não confere.", status=400)
    try:
        version = users.change_password(db, request.state.panel_user_id, current, new)
        db.commit()
    except users.UserError as exc:
        db.rollback()
        return _password_page(request, error=str(exc), status=400)
    # As outras sessões caem (versão nova); esta continua com um cookie novo.
    target = "/conta/senha?msg=Senha+alterada."
    # E19.5: numa instalação nova, o admin segue direto para os primeiros passos.
    if request.state.panel_role == "admin" and setup.pending(db):
        target = "/dashboard/setup?msg=Senha+alterada.+Agora+os+primeiros+passos."
    response = RedirectResponse(target, status_code=303)
    _start_session(request, response, {"id": request.state.panel_user_id,
                                       "session_version": version})
    log.info("Painel: %s trocou a senha", request.state.panel_user)
    return response
