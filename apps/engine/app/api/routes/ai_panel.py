"""Painel "Provedores de IA" (E3.7).

Protegido como o resto do painel (`core/panel_auth.py`): Cloudflare Access +
sessão do login próprio. O cookie de sessão é `SameSite=Strict`, por isso os
formulários só são aceites a partir do próprio painel.
"""
from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.core.logging import get_logger
from app.services import users
from app.services.ai import chain, gateway, health, providers, quota

log = get_logger(__name__)
router = APIRouter(prefix="/dashboard/ai", tags=["dashboard"])
templates = Jinja2Templates(directory="app/dashboard/templates")

ROLE_LABELS = {"writer": "Redator", "reviewer": "Revisor", "translator": "Tradutor",
               "researcher": "Pesquisador"}
HEALTH_LABELS = {"ok": "autenticado", "not_authenticated": "não autenticado",
                 "quota_exhausted": "cota esgotada", "error": "erro", "unknown": "por verificar"}
# Só o Codex tem login conduzido pelo painel; os outros dois fazem-se no servidor.
LOGIN_HELP = {
    # D-038: login próprio no container (a cópia do login do servidor caducava em horas).
    "claude": "docker exec -it sb-ai-gateway claude auth login",
    "agy": "docker exec -it sb-ai-gateway agy",
}


LIMIT_LABELS = {"daily_call_limit": "limite diário de chamadas atingido",
                "daily_budget": "orçamento diário atingido",
                "monthly_budget": "orçamento mensal atingido"}


def _number(value: str, kind: type = Decimal) -> Decimal | int | None:
    """Campo de formulário → número; vazio = sem limite."""
    value = value.strip().replace(",", ".")
    if not value:
        return None
    try:
        return int(value) if kind is int else Decimal(value)
    except (ValueError, InvalidOperation):
        raise ValueError(f"valor numérico inválido: {value!r}") from None


def _back(msg: str = "", err: str = "") -> RedirectResponse:
    query = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    return RedirectResponse("/dashboard/ai" + (f"?{query}" if query else ""), status_code=303)


def _user(request: Request) -> str:
    return str(getattr(request.state, "panel_user", None) or "painel")[:120]


def _apply(request: Request, db: Session, action: str, done: str,
           operation: Callable[[], None]) -> RedirectResponse:
    """Corre uma alteração; regra violada ou dado inválido volta como erro, sem gravar."""
    try:
        operation()
        db.commit()
    except (chain.ChainInvalid, providers.ProviderInvalid, ValueError) as exc:
        db.rollback()
        return _back(err=str(exc))
    log.info("Painel IA: %s por %s", action, _user(request))
    return _back(msg=done)


def _audited(db: Session, request: Request, action: str, name: str, details: dict,
             operation: Callable[[], None]) -> None:
    """E18.3b: chaves de IA também vão para a auditoria — nunca o valor da chave."""
    operation()
    users.audit(db, _user(request), action, f"ai:{name.strip().lower()}", details)


def _limit_row(db: Session, query: str, key: str) -> dict[str, str | None]:
    """Limites/orçamentos como texto para a auditoria (E18.3b): o antes e o depois."""
    row = db.execute(text(query), {"k": key}).mappings().first()
    return {k: (None if v is None else str(v)) for k, v in (row or {}).items()}


@router.get("", response_class=HTMLResponse, summary="Provedores de IA")
def page(request: Request, msg: str = "", err: str = "", db: Session = Depends(get_db)):
    chains = {role: chain.load_chain(db, role) for role in chain.ROLES}
    all_providers = providers.list_providers(db)
    try:
        login = gateway.login_state("codex")
    except gateway.GatewayError:
        login = {"status": "none"}
    return templates.TemplateResponse(
        request=request,
        name="ai.html",
        context={
            "brand": settings.brand_name, "primary": settings.brand_primary_color,
            "accent": settings.brand_accent_color, "user": _user(request),
            "providers": all_providers,
            "roles": [
                {"key": role, "label": ROLE_LABELS[role], "chain": chains[role],
                 "available": [p["name"] for p in all_providers
                               if p["name"] not in {c.name for c in chains[role]}]}
                for role in chain.ROLES
            ],
            "problems": chain.problems(chains),
            "health_labels": HEALTH_LABELS, "login_help": LOGIN_HELP,
            "login": login, "msg": msg, "err": err,
            "cooldown": settings.ai_cooldown_min,
            "site_budgets": quota.site_budgets(db),
            "limit_labels": LIMIT_LABELS,
        },
    )


@router.post("/providers/{name}/toggle", summary="Liga ou desliga um provedor")
def toggle(name: str, request: Request, enabled: bool = Form(...),
           db: Session = Depends(get_db)):
    return _apply(request, db, f"{name} enabled={enabled}",
                  f"{name} {'ligado' if enabled else 'desligado'}.",
                  lambda: providers.set_enabled(db, name, enabled))


@router.post("/providers/{name}/model", summary="Escolhe o modelo de um provedor")
def model(name: str, request: Request, model: str = Form(""), db: Session = Depends(get_db)):
    return _apply(request, db, f"{name} model={model!r}", f"Modelo de {name} atualizado.",
                  lambda: providers.set_model(db, name, model))


