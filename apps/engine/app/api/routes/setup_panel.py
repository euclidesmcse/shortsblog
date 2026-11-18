"""E19.5: assistente de 1.º arranque — a tela Sistema em modo guiado (spec do portal, E18.3).

Uma página com os passos (administrador, blog, IA, e-mail, WordPress, ligar) e o estado REAL de
cada um (`services.setup`). Construído uma vez, usado nos dois lugares: os cartões de e-mail e de
WordPress são os mesmos da tela Sistema (macros em `_system_cards.html`, gravação pelas rotas de
`/dashboard/system`, que voltam para cá). Próprias daqui só três ações, que reaproveitam os
serviços das outras telas:

- IA: cadastra o provedor por chave de API (`providers.save_api_provider`), verifica na hora e,
  se passou, põe-no À FRENTE das 4 cadeias (num pacote novo as cadeias trazem as assinaturas, sem
  login);
- o nome e a descrição do blog (`editorial.edit_site`, a mesma edição da Configuração);
- ligar o blog — só com a IA e o WordPress prontos.

Só `admin` (`/dashboard/setup` está em `ADMIN_PREFIXES`). Nenhuma chave aparece em log, URL ou
auditoria.
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
from app.services import editorial, setup, users, vault
from app.services.ai import chain, health, providers

log = get_logger(__name__)
router = APIRouter(prefix="/dashboard/setup", tags=["dashboard"])
templates = Jinja2Templates(directory="app/dashboard/templates")

PATH = "/dashboard/setup"
HEALTH_LABELS = {"ok": "verificado", "not_authenticated": "chave recusada ou sem acesso",
                 "quota_exhausted": "sem cota/créditos", "error": "erro",
                 "unknown": "por verificar"}


def _back(msg: str = "", err: str = "", anchor: str = "") -> RedirectResponse:
    query = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    return RedirectResponse(f"{PATH}{'?' + query if query else ''}{anchor}", status_code=303)


def _actor(request: Request) -> str:
    return str(request.state.panel_user or "painel")[:120]


@router.get("", response_class=HTMLResponse, summary="Primeiros passos (assistente)")
def page(request: Request, msg: str = "", err: str = "", db: Session = Depends(get_db)):
    # Os dados dos cartões compartilhados vêm da própria tela Sistema.
    from app.api.routes import system_panel

    found = setup.steps(db, _actor(request))
    sites = found[1].detail.get("sites", [])
    blogs = []
    for site in sites:
        user_item, password_item = vault.wp_items(site["site_key"])
        blogs.append({**site, "user": vault.describe(user_item),
                      "password": vault.describe(password_item),
                      "version": vault.version(db, system_panel._wp_keys(site["site_key"]))})
    versions = {}
    for site in sites:
        try:
            versions[site["site_key"]] = editorial.version(
                editorial.site_values(db, site["site_key"]))
        except editorial.EditError:
            continue
    response = templates.TemplateResponse(
        request=request, name="setup.html",
        context={"brand": settings.brand_name, "primary": settings.brand_primary_color,
                 "accent": settings.brand_accent_color, "user": request.state.panel_user,
                 "msg": msg, "err": err, "steps": found,
                 "required_left": sum(1 for s in found if s.required and not s.done),
                 "can_enable": setup.can_enable(found),
                 "email": [vault.describe(i) for i in vault.EMAIL_ITEMS],
                 "email_version": vault.version(db, system_panel.EMAIL_KEYS),
                 "blogs": blogs, "site_versions": versions,
                 "providers": providers.list_providers(db), "presets": setup.PRESETS,
                 "health_labels": HEALTH_LABELS},
    )
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/ai", summary="Cadastra um provedor por chave de API, verifica e põe nas cadeias")
def ai_provider(request: Request, preset: str = Form("custom"), name: str = Form(""),
                family: str = Form(""), base_url: str = Form(""), model: str = Form(...),
                api_key: str = Form(""), db: Session = Depends(get_db)) -> Response:
    base = setup.PRESETS.get(preset, setup.PRESETS["custom"])
    name = (name.strip() or base["name"]).lower()
    family = family.strip() or base["family"]
    base_url = base_url.strip() or base["base_url"]
    try:
        providers.save_api_provider(db, name, family, base_url, model, api_key)
        users.audit(db, _actor(request), "ai.provider.save", f"ai:{name}",
                    {"model": model.strip(), "key_changed": bool(api_key.strip()),
                     "via": "assistente"})
        db.commit()
    except (providers.ProviderInvalid, chain.ChainInvalid) as exc:
        db.rollback()
        return _back(err=f"IA: {exc}", anchor="#ai")
    status = health.probe_all(only=name, force=True).get(name, "unknown")
    if status != health.OK:
        log.info("Assistente: %s cadastrou %s, verificação %s", _actor(request), name, status)
        return _back(err=f"IA: {name} foi guardado, mas a verificação falhou "
                         f"({HEALTH_LABELS.get(status, status)}). Confira a chave, o modelo e o "
                         "endereço e grave de novo (a chave em branco mantém a guardada).",
                     anchor="#ai")
    try:
        for role in chain.ROLES:
            names = [p.name for p in chain.load_chain(db, role)]
            chain.set_chain(db, role, [name] + [n for n in names if n != name])
        users.audit(db, _actor(request), "ai.chain.front", f"ai:{name}",
                    {"roles": list(chain.ROLES), "via": "assistente"})
        db.commit()
    except chain.ChainInvalid as exc:
        db.rollback()
        return _back(err=f"IA: {name} verificado, mas não entrou nas cadeias: {exc}", anchor="#ai")
    log.info("Assistente: %s pôs %s à frente das cadeias", _actor(request), name)
    state = setup.ai_state(db)
    tail = ("" if state["ok"] else
            " Falta um provedor de OUTRA família para revisar o que este escreve.")
    return _back(msg=f"IA: {name} verificado e posto à frente das cadeias.{tail}", anchor="#ai")


@router.post("/site/{site_key}", summary="Nome e descrição do blog")
def site_name(site_key: str, request: Request, name: str = Form(...),
              description: str = Form(""), version: str = Form(""),
              db: Session = Depends(get_db)) -> Response:
    try:
        fields = editorial.edit_site(
            db, site_key, {"name": name.strip(), "description": description.strip()},
            _actor(request), expected=version)
        db.commit()
    except editorial.EditError as exc:
        db.rollback()
        return _back(err=f"Blog: {exc}", anchor="#blog")
    if not fields:
        return _back(msg="Blog: nada mudou.", anchor="#blog")
    return _back(msg="Blog: " + ", ".join(fields) + " atualizado(s). O nome no próprio "
                 "WordPress muda em Configurações → Geral do blog.", anchor="#blog")


@router.post("/site/{site_key}/enable", summary="Liga o blog (com IA e WordPress prontos)")
def enable(site_key: str, request: Request, version: str = Form(""),
           db: Session = Depends(get_db)) -> Response:
    ok, why = setup.can_enable(setup.steps(db, _actor(request)))
    if not ok:
        return _back(err=why, anchor="#enable")
    try:
        fields = editorial.edit_site(db, site_key, {"enabled": True}, _actor(request),
                                     expected=version)
        db.commit()
    except editorial.EditError as exc:
        db.rollback()
        return _back(err=f"Blog: {exc}", anchor="#enable")
    if not fields:
        return _back(msg="O blog já estava ligado.", anchor="#enable")
    log.info("Assistente: %s ligou o blog %s", _actor(request), site_key)
    return _back(msg="Blog ligado. O próximo planejamento cria os slots; as pautas chegam para "
                     "aprovação em Aprovações.", anchor="#enable")
