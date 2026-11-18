"""E18.3a (D-104/D-106/D-107): Configuração — reimportar do YAML, blogs, políticas, feeds,
newsletters de pauta, sem-leitura, histórico com desfazer e exportações.

Só `admin` (o `panel_guard` barra o resto). A tela mostra o diff YAML → banco; o botão aplica
exatamente o plano mostrado (`fingerprint`): se o YAML ou o banco mudaram entretanto, pede para
rever de novo. Cada mudança vai para o `audit_log` (`config.import`) com antes/depois.
"""
from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.logging import get_logger
from app.services import discovery, editorial, feedcheck, pauta, sites, terms
from app.services.sites import LANGUAGES

log = get_logger(__name__)
router = APIRouter(prefix="/dashboard/config", tags=["dashboard"])
templates = Jinja2Templates(directory="app/dashboard/templates")

KIND_LABELS = {"add": "adicionar", "update": "alterar", "disable": "desligar"}
ENTITY_LABELS = {"site": "Blog", "source": "Feed", "newsletters": "Newsletters de pauta",
                 "policy_common": "Regras editoriais comuns", "no_fetch": "Sites sem leitura"}
ACTION_LABELS = {editorial.IMPORT_ACTION: "importação", editorial.EDIT_ACTION: "edição no portal",
                 editorial.UNDO_ACTION: "desfazer"}
_CONFIG_ERRORS = (sites.SiteConfigError, discovery.SourceConfigError, pauta.NewsletterConfigError,
                  terms.TermsConfigError)


def _back(msg: str = "", err: str = "", path: str = "/dashboard/config") -> RedirectResponse:
    query = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    return RedirectResponse(f"{path}{'?' + query if query else ''}", status_code=303)


def _ctx(request: Request, **extra) -> dict:
    return {"brand": settings.brand_name, "primary": settings.brand_primary_color,
            "accent": settings.brand_accent_color, "user": request.state.panel_user, **extra}


def _actor(request: Request) -> str:
    return str(request.state.panel_user or "painel")[:120]


@router.get("", response_class=HTMLResponse, summary="Configuração editorial")
def page(request: Request, msg: str = "", err: str = "", db: Session = Depends(get_db)):
    changes: list[editorial.Change] = []
    problem = ""
    if not editorial.config_root_ok():
        problem = ("O content-config não está montado neste container: a reimportação só "
                   "funciona pela linha de comando (make reimport).")
    else:
        try:
            changes = editorial.plan(db)
        except _CONFIG_ERRORS as exc:
            problem = f"O YAML de content-config/ está inválido: {exc}"
    blogs = db.execute(text("SELECT site_key, name, enabled, daily_target FROM sites "
                            "ORDER BY slot_offset_minutes, site_key")).mappings().all()
    return templates.TemplateResponse(
        request=request, name="config.html",
        context=_ctx(request, msg=msg, err=err, problem=problem, changes=changes,
                     fingerprint=editorial.fingerprint(changes) if changes else "",
                     edited=sum(1 for c in changes if c.edited_in_portal),
                     kinds=KIND_LABELS, entities=ENTITY_LABELS, actions=ACTION_LABELS,
                     history=editorial.history(db), blogs=[dict(b) for b in blogs]),
    )


@router.post("/import", summary="Reimporta o YAML para o banco")
def reimport(request: Request, fingerprint: str = Form(...), db: Session = Depends(get_db)):
    actor = str(request.state.panel_user or "painel")
    try:
        changes = editorial.plan(db)
    except _CONFIG_ERRORS as exc:
        return _back(err=f"YAML inválido, nada foi aplicado: {exc}")
    if not changes:
        return _back(msg="Nada a reimportar: o banco já está igual ao YAML.")
    if editorial.fingerprint(changes) != fingerprint:
        return _back(err="O YAML ou o banco mudaram desde que a tela foi aberta: reveja as "
                         "mudanças abaixo e confirme de novo.")
    try:
        count = editorial.apply(db, changes, actor, portal=True)
    except editorial.EditError as exc:
        db.rollback()
        return _back(err=f"Nada foi aplicado: {exc}.")
    db.commit()
    log.info("Configuração: %d mudança(s) reimportadas do YAML por %s", count, actor)
    return _back(msg=f"{count} mudança(s) reimportada(s) do YAML; ficaram na auditoria.")