@router.post("/providers/{name}/probe", summary="Verifica agora um provedor")
def probe(name: str, request: Request):
    result = health.probe_all(only=name, force=True)
    if name not in result:
        return _back(err=f"{name}: provedor desconhecido ou desligado")
    status = result[name]
    label = HEALTH_LABELS.get(status, status)
    log.info("Painel IA: probe de %s por %s → %s", name, _user(request), status)
    return _back(msg=f"{name}: {label}.") if status == health.OK else _back(
        err=f"{name}: {label}. Continua fora da cadeia até a verificação passar."
        if status in health.BLOCKING else f"{name}: {label}.")


@router.post("/providers/{name}/login", summary="Inicia o login por código de dispositivo")
def login(name: str, request: Request):
    try:
        state = gateway.login_start(name)
    except gateway.GatewayError as exc:
        if exc.kind == "already_authenticated":
            # Um novo login apagaria a credencial que está a funcionar.
            return _back(err=f"{name} já está autenticado; não é preciso novo login.")
        return _back(err=f"{name}: não foi possível iniciar o login ({exc.kind}).")
    log.info("Painel IA: login de %s iniciado por %s", name, _user(request))
    if state.get("status") == "failed" or not state.get("code"):
        return _back(err=f"{name}: o login não devolveu um código. No ChatGPT, confirme que o "
                         "login por código de dispositivo está ativo.")
    return _back(msg=f"{name}: abra o endereço abaixo e introduza o código.")


@router.post("/chain/{role}", summary="Altera a ordem dos provedores de um papel")
def edit_chain(role: str, request: Request, action: str = Form(...),
               provider: str = Form(...), db: Session = Depends(get_db)):
    return _apply(request, db, f"cadeia {role}: {action} {provider}",
                  f"Cadeia de {ROLE_LABELS.get(role, role)} atualizada.",
                  lambda: providers.edit_chain(db, role, action, provider))


@router.post("/api-providers", summary="Cria ou atualiza um provedor por API key")
def save_api_provider(request: Request, name: str = Form(...), family: str = Form(...),
                      base_url: str = Form(...), model: str = Form(...),
                      api_key: str = Form(""), db: Session = Depends(get_db)):
    return _apply(request, db, f"provedor API {name.strip().lower()} guardado",
                  f"{name.strip().lower()} guardado. Verifique-o antes de o pôr numa cadeia.",
                  lambda: _audited(db, request, "ai.provider.save", name,
                                   {"model": model.strip(), "key_changed": bool(api_key.strip())},
                                   lambda: providers.save_api_provider(db, name, family, base_url,
                                                                       model, api_key)))


@router.post("/api-providers/{name}/delete", summary="Apaga um provedor por API key")
def delete_api_provider(name: str, request: Request, db: Session = Depends(get_db)):
    return _apply(request, db, f"provedor API {name} apagado", f"{name} apagado.",
                  lambda: _audited(db, request, "ai.provider.delete", name, {},
                                   lambda: providers.delete_api_provider(db, name)))


@router.post("/providers/{name}/limits", summary="Limites e preços de um provedor")
def limits(name: str, request: Request, daily_call_limit: str = Form(""),
           daily_budget_usd: str = Form(""), monthly_budget_usd: str = Form(""),
           price_in_usd_mtok: str = Form(""), price_out_usd_mtok: str = Form(""),
           db: Session = Depends(get_db)):
    def operation() -> None:
        before = _limit_row(db, "SELECT daily_call_limit, daily_budget_usd, monthly_budget_usd, "
                                "price_in_usd_mtok, price_out_usd_mtok FROM ai_providers "
                                "WHERE name = :k", name)
        quota.set_limits(
            db, name,
            daily_call_limit=_number(daily_call_limit, int),  # type: ignore[arg-type]
            daily_budget_usd=_number(daily_budget_usd),  # type: ignore[arg-type]
            monthly_budget_usd=_number(monthly_budget_usd),  # type: ignore[arg-type]
            price_in_usd_mtok=_number(price_in_usd_mtok),  # type: ignore[arg-type]
            price_out_usd_mtok=_number(price_out_usd_mtok),  # type: ignore[arg-type]
        )
        after = _limit_row(db, "SELECT daily_call_limit, daily_budget_usd, monthly_budget_usd, "
                               "price_in_usd_mtok, price_out_usd_mtok FROM ai_providers "
                               "WHERE name = :k", name)
        users.audit(db, _user(request), "ai.provider.limits", f"ai:{name.strip().lower()}",
                    {"before": before, "after": after})

    return _apply(request, db, f"limites de {name}", f"Limites de {name} atualizados.", operation)


@router.post("/site-budgets", summary="Orçamento de um blog nos provedores pagos")
def site_budget(request: Request, site: str = Form(...), daily_budget_usd: str = Form(""),
                monthly_budget_usd: str = Form(""), db: Session = Depends(get_db)):
    def operation() -> None:
        query = ("SELECT daily_budget_usd, monthly_budget_usd FROM ai_site_budgets "
                 "WHERE site = :k")
        key = site.strip().lower()
        before = _limit_row(db, query, key)
        quota.set_site_budget(
            db, site,
            _number(daily_budget_usd),  # type: ignore[arg-type]
            _number(monthly_budget_usd),  # type: ignore[arg-type]
        )
        users.audit(db, _user(request), "ai.site_budget", f"site:{key}",
                    {"before": before, "after": _limit_row(db, query, key)})

    return _apply(request, db, f"orçamento do blog {site.strip().lower()}",
                  "Orçamento do blog atualizado.", operation)
