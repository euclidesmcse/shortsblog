"""E18.3b (D-104, D-109): Sistema — a «engrenagem» do admin.

Entrega 1: e-mail e WordPress. Entrega 2: tokens de webhook (o portal gera o novo, mostra UMA vez e
o anterior vale por mais 24 h, com a lista de onde atualizar) e o resumo das chaves de IA (editadas
em Provedores de IA). Entrega 3: limites (chamadas de IA por job, buscas na web) e horários-base dos
slots, mais o resumo da cota diária de cada provedor de IA (editada em Provedores de IA).

Só `admin` (o `panel_guard` barra o resto, R68). Os valores vão cifrados para o cofre
(`services.vault`), que tem precedência sobre o ambiente; os segredos aparecem só mascarados
(últimos 4) e o campo de segredo em branco mantém o atual. Gravar testa a conexão antes: se o teste
falha, nada é gravado, salvo «gravar mesmo assim». A auditoria nunca leva o valor de um segredo.
"""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.logging import get_logger
from app.services import integrations, vault

log = get_logger(__name__)
router = APIRouter(prefix="/dashboard/system", tags=["dashboard"])
templates = Jinja2Templates(directory="app/dashboard/templates")

PATH = "/dashboard/system"
STALE = "A configuração mudou desde que a tela foi aberta: recarregue e refaça."
EMAIL_KEYS = [i.key for i in vault.EMAIL_ITEMS]
TOKEN_KEYS = [i.key for i in vault.TOKEN_ITEMS]
LIMIT_KEYS = [i.key for i in vault.LIMIT_ITEMS]


# E19.5: os cartões de e-mail e de WordPress também aparecem no assistente de 1.º arranque; com
# `back=setup` no formulário, a resposta volta para lá (só esse destino: nada de URL livre).
BACK_PATHS = {"setup": "/dashboard/setup"}


def _back(msg: str = "", err: str = "", anchor: str = "", to: str = PATH) -> RedirectResponse:
    query = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    return RedirectResponse(f"{to}{'?' + query if query else ''}{anchor}", status_code=303)


def _dest(form: dict[str, str]) -> str:
    return BACK_PATHS.get(form.get("back", ""), PATH)


def _actor(request: Request) -> str:
    return str(request.state.panel_user or "painel")[:120]


def _sites(db: Session) -> list[dict]:
    return [dict(r) for r in db.execute(text(
        "SELECT site_key, name, base_url, wordpress_api_url, enabled FROM sites "
        "ORDER BY site_key")).mappings()]


def _site(db: Session, site_key: str) -> dict | None:
    return next((s for s in _sites(db) if s["site_key"] == site_key), None)


def _wp_keys(site_key: str) -> list[str]:
    return [i.key for i in vault.wp_items(site_key)]


def _ai_keys(db: Session) -> list[dict]:
    """Resumo das chaves de IA (a edição fica em Provedores de IA): nunca o valor."""
    from app.services.ai import quota
    from app.services.ai import secrets as ai_secrets

    rows = db.execute(text("SELECT name, kind, family, model, enabled, api_key_enc, "
                           "daily_call_limit FROM ai_providers ORDER BY id")).mappings().all()
    out = []
    for r in rows:
        if r["kind"] != "api":
            key = "assinatura (login da CLI no gateway, sem chave)"
        elif not r["api_key_enc"]:
            key = "faltando"
        else:
            try:
                key = vault.mask(ai_secrets.decrypt(r["api_key_enc"])[-4:])
            except ai_secrets.SecretUnreadable:
                key = "ilegível (SB_SECRET_KEY mudou?)"
        out.append({"name": r["name"], "kind": r["kind"], "family": r["family"],
                    "model": r["model"], "enabled": r["enabled"], "key": key,
                    "daily_call_limit": r["daily_call_limit"],
                    "calls_today": quota.provider_usage(db, r["name"]).calls_today})
    return out


def _planned_until(db: Session):
    """Último dia com slots já criados: mudar os horários só vale depois dele."""
    return db.execute(text("SELECT max(local_date) FROM publication_jobs")).scalar()


