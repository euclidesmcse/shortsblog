"""Custos e quotas dos provedores de IA (E3.8).

Cada tentativa que chega a um provedor fica em `provider_usage`, com o resultado em
`outcome` (sucesso ou tipo de falha). Antes de chamar um
provedor, a cadeia pergunta aqui se ele já atingiu um limite; se sim, salta-o
(o mesmo mecanismo do fallback), e o provedor volta sozinho no dia ou no mês
seguinte, contados no fuso do motor.

A verificação e o registo não são atómicos: com vários workers em simultâneo o
limite pode ser ultrapassado por tantas chamadas quantos os workers. É um teto
de proteção da cota, não um contador exato.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import session_scope
from app.core.logging import get_logger

log = get_logger(__name__)

# Job de publicação em produção neste contexto: vai para `provider_usage.job_id` sem ter de
# passar o id por cada chamada à IA (pesquisa, triagem, redação, revisão, traduções, capa).
_JOB: ContextVar[int | None] = ContextVar("sb_ai_job", default=None)


@contextmanager
def for_job(job_id: int) -> Iterator[None]:
    """As chamadas à IA feitas dentro do bloco ficam registadas com `job_id`."""
    token = _JOB.set(job_id)
    try:
        yield
    finally:
        _JOB.reset(token)


def current_job() -> int | None:
    """O job de produção do contexto atual (`for_job`), se houver."""
    return _JOB.get()

# Início do dia e do mês correntes no fuso do motor, como instantes.
_TZ = "CAST(:tz AS TEXT)"
_DAY_START = f"date_trunc('day', now() AT TIME ZONE {_TZ}) AT TIME ZONE {_TZ}"
_MONTH_START = f"date_trunc('month', now() AT TIME ZONE {_TZ}) AT TIME ZONE {_TZ}"


@dataclass(frozen=True)
class Usage:
    calls_today: int
    cost_today: Decimal
    cost_month: Decimal


def cost_usd(provider: Any, input_tokens: int, output_tokens: int) -> Decimal:
    """Custo pelo preço do provedor (USD por milhão de tokens). Assinaturas: 0."""
    price_in = provider.price_in_usd_mtok or Decimal(0)
    price_out = provider.price_out_usd_mtok or Decimal(0)
    return (Decimal(input_tokens) * price_in + Decimal(output_tokens) * price_out) / 1_000_000


def provider_usage(db: Session, name: str) -> Usage:
    row = db.execute(
        text(f"""SELECT COUNT(*) FILTER (WHERE created_at >= {_DAY_START}) AS calls_today,
                        COALESCE(SUM(cost_usd) FILTER (WHERE created_at >= {_DAY_START}), 0)
                            AS cost_today,
                        COALESCE(SUM(cost_usd), 0) AS cost_month
                   FROM provider_usage
                  WHERE provider = :p AND created_at >= {_MONTH_START}"""),
        {"p": name, "tz": settings.tz},
    ).one()
    return Usage(int(row.calls_today), Decimal(row.cost_today), Decimal(row.cost_month))


def site_usage(db: Session, site: str) -> Usage:
    """Custo de um blog nos provedores pagos."""
    row = db.execute(
        text(f"""SELECT COUNT(*) FILTER (WHERE created_at >= {_DAY_START}) AS calls_today,
                        COALESCE(SUM(cost_usd) FILTER (WHERE created_at >= {_DAY_START}), 0)
                            AS cost_today,
                        COALESCE(SUM(cost_usd), 0) AS cost_month
                   FROM provider_usage
                  WHERE site = :s AND kind = 'api' AND created_at >= {_MONTH_START}"""),
        {"s": site, "tz": settings.tz},
    ).one()
    return Usage(int(row.calls_today), Decimal(row.cost_today), Decimal(row.cost_month))


def job_calls_today(db: Session, job_id: int) -> int:
    """Chamadas de IA que o job já fez hoje (sucessos e falhas que gastaram cota)."""
    return int(db.execute(
        text(f"""SELECT COUNT(*) FROM provider_usage
                  WHERE job_id = :j AND created_at >= {_DAY_START}"""),
        {"j": job_id, "tz": settings.tz},
    ).scalar() or 0)


def job_blocked(db: Session, job_id: int) -> str | None:
    """Motivo pelo qual o job não pode chamar a IA agora, ou None.

    Orçamento diário por job (`llm_job_cap`, 0 desliga): um job em maratona de
    tentativas não pode esgotar sozinho a cota do dia dos outros slots — mesmo
    padrão do `searxng_job_cap`. Teto de proteção, não contador exato (ver topo).
    """
    from app.services import vault

    cap = vault.limit("llm_job_cap")
    if cap <= 0:
        return None
    if job_calls_today(db, job_id) >= cap:
        return "llm_job_cap"
    return None


def blocked(db: Session, provider: Any, site: str | None = None) -> str | None:
    """Motivo pelo qual o provedor está fora da cadeia por limite, ou None."""
    has_limit = (
        provider.daily_call_limit is not None
        or provider.daily_budget_usd is not None
        or provider.monthly_budget_usd is not None
    )
    if has_limit:
        used = provider_usage(db, provider.name)
        if provider.daily_call_limit is not None and used.calls_today >= provider.daily_call_limit:
            return "daily_call_limit"
        if provider.daily_budget_usd is not None and used.cost_today >= provider.daily_budget_usd:
            return "daily_budget"
        if (provider.monthly_budget_usd is not None
                and used.cost_month >= provider.monthly_budget_usd):
            return "monthly_budget"

    # Orçamento do blog: só trava provedores pagos; as assinaturas não têm custo por chamada.
    if site and provider.kind == "api":
        budget = db.execute(
            text("SELECT daily_budget_usd, monthly_budget_usd FROM ai_site_budgets "
                 "WHERE site = :s"),
            {"s": site},
        ).first()
        if budget is not None and (budget[0] is not None or budget[1] is not None):
            used = site_usage(db, site)
            if budget[0] is not None and used.cost_today >= budget[0]:
                return "site_daily_budget"
            if budget[1] is not None and used.cost_month >= budget[1]:
                return "site_monthly_budget"
    return None


def record(provider: Any, *, role: str, purpose: str, model: str, site: str | None,
           article_id: int | None, input_tokens: int = 0, output_tokens: int = 0,
           outcome: str = "ok") -> Decimal:
    """Regista uma tentativa que chegou ao provedor: sucesso ou falha (`outcome`).

    As falhas contam para o limite diário de chamadas, porque também gastam cota.
    """
    cost = cost_usd(provider, input_tokens, output_tokens)
    try:
        with session_scope() as db:
            db.execute(
                text("""INSERT INTO provider_usage
                          (provider, kind, model, role, purpose, site, article_id, job_id,
                           input_tokens, output_tokens, cost_usd, outcome)
                        VALUES (:p, :k, :m, :r, :pu, :s, :a, :j, :i, :o, :c, :oc)"""),
                {"p": provider.name, "k": provider.kind, "m": model, "r": role, "pu": purpose,
                 "s": site, "a": article_id, "j": _JOB.get(), "i": input_tokens,
                 "o": output_tokens, "c": cost, "oc": outcome},
            )
    except Exception:  # noqa: BLE001 — a contabilidade nunca pode derrubar a geração
        log.exception("Falha ao registar o uso do provedor %s", provider.name)
    return cost


def set_limits(db: Session, name: str, *, daily_call_limit: int | None,
               daily_budget_usd: Decimal | None, monthly_budget_usd: Decimal | None,
               price_in_usd_mtok: Decimal | None, price_out_usd_mtok: Decimal | None) -> None:
    """Grava limites e preços de um provedor (None = sem limite / sem preço)."""
    values = {"daily_call_limit": daily_call_limit, "daily_budget_usd": daily_budget_usd,
              "monthly_budget_usd": monthly_budget_usd,
              "price_in_usd_mtok": price_in_usd_mtok, "price_out_usd_mtok": price_out_usd_mtok}
    if any(v is not None and v < 0 for v in values.values()):
        raise ValueError("limites e preços não podem ser negativos")
    updated = db.execute(
        text("""UPDATE ai_providers
                   SET daily_call_limit = :daily_call_limit,
                       daily_budget_usd = :daily_budget_usd,
                       monthly_budget_usd = :monthly_budget_usd,
                       price_in_usd_mtok = :price_in_usd_mtok,
                       price_out_usd_mtok = :price_out_usd_mtok, updated_at = now()
                 WHERE name = :name RETURNING id"""),
        {**values, "name": name},
    ).first()
    if updated is None:
        raise ValueError(f"provedor desconhecido: {name}")


def set_site_budget(db: Session, site: str, daily_budget_usd: Decimal | None,
                    monthly_budget_usd: Decimal | None) -> None:
    """Orçamento de um blog nos provedores pagos; os dois vazios apagam a linha."""
    site = site.strip().lower()
    if not site or len(site) > 60 or any(c.isspace() for c in site):
        raise ValueError("identificador do blog inválido")
    if any(v is not None and v < 0 for v in (daily_budget_usd, monthly_budget_usd)):
        raise ValueError("orçamentos não podem ser negativos")
    if daily_budget_usd is None and monthly_budget_usd is None:
        db.execute(text("DELETE FROM ai_site_budgets WHERE site = :s"), {"s": site})
        return
    db.execute(
        text("""INSERT INTO ai_site_budgets (site, daily_budget_usd, monthly_budget_usd)
                VALUES (:s, :d, :m)
                ON CONFLICT (site) DO UPDATE
                   SET daily_budget_usd = EXCLUDED.daily_budget_usd,
                       monthly_budget_usd = EXCLUDED.monthly_budget_usd, updated_at = now()"""),
        {"s": site, "d": daily_budget_usd, "m": monthly_budget_usd},
    )


def site_budgets(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(
        text("SELECT site, daily_budget_usd, monthly_budget_usd FROM ai_site_budgets "
             "ORDER BY site")
    ).mappings().all()
    result = []
    for row in rows:
        used = site_usage(db, row["site"])
        result.append({**row, "cost_today": used.cost_today, "cost_month": used.cost_month})
    return result