# --- E18.3a entrega 2: editar sites e políticas, desfazer, exportar ------------------------------
def _lines(raw: str) -> list[str]:
    seen: list[str] = []
    for item in raw.replace("\r", "").split("\n"):
        item = item.strip()
        if item and item not in seen:
            seen.append(item)
    return seen


def _int(raw: str, name: str) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise editorial.EditError(f"{name}: número inválido") from None


@router.get("/sites/{site_key}", response_class=HTMLResponse, summary="Editar um blog")
def site_page(site_key: str, request: Request, msg: str = "", err: str = "",
              db: Session = Depends(get_db)):
    try:
        values = editorial.site_values(db, site_key)
    except editorial.EditError as exc:
        return _back(err=str(exc))
    policy = values["config"].get("search_policy") or {}
    return templates.TemplateResponse(
        request=request, name="config_site.html",
        context=_ctx(request, msg=msg, err=err, site=values, cfg=values["config"],
                     search=policy, languages=LANGUAGES, readonly=editorial.SITE_READONLY,
                     version=editorial.version(values)),
    )


@router.post("/sites/{site_key}", summary="Grava a edição de um blog")
async def site_save(site_key: str, request: Request, db: Session = Depends(get_db)):
    data = await request.form()
    path = f"/dashboard/config/sites/{site_key}"
    try:
        domains = _lines(str(data.get("allow_domains", "")))
        official = data.get("official_only") == "true"
        form = {
            "name": str(data.get("name", "")).strip(),
            "enabled": data.get("enabled") == "true",
            "daily_target": _int(str(data.get("daily_target")), "posts por dia"),
            "slot_offset_minutes": _int(str(data.get("slot_offset_minutes")), "deslocamento"),
            "languages": [str(v) for v in data.getlist("languages")],
            "description": str(data.get("description", "")).strip(),
            "min_sources": _int(str(data.get("min_sources")), "fontes mínimas"),
            "categories": _lines(str(data.get("categories", ""))),
            "recipe_mode": data.get("recipe_mode") == "true" or None,
            "search_policy": ({"official_only": official, "allow_domains": domains}
                              if official or domains else None),
        }
        fields = editorial.edit_site(db, site_key, form, _actor(request),
                                     expected=str(data.get("version", "")))
        db.commit()
    except editorial.EditError as exc:
        db.rollback()
        return _back(err=str(exc), path=path)
    if not fields:
        return _back(msg="Nada mudou.", path=path)
    log.info("Configuração: %s editou o blog %s (%s)", _actor(request), site_key,
             ", ".join(fields))
    return _back(msg="Blog atualizado: " + ", ".join(fields) + ". Vale a partir do próximo "
                 "planejamento/produção.", path=path)


@router.get("/policies", response_class=HTMLResponse, summary="Políticas editoriais")
def policies_page(request: Request, msg: str = "", err: str = "", db: Session = Depends(get_db)):
    try:
        common, owns = editorial.policies(db)
    except editorial.EditError as exc:
        return _back(err=str(exc))
    names: dict[str, str] = {k: n for k, n in db.execute(text("SELECT site_key, name FROM sites"))}
    return templates.TemplateResponse(
        request=request, name="config_policies.html",
        context=_ctx(request, msg=msg, err=err, common=common, owns=owns, names=names,
                     max_len=editorial.POLICY_MAX, common_version=editorial.version(common),
                     own_versions={k: editorial.version(v) for k, v in owns.items()}),
    )


@router.post("/policies/common", summary="Grava as regras comuns")
def policy_common_save(request: Request, text_value: str = Form(..., alias="text"),
                       version: str = Form(""), db: Session = Depends(get_db)):
    path = "/dashboard/config/policies"
    try:
        touched = editorial.edit_policy_common(db, text_value, _actor(request), version)
        db.commit()
    except editorial.EditError as exc:
        db.rollback()
        return _back(err=str(exc), path=path)
    if not touched:
        return _back(msg="Nada mudou.", path=path)
    return _back(msg=f"Regras comuns atualizadas; política remontada em {len(touched)} blog(s).",
                 path=path)


