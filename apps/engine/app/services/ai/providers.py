"""Gestão dos provedores de IA pelo painel (E3.7).

Tudo o que aqui grava valida primeiro as regras da cadeia (`chain.problems`):
uma alteração que deixe um papel sem provedor ativo, ou um redator sem revisor
de outra família, é rejeitada sem gravar. As API keys entram cifradas e nunca
saem: as leituras só dizem se há chave.
"""
from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services.ai import chain, quota
from app.services.ai.secrets import encrypt

_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")
_FAMILY = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}$")


# Pistas no nome do modelo → família. Serve para apanhar o engano de pôr um modelo
# de uma família num provedor declarado como de outra (o que furaria a regra
# "quem escreve nunca revê" sem a validação notar).
_FAMILY_HINTS = (
    ("anthropic", ("anthropic/", "anthropic.", "claude", "opus", "sonnet", "haiku")),
    ("openai", ("openai/", "openai.", "gpt", "chatgpt", "o1", "o3", "o4", "codex")),
    ("google", ("google/", "gemini", "gemma")),
    ("meta", ("meta/", "meta-llama/", "llama")),
    ("mistral", ("mistralai/", "mistral", "mixtral")),
    ("deepseek", ("deepseek",)),
    ("xai", ("x-ai/", "xai/", "grok")),
)


def implied_family(model: str) -> str | None:
    """Família que o nome do modelo indica, ou None se não der para saber."""
    name = model.strip().lower()
    for family, hints in _FAMILY_HINTS:
        if any(name.startswith(h) or f"/{h}" in name for h in hints):
            return family
    return None


def _check_family(name: str, family: str, model: str) -> None:
    implied = implied_family(model)
    if implied is not None and implied != family:
        raise ProviderInvalid(
            f"o modelo {model} é da família {implied}, mas {name} está declarado como "
            f"{family}: a regra redator ≠ revisor deixaria de valer"
        )


class ProviderInvalid(ValueError):
    """Dados de provedor recusados (mensagem pronta para mostrar no painel)."""