def _render(request: Request, db: Session, *, msg: str = "", err: str = "",
            revealed: dict | None = None) -> Response:
    blogs = []
    for site in _sites(db):
        user_item, password_item = vault.wp_items(site["site_key"])
        blogs.append({**site, "user": vault.describe(user_item),
                      "password": vault.describe(password_item),
                      "version": vault.version(db, _wp_keys(site["site_key"]))})
    response = templates.TemplateResponse(
        request=request, name="system.html",
        context={"brand": settings.brand_name, "primary": settings.brand_primary_color,
                 "accent": settings.brand_accent_color, "user": request.state.panel_user,
                 "msg": msg, "err": err,
                 "email": [vault.describe(i) for i in vault.EMAIL_ITEMS],
                 "email_version": vault.version(db, EMAIL_KEYS), "blogs": blogs,
                 "tokens": [vault.describe(i) for i in vault.TOKEN_ITEMS],
                 "token_version": vault.version(db, TOKEN_KEYS),
                 "grace_hours": vault.GRACE_HOURS, "ai_keys": _ai_keys(db),
                 "limits": [vault.describe(i) for i in vault.LIMIT_ITEMS],
                 "limits_version": vault.version(db, LIMIT_KEYS),
                 "planned_until": _planned_until(db),
                 "revealed": revealed},
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.get("", response_class=HTMLResponse, summary="Sistema (engrenagem)")
def page(request: Request, msg: str = "", err: str = "", db: Session = Depends(get_db)):
    return _render(request, db, msg=msg, err=err)


async def _form(request: Request) -> dict[str, str]:
    form = await request.form()
    return {k: str(v) for k, v in form.items()}


def _changes(form: dict[str, str], items: list[vault.Item]) -> dict[str, str]:
    """O que o formulário muda: segredo em branco mantém; parâmetro igual ao atual não conta."""
    changed = {}
    for it in items:
        value = form.get(it.key, "").strip()
        if value and value != vault.get(it.key):
            changed[it.key] = value
    return changed


def _save(db: Session, request: Request, changed: dict[str, str]) -> list[str]:
    saved = [k for k, v in changed.items() if vault.set_value(db, k, v, _actor(request))]
    db.commit()
    vault.invalidate()
    return saved


SMTP_DESTINATION = ("smtp_host", "smtp_port", "smtp_user")


def _smtp_secret_to_retype(form: dict[str, str], changed: dict[str, str],
                           candidate: dict[str, str]) -> str:
    """Rótulo do segredo SMTP a redigitar, ou "" se não precisa.

    Trocar o destino (host, porta ou usuário) com o segredo em branco faria o teste mandar o
    segredo guardado para um servidor digitado no formulário — um jeito de tirá-lo do cofre que a
    tela só mostra mascarado (revisão final da E18, achado 4). Só vale para o caminho SMTP: com a
    chave da API do Brevo o teste vai sempre ao endereço fixo do Brevo.
    """
    if not any(k in changed for k in SMTP_DESTINATION):
        return ""
    key = candidate.get("brevo_api_key", "")
    if key and not key.startswith(vault.SMTP_KEY_PREFIX):
        return ""
    secret = "smtp_password" if candidate.get("smtp_password") else (
        "brevo_api_key" if key else "")
    if not secret or form.get(secret, "").strip():
        return ""
    return vault.item(secret).label


@router.post("/email", summary="Testa ou grava o e-mail (Brevo/SMTP e remetente)")
async def email(request: Request, db: Session = Depends(get_db)) -> Response:
    form = await _form(request)
    to = _dest(form)
    items = list(vault.EMAIL_ITEMS)
    try:
        changed = _changes(form, items)
        for key, value in changed.items():
            changed[key] = vault.validate(vault.item(key), value)
    except vault.VaultError as exc:
        return _back(err=str(exc), anchor="#email", to=to)
    candidate = {k: changed.get(k, vault.get(k)) for k in EMAIL_KEYS}
    retype = _smtp_secret_to_retype(form, changed, candidate)
    if retype:
        return _back(err=f"Mudou o servidor, a porta ou o usuário SMTP: digite de novo {retype} "
                         "(o guardado no cofre não vai para outro servidor).",
                     anchor="#email", to=to)
    check = integrations.check_email(candidate)
    if form.get("action") != "save":
        return (_back(msg=f"Teste: {check.message}", anchor="#email", to=to) if check.ok
                else _back(err=f"Teste: {check.message}", anchor="#email", to=to))
    if form.get("version") != vault.version(db, EMAIL_KEYS):
        return _back(err=STALE, anchor="#email", to=to)
    if not changed:
        return _back(msg="Nada mudou.", anchor="#email", to=to)
    if not check.ok and form.get("force") != "1":
        return _back(err=f"Nada gravado — o teste falhou: {check.message} "
                         "(marque «gravar mesmo assim» se for intencional).",
                     anchor="#email", to=to)
    try:
        saved = _save(db, request, changed)
    except vault.VaultError as exc:
        db.rollback()
        return _back(err=str(exc), anchor="#email", to=to)
    log.info("Sistema: %s gravou %s", _actor(request), ", ".join(saved))
    note = "" if check.ok else " (o teste tinha falhado)"
    return _back(msg=f"Gravado no cofre: {', '.join(saved) or 'nada'}{note}.",
                 anchor="#email", to=to)


@router.post("/wp/{site_key}", summary="Testa ou grava a credencial do WordPress de um blog")
async def wordpress(site_key: str, request: Request, db: Session = Depends(get_db)) -> Response:
    form = await _form(request)
    to = _dest(form)
    anchor = f"#wp-{site_key}"
    site = _site(db, site_key)
    if not site:
        return _back(err=f"Blog desconhecido: {site_key}", to=to)
    items = list(vault.wp_items(site_key))
    renamed = {it.key: form.get(field, "") for it, field in zip(items, ("user", "password"),
                                                               strict=True)}
    try:
        changed = _changes(renamed, items)
        for key, value in changed.items():
            changed[key] = vault.validate(vault.item(key), value)
    except vault.VaultError as exc:
        return _back(err=str(exc), anchor=anchor, to=to)
    user_key, password_key = _wp_keys(site_key)
    check = integrations.check_wordpress(site, changed.get(user_key, vault.get(user_key)),
                                         changed.get(password_key, vault.get(password_key)))
    label = f"{site['name']}: "
    if form.get("action") != "save":
        return (_back(msg=f"Teste — {label}{check.message}", anchor=anchor, to=to) if check.ok
                else _back(err=f"Teste — {label}{check.message}", anchor=anchor, to=to))
    if form.get("version") != vault.version(db, _wp_keys(site_key)):
        return _back(err=STALE, anchor=anchor, to=to)
    if not changed:
        return _back(msg=f"{label}nada mudou.", anchor=anchor, to=to)
    if not check.ok and form.get("force") != "1":
        return _back(err=f"{label}nada gravado — o teste falhou: {check.message}",
                     anchor=anchor, to=to)
    try:
        saved = _save(db, request, changed)
    except vault.VaultError as exc:
        db.rollback()
        return _back(err=str(exc), anchor=anchor, to=to)
    log.info("Sistema: %s gravou %s", _actor(request), ", ".join(saved))
    return _back(msg=f"{label}gravado no cofre ({', '.join(saved)}).", anchor=anchor, to=to)


@router.post("/clear", summary="Tira um item do cofre (volta a valer o ambiente)")
def clear(request: Request, key: str = Form(...), version: str = Form(""), back: str = Form(""),
          db: Session = Depends(get_db)) -> Response:
    to = BACK_PATHS.get(back, PATH)
    try:
        it = vault.item(key)
    except vault.VaultError as exc:
        return _back(err=str(exc), to=to)
    if it.group == "email":
        group, anchor = EMAIL_KEYS, "#email"
    elif it.group == "token":
        group, anchor = TOKEN_KEYS, "#tokens"
    elif it.group == "limit":
        group, anchor = LIMIT_KEYS, "#limits"
    else:
        group, anchor = _wp_keys(key.split(".")[1]), f"#wp-{key.split('.')[1]}"
    if version != vault.version(db, group):
        return _back(err=STALE, anchor=anchor, to=to)
    if not vault.clear(db, key, _actor(request)):
        return _back(msg=f"{it.label}: não estava no cofre.", anchor=anchor, to=to)
    db.commit()
    vault.invalidate()
    fallback = "vale o ambiente" if vault.env_value(key) else "fica SEM valor (nem no ambiente)"
    return _back(msg=f"{it.label} ({key}) saiu do cofre: agora {fallback}.", anchor=anchor, to=to)


# --- Tokens (entrega 2) ---------------------------------------------------------------
def _token_item(key: str) -> vault.Item | None:
    try:
        it = vault.item(key)
    except vault.VaultError:
        return None
    return it if it.group == "token" else None


@router.post("/token/{key}/rotate", summary="Gera um token novo (o anterior vale por 24 h)")
def rotate_token(key: str, request: Request, version: str = Form(""), confirm: str = Form(""),
                 db: Session = Depends(get_db)) -> Response:
    it = _token_item(key)
    if not it:
        return _back(err=f"Token desconhecido: {key}")
    if version != vault.version(db, TOKEN_KEYS):
        return _back(err=STALE, anchor="#tokens")
    if confirm != "1":
        return _back(err=f"{it.label}: marque a confirmação para gerar o token novo.",
                     anchor="#tokens")
    token = vault.rotate_token(db, key, _actor(request))
    db.commit()
    vault.invalidate()
    log.info("Sistema: %s trocou o token %s", _actor(request), key)
    # O token aparece SÓ nesta resposta (no-store), nunca em URL, log ou auditoria.
    return _render(request, db, msg=f"{it.label}: token novo gerado.",
                   revealed={"key": key, "label": it.label, "token": token, "where": it.where})


@router.post("/token/{key}/end-grace", summary="Encerra a carência: o token anterior para já")
def end_grace(key: str, request: Request, version: str = Form(""),
              db: Session = Depends(get_db)) -> Response:
    it = _token_item(key)
    if not it:
        return _back(err=f"Token desconhecido: {key}")
    if version != vault.version(db, TOKEN_KEYS):
        return _back(err=STALE, anchor="#tokens")
    if not vault.end_grace(db, key, _actor(request)):
        return _back(msg=f"{it.label}: não havia carência em curso.", anchor="#tokens")
    db.commit()
    vault.invalidate()
    return _back(msg=f"{it.label}: o token anterior deixou de valer.", anchor="#tokens")


# --- Limites e horários (entrega 3) ----------------------------------------------------------
@router.post("/limits", summary="Grava limites (IA por job, buscas) e horários dos slots")
async def limits(request: Request, db: Session = Depends(get_db)) -> Response:
    form = await _form(request)
    if form.get("version") != vault.version(db, LIMIT_KEYS):
        return _back(err=STALE, anchor="#limits")
    changed: dict[str, str] = {}
    try:
        for it in vault.LIMIT_ITEMS:
            raw = form.get(it.key, "").strip()
            if not raw:
                continue
            value = vault.validate(it, raw)
            if value != vault.get(it.key):
                changed[it.key] = value
        vault.check_limits({k: changed.get(k, vault.get(k)) for k in LIMIT_KEYS})
    except vault.VaultError as exc:
        return _back(err=str(exc), anchor="#limits")
    if not changed:
        return _back(msg="Nada mudou.", anchor="#limits")
    try:
        saved = _save(db, request, changed)
    except vault.VaultError as exc:
        db.rollback()
        return _back(err=str(exc), anchor="#limits")
    log.info("Sistema: %s gravou %s", _actor(request), ", ".join(saved))
    note = ""
    if "slot_times" in saved:
        until = _planned_until(db)
        note = (f" Os horários novos valem para os dias ainda não planejados (depois de "
                f"{until:%d/%m}); os slots já criados ficam como estão." if until else
                " Os horários novos valem a partir do próximo planejamento.")
    return _back(msg=f"Gravado no cofre: {', '.join(saved)}.{note}", anchor="#limits")