@router.post("/policies/{site_key}", summary="Grava as regras de um blog")
def policy_own_save(site_key: str, request: Request, text_value: str = Form(..., alias="text"),
                    version: str = Form(""), db: Session = Depends(get_db)):
    path = "/dashboard/config/policies"
    try:
        fields = editorial.edit_policy_own(db, site_key, text_value, _actor(request), version)
        db.commit()
    except editorial.EditError as exc:
        db.rollback()
        return _back(err=str(exc), path=path)
    return _back(msg=f"Regras do blog {site_key} atualizadas." if fields else "Nada mudou.",
                 path=path)


@router.post("/history/{audit_id}/undo", summary="Desfaz uma importação ou edição")
def undo(audit_id: int, request: Request, db: Session = Depends(get_db)):
    try:
        target = editorial.undo(db, audit_id, _actor(request))
        db.commit()
    except editorial.EditError as exc:
        db.rollback()
        return _back(err=f"Não foi possível desfazer: {exc}")
    return _back(msg=f"Desfeito: {target} voltou ao valor anterior (registrado na auditoria).")


@router.get("/export/sites/{site_key}.yaml", response_class=PlainTextResponse,
            summary="Exporta um blog para YAML")
def export_site(site_key: str, db: Session = Depends(get_db)):
    try:
        body = editorial.export_site_yaml(db, site_key)
    except editorial.EditError as exc:
        return PlainTextResponse(str(exc), status_code=404)
    return PlainTextResponse(body, headers={
        "Content-Disposition": f'attachment; filename="{site_key}.yaml"'})


@router.get("/export/policies/{name}.md", response_class=PlainTextResponse,
            summary="Exporta uma política para Markdown")
def export_policy(name: str, db: Session = Depends(get_db)):
    try:
        body = editorial.export_policy(db, name)
    except editorial.EditError as exc:
        return PlainTextResponse(str(exc), status_code=404)
    return PlainTextResponse(body, headers={
        "Content-Disposition": f'attachment; filename="{name}.md"'})


# --- E18.3a entrega 3: feeds, newsletters de pauta e sem-leitura ----------------------------------
def _blog_names(db: Session) -> dict[str, str]:
    return {k: n for k, n in db.execute(text(
        "SELECT site_key, name FROM sites ORDER BY slot_offset_minutes, site_key"))}


def _feeds_context(request: Request, db: Session, site_key: str, **extra) -> dict:
    from app.services.urls import under

    site = editorial.site_values(db, site_key)
    rows = editorial.site_sources(db, site_key)
    pauta_only = terms.no_fetch_domains()
    for row in rows:
        row["pauta_only"] = under(terms.host_of(row["feed_url"]), pauta_only)
        row["version"] = editorial.version({f: row[f] for f in editorial.SOURCE_FIELDS})
    return _ctx(request, site=site, feeds=rows, active=sum(1 for r in rows if r["enabled"]),
                blogs=_blog_names(db), tiers=feedcheck.TIERS, langs=feedcheck.LANGS,
                kinds=feedcheck.KINDS, min_feeds=feedcheck.MIN_FEEDS,
                **{"msg": "", "err": "", "check": None, **extra})


@router.get("/feeds/{site_key}", response_class=HTMLResponse, summary="Feeds de um blog")
def feeds_page(site_key: str, request: Request, msg: str = "", err: str = "",
               db: Session = Depends(get_db)):
    try:
        context = _feeds_context(request, db, site_key, msg=msg, err=err)
    except editorial.EditError as exc:
        return _back(err=str(exc))
    return templates.TemplateResponse(request=request, name="config_feeds.html", context=context)