def list_providers(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(
        text("""SELECT p.name, p.kind, p.family, p.model, p.base_url, p.enabled,
                       p.api_key_enc IS NOT NULL AS has_api_key,
                       COALESCE(h.status, 'unknown') AS health,
                       COALESCE(h.detail, '') AS health_detail, h.checked_at,
                       p.daily_call_limit, p.daily_budget_usd, p.monthly_budget_usd,
                       p.price_in_usd_mtok, p.price_out_usd_mtok
                  FROM ai_providers p
                  LEFT JOIN ai_provider_health h ON h.provider_id = p.id
                 ORDER BY (p.kind = 'api'), p.id""")
    ).mappings().all()
    result = []
    for row in rows:
        used = quota.provider_usage(db, row["name"])
        result.append({**row, "calls_today": used.calls_today, "cost_today": used.cost_today,
                       "cost_month": used.cost_month,
                       "limit_reached": quota.blocked(db, SimpleNamespace(**row))})
    return result


def _get(db: Session, name: str) -> dict[str, Any]:
    row = db.execute(text("SELECT id, name, kind, family FROM ai_providers WHERE name = :n"),
                     {"n": name}).mappings().first()
    if row is None:
        raise ProviderInvalid(f"provedor desconhecido: {name}")
    return dict(row)


def _require_valid_chain(db: Session) -> None:
    found = chain.validate(db)
    if found:
        raise chain.ChainInvalid(found)


def set_enabled(db: Session, name: str, enabled: bool) -> None:
    _get(db, name)
    db.execute(text("UPDATE ai_providers SET enabled = :e, updated_at = now() WHERE name = :n"),
               {"e": enabled, "n": name})
    _require_valid_chain(db)


def set_model(db: Session, name: str, model: str) -> None:
    provider = _get(db, name)
    model = model.strip()
    if provider["kind"] == "api" and not model:
        raise ProviderInvalid("um provedor por API key precisa de modelo")
    if len(model) > 120 or any(c.isspace() for c in model):
        raise ProviderInvalid("nome de modelo inválido")
    _check_family(name, provider["family"], model)
    # O agy também serve modelos de outras famílias (Claude, por exemplo) com nomes que
    # as pistas podem não apanhar: aqui só entram modelos Gemini (ADR 0003).
    if name == "agy" and model and not model.lower().startswith("gemini-"):
        raise ProviderInvalid("o agy só aceita modelos gemini-* (regra redator ≠ revisor)")
    db.execute(text("UPDATE ai_providers SET model = :m, updated_at = now() WHERE name = :n"),
               {"m": model, "n": name})


def save_api_provider(db: Session, name: str, family: str, base_url: str, model: str,
                      api_key: str) -> None:
    """Cria ou atualiza um provedor por API key. Chave em branco = manter a guardada."""
    name, family = name.strip().lower(), family.strip().lower()
    base_url, model, api_key = base_url.strip().rstrip("/"), model.strip(), api_key.strip()
    if not _NAME.match(name):
        raise ProviderInvalid("nome: só minúsculas, algarismos e hífen (2 a 31 caracteres)")
    if not _FAMILY.match(family):
        raise ProviderInvalid("família do modelo inválida (ex.: openai, anthropic, google, meta)")
    if not base_url.startswith("https://") or any(c.isspace() for c in base_url):
        raise ProviderInvalid("o endereço tem de começar por https://")
    if not model or any(c.isspace() for c in model):
        raise ProviderInvalid("modelo em falta ou inválido")
    _check_family(name, family, model)

    existing = db.execute(text("SELECT kind, api_key_enc FROM ai_providers WHERE name = :n"),
                          {"n": name}).mappings().first()
    if existing is not None and existing["kind"] != "api":
        raise ProviderInvalid(f"{name} é um provedor por assinatura")
    if existing is None and not api_key:
        raise ProviderInvalid("API key em falta")

    params = {"n": name, "f": family, "u": base_url, "m": model}
    if existing is None:
        db.execute(
            text("""INSERT INTO ai_providers (name, kind, family, model, base_url, api_key_enc)
                    VALUES (:n, 'api', :f, :m, :u, :k)"""),
            {**params, "k": encrypt(api_key)},
        )
    else:
        db.execute(
            text("""UPDATE ai_providers
                       SET family = :f, model = :m, base_url = :u, updated_at = now(),
                           api_key_enc = COALESCE(:k, api_key_enc)
                     WHERE name = :n"""),
            {**params, "k": encrypt(api_key) if api_key else None},
        )
        # Mudar a família pode quebrar a regra redator ≠ revisor.
        _require_valid_chain(db)


def delete_api_provider(db: Session, name: str) -> None:
    if _get(db, name)["kind"] != "api":
        raise ProviderInvalid("os provedores por assinatura não se apagam; desligue-os")
    db.execute(text("DELETE FROM ai_providers WHERE name = :n"), {"n": name})
    _renumber(db)
    _require_valid_chain(db)


def edit_chain(db: Session, role: str, action: str, provider: str) -> None:
    """Ações do painel sobre a cadeia de um papel: up | down | add | remove."""
    names = [p.name for p in chain.load_chain(db, role)]
    if action == "add":
        if provider not in names:
            names.append(provider)
    elif provider not in names:
        raise ProviderInvalid(f"{provider} não está na cadeia de {role}")
    elif action == "remove":
        names.remove(provider)
    elif action in ("up", "down"):
        index = names.index(provider)
        target = index - 1 if action == "up" else index + 1
        if 0 <= target < len(names):
            names[index], names[target] = names[target], names[index]
    else:
        raise ProviderInvalid(f"ação desconhecida: {action}")
    chain.set_chain(db, role, names)


def _renumber(db: Session) -> None:
    """Fecha os buracos de posição deixados por um provedor apagado."""
    for role in chain.ROLES:
        ids = db.execute(
            text("SELECT provider_id FROM ai_role_chain WHERE role = :r ORDER BY position"),
            {"r": role},
        ).scalars().all()
        db.execute(text("DELETE FROM ai_role_chain WHERE role = :r"), {"r": role})
        for position, provider_id in enumerate(ids, start=1):
            db.execute(
                text("INSERT INTO ai_role_chain (role, position, provider_id) "
                     "VALUES (:r, :p, :id)"),
                {"r": role, "p": position, "id": provider_id},
            )