@router.post("/feeds/{site_key}/add", summary="Valida e acrescenta um feed")
def feed_add(site_key: str, request: Request, url: str = Form(...), name: str = Form(...),
             tier: str = Form(...), lang: str = Form(...), kind: str = Form("rss"),
             notes: str = Form(""), terms_reviewed: bool = Form(False),
             db: Session = Depends(get_db)):
    path = f"/dashboard/config/feeds/{site_key}"
    url, name = url.strip(), name.strip()
    if not terms_reviewed:
        return _back(err="Confirme que os termos de uso do site foram revisados (D-065).",
                     path=path)
    if not name:
        return _back(err="Dê um nome ao feed.", path=path)
    result = feedcheck.check(url, kind=kind, tier=tier, lang=lang)
    if not result.ok:
        return _back(err=f"Feed recusado: {result.reason}.", path=path)
    try:
        editorial.add_source(db, site_key, url, {"name": name, "source_type": kind, "tier": tier,
                                                 "lang": lang, "usage_notes": notes.strip()},
                             _actor(request),
                             {"items": result.items, "newest": result.newest,
                              "readable": result.readable, "pauta": result.pauta})
        db.commit()
    except editorial.EditError as exc:
        db.rollback()
        return _back(err=str(exc), path=path)
    extra = (f" O feed entra só como pauta: {feedcheck.PAUTA_WHY[result.pauta]}."
             if result.pauta else "")
    return _back(msg=f"Feed acrescentado ({result.items} itens, mais recente de "
                     f"{result.newest}).{extra}", path=path)


@router.post("/feeds/{site_key}/check", response_class=HTMLResponse,
             summary="Valida um feed sem gravar")
def feed_check(site_key: str, request: Request, url: str = Form(...), kind: str = Form("rss"),
               db: Session = Depends(get_db)):
    try:
        context = _feeds_context(request, db, site_key, check={
            "url": url.strip(), "kind": kind, "result": feedcheck.check(url.strip(), kind=kind)})
    except editorial.EditError as exc:
        return _back(err=str(exc))
    return templates.TemplateResponse(request=request, name="config_feeds.html", context=context)


@router.post("/feeds/{site_key}/edit", summary="Edita ou liga/desliga um feed")
def feed_edit(site_key: str, request: Request, url: str = Form(...), name: str = Form(...),
              tier: str = Form(...), lang: str = Form(...), notes: str = Form(""),
              enabled: bool = Form(False), version: str = Form(""),
              db: Session = Depends(get_db)):
    path = f"/dashboard/config/feeds/{site_key}"
    try:
        fields = editorial.edit_source(db, site_key, url, {
            "name": name.strip(), "tier": tier, "lang": lang, "usage_notes": notes.strip(),
            "enabled": enabled}, _actor(request), version)
        db.commit()
    except editorial.EditError as exc:
        db.rollback()
        return _back(err=str(exc), path=path)
    if not fields:
        return _back(msg="Nada mudou.", path=path)
    active = sum(1 for r in editorial.site_sources(db, site_key) if r["enabled"])
    warn = (f" Atenção: o blog ficou com {active} feeds ativos (piso {feedcheck.MIN_FEEDS})."
            if active < feedcheck.MIN_FEEDS else "")
    return _back(msg="Feed atualizado: " + ", ".join(fields) + "." + warn, path=path)


@router.get("/newsletters", response_class=HTMLResponse, summary="Newsletters de pauta")
def newsletters_page(request: Request, msg: str = "", err: str = "",
                     db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request=request, name="config_newsletters.html",
        context=_ctx(request, msg=msg, err=err, entries=editorial.newsletters(db),
                     blogs=_blog_names(db), langs=feedcheck.LANGS,
                     version=editorial.version(editorial.newsletters(db))),
    )


@router.post("/newsletters", summary="Grava a lista de newsletters de pauta")
async def newsletters_save(request: Request, db: Session = Depends(get_db)):
    data = await request.form()
    names, domains = data.getlist("name"), data.getlist("sender_domain")
    blogs, langs, drop = data.getlist("site_key"), data.getlist("lang"), data.getlist("drop")
    entries = [{"name": str(n), "sender_domain": str(d), "site_key": str(b), "lang": str(lang)}
               for i, (n, d, b, lang) in enumerate(zip(names, domains, blogs, langs, strict=False))
               if str(i) not in drop and (str(n).strip() or str(d).strip())]
    try:
        changed = editorial.save_newsletters(db, entries, _actor(request),
                                             str(data.get("version", "")))
        db.commit()
    except editorial.EditError as exc:
        db.rollback()
        return _back(err=str(exc), path="/dashboard/config/newsletters")
    return _back(msg="Lista de newsletters atualizada." if changed else "Nada mudou.",
                 path="/dashboard/config/newsletters")


@router.get("/sem-leitura", response_class=HTMLResponse, summary="Sites sem leitura")
def no_fetch_page(request: Request, msg: str = "", err: str = "", q: str = "",
                  db: Session = Depends(get_db)):
    entries = editorial.no_fetch(db)
    shown = [e for e in entries if q.strip().lower() in e["domain"]
             or q.strip().lower() in e["reason"].lower()] if q.strip() else entries
    return templates.TemplateResponse(
        request=request, name="config_no_fetch.html",
        context=_ctx(request, msg=msg, err=err, entries=shown, total=len(entries), q=q,
                     version=editorial.version(entries)),
    )


@router.post("/sem-leitura", summary="Acrescenta, edita ou tira um site da lista")
def no_fetch_save(request: Request, action: str = Form(...), domain: str = Form(...),
                  reason: str = Form(""), terms_url: str = Form(""), no_rss: bool = Form(False),
                  justification: str = Form(""), version: str = Form(""),
                  db: Session = Depends(get_db)):
    path = "/dashboard/config/sem-leitura"
    current = editorial.no_fetch(db)
    key = domain.strip().lower().removeprefix("www.")
    entry: dict = {"domain": key, "reason": reason.strip()}
    if terms_url.strip():
        entry["terms"] = terms_url.strip()
    if no_rss:
        entry["rss"] = False
    if action == "add":
        if any(e["domain"] == key for e in current):
            return _back(err=f"{key} já está na lista.", path=path)
        entries = [*current, entry]
    elif action == "edit":
        entries = [entry if e["domain"] == key else e for e in current]
    elif action == "remove":
        entries = [e for e in current if e["domain"] != key]
    else:
        return _back(err="Ação inválida.", path=path)
    try:
        changed = editorial.save_no_fetch(db, entries, _actor(request), justification, version)
        db.commit()
    except editorial.EditError as exc:
        db.rollback()
        return _back(err=str(exc), path=path)
    labels = {"add": "acrescentado", "edit": "atualizado", "remove": "retirado"}
    warn = ""
    if changed and action in ("add", "edit") and no_rss:
        active = db.execute(text(
            "SELECT count(*) FROM sources WHERE enabled AND (domain = :d OR domain LIKE :s)"),
            {"d": key, "s": f"%.{key}"}).scalar_one()
        if active:
            warn = (f" Atenção: {active} feed(s) ativo(s) deste domínio — com rss: false eles "
                    "devem ser desligados.")
    return _back(msg=(f"{key} {labels[action]}. Vale nos workers em até 1 minuto." + warn)
                 if changed else "Nada mudou.", path=path)


@router.get("/export/feeds/{site_key}.yaml", response_class=PlainTextResponse,
            summary="Exporta os feeds ativos de um blog")
def export_feeds(site_key: str, db: Session = Depends(get_db)):
    try:
        body = editorial.export_feeds_yaml(db, site_key)
    except editorial.EditError as exc:
        return PlainTextResponse(str(exc), status_code=404)
    return PlainTextResponse(body, headers={
        "Content-Disposition": f'attachment; filename="{site_key}.yaml"'})


@router.get("/export/newsletters.yaml", response_class=PlainTextResponse,
            summary="Exporta as newsletters de pauta")
def export_newsletters(db: Session = Depends(get_db)):
    return PlainTextResponse(editorial.export_newsletters_yaml(db), headers={
        "Content-Disposition": 'attachment; filename="newsletters.yaml"'})


@router.get("/export/sem-leitura.yaml", response_class=PlainTextResponse,
            summary="Exporta a lista de sites sem leitura")
def export_no_fetch(db: Session = Depends(get_db)):
    return PlainTextResponse(editorial.export_no_fetch_yaml(db), headers={
        "Content-Disposition": 'attachment; filename="sem-leitura.yaml"'})
