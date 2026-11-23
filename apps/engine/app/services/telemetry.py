"""Métricas do motor multi-site para o Prometheus (E12.1): só leitura da base, a cada scrape.

Cada amostra é `(nome, ajuda, rótulos, valor)`; o coletor em `health.py` transforma-as em
métricas. Sem estado: não ficam rótulos antigos no Prometheus quando um blog, um motivo ou um
provedor deixa de existir.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings

Sample = tuple[str, str, dict[str, str], float]
QUEUED_WAIT_MIN = 20  # um job QUEUED há mais que isto (num blog ligado) conta como parado
APPROVAL_WAIT_H = 24  # à espera de aprovação há mais que isto
_REASONS = (  # motivo do bloqueio (prefixo de `blocked_reason`) → categoria
    (re.compile(r"fontes insuficientes"), "fontes_insuficientes"),
    (re.compile(r"revisão"), "revisao"),
    (re.compile(r"gates"), "gates"),
    (re.compile(r"tradução"), "traducao"),
    (re.compile(r"o redator recusou"), "redator_recusou"),
    (re.compile(r"pipeline editorial"), "pipeline"),
    (re.compile(r"sem ideias"), "sem_ideias"),
)
_PRODUCING = ("RESEARCHING", "WRITING", "VALIDATING", "TRANSLATING")


def reason_category(reason: str | None) -> str:
    text_ = (reason or "").lower()
    return next((name for pattern, name in _REASONS if pattern.search(text_)), "outro")


def _today(now: datetime):
    return now.astimezone(ZoneInfo(settings.tz)).date()


def samples(db: Session, now: datetime | None = None) -> list[Sample]:
    moment = now or datetime.now(UTC)
    day = _today(moment)
    out: list[Sample] = []

    # --- Slots e jobs de hoje, por blog
    for site, status, n in db.execute(
        text(
            "SELECT site_key, status, count(*) FROM publication_jobs WHERE local_date = :d "
            "AND NOT reserve GROUP BY 1, 2 ORDER BY 1, 2"
        ),
        {"d": day},
    ).all():
        out.append(
            ("sb_jobs_today", "Jobs de hoje por blog e estado", {"site": site, "status": status}, n)
        )
    for site, n in db.execute(
        text("SELECT site_key, count(*) FROM publication_jobs WHERE local_date = :d "
             "AND NOT reserve GROUP BY 1 ORDER BY 1"),
        {"d": day},
    ).all():
        out.append(("sb_slots_planned_today", "Slots planeados hoje por blog", {"site": site}, n))
    blocked: dict[tuple[str, str], int] = {}
    for site, reason in db.execute(
        text(
            "SELECT site_key, blocked_reason FROM publication_jobs WHERE local_date = :d "
            "AND NOT reserve AND status IN ('BLOCKED', 'RETRY_WAIT', 'FAILED')"
        ),
        {"d": day},
    ).all():
        key = (site, reason_category(reason))
        blocked[key] = blocked.get(key, 0) + 1
    for (site, reason), n in blocked.items():
        out.append(
            (
                "sb_jobs_blocked_today",
                "Jobs bloqueados hoje por blog e motivo",
                {"site": site, "reason": reason},
                n,
            )
        )

    # --- Jobs presos
    stuck = {
        "lease_expired": db.execute(
            text(
                "SELECT count(*) FROM publication_jobs WHERE status = ANY(:a) AND "
                "(lease_until_utc IS NULL OR lease_until_utc < :n)"
            ),
            {
                "a": [
                    "CLAIMED",
                    "RESEARCHING",
                    "WRITING",
                    "VALIDATING",
                    "TRANSLATING",
                    "PUBLISHING",
                ],
                "n": moment,
            },
        ).scalar_one(),
        "queued_waiting": db.execute(
            text(
                "SELECT count(*) FROM publication_jobs j JOIN sites s ON s.site_key = j.site_key "
                "WHERE s.enabled AND j.status = 'QUEUED' AND j.due_at_utc < :t"
            ),
            {"t": moment - timedelta(minutes=QUEUED_WAIT_MIN)},
        ).scalar_one(),
        "approval_waiting": db.execute(
            text(
                "SELECT count(*) FROM publication_jobs WHERE status = 'AWAITING_APPROVAL' AND "
                "updated_at < :t"
            ),
            {"t": moment - timedelta(hours=APPROVAL_WAIT_H)},
        ).scalar_one(),
        "verify_required": db.execute(
            text("SELECT count(*) FROM publication_jobs WHERE status = 'VERIFY_REQUIRED'")
        ).scalar_one(),
    }
    for kind, n in stuck.items():
        out.append(("sb_jobs_stuck", "Jobs presos por tipo", {"kind": kind}, n))

    # --- Duração por etapa (média dos últimos 7 dias) e tempo até à aprovação
    for status, avg_s, _n in db.execute(
        text(
            """SELECT to_status, avg(extract(epoch FROM (next_ts - ts))), count(*) FROM (
               SELECT to_status, timestamp_utc AS ts,
                      lead(timestamp_utc) OVER (PARTITION BY job_id ORDER BY id) AS next_ts
                 FROM publication_events
                WHERE event_type = 'status_changed' AND timestamp_utc > now() - interval '7 days') e
            WHERE next_ts IS NOT NULL AND to_status = ANY(:s) GROUP BY to_status
            ORDER BY to_status"""
        ),
        {"s": [*_PRODUCING, "AWAITING_APPROVAL"]},
    ).all():
        if status == "AWAITING_APPROVAL":
            out.append(
                (
                    "sb_time_to_approval_seconds",
                    "Tempo médio até à decisão (7 dias)",
                    {},
                    float(avg_s),
                )
            )
        else:
            out.append(
                (
                    "sb_stage_avg_seconds",
                    "Duração média por etapa de produção (7 dias)",
                    {"stage": status.lower()},
                    float(avg_s),
                )
            )

    # --- Provedores de IA: chamadas, custo, estado
    for provider, outcome, n in db.execute(
        text(
            "SELECT provider, outcome, count(*) FROM provider_usage "
            "WHERE created_at > now() - interval '24 hours' GROUP BY 1, 2 ORDER BY 1, 2"
        )
    ).all():
        out.append(
            (
                "sb_provider_calls_24h",
                "Chamadas por provedor e resultado (24 h)",
                {"provider": provider, "outcome": outcome},
                n,
            )
        )
    for provider, window, cost in db.execute(
        text(
            """SELECT provider, 'day', COALESCE(sum(cost_usd), 0) FROM provider_usage
            WHERE created_at > now() - interval '24 hours' GROUP BY 1
           UNION ALL
           SELECT provider, 'month', COALESCE(sum(cost_usd), 0) FROM provider_usage
            WHERE created_at > now() - interval '30 days' GROUP BY 1
            ORDER BY 2, 1"""
        )
    ).all():
        out.append(
            (
                "sb_provider_cost_usd",
                "Custo por provedor (USD)",
                {"provider": provider, "window": window},
                float(cost),
            )
        )
    for name, enabled, status in db.execute(
        text(
            "SELECT p.name, p.enabled, COALESCE(h.status, 'unknown') FROM ai_providers p "
            "LEFT JOIN ai_provider_health h ON h.provider_id = p.id ORDER BY p.name"
        )
    ).all():
        out.append(
            (
                "sb_provider_enabled",
                "1 se o provedor está ligado",
                {"provider": name},
                1 if enabled else 0,
            )
        )
        out.append(
            (
                "sb_provider_status",
                "Estado do provedor (1 no estado atual)",
                {"provider": name, "status": status},
                1,
            )
        )
    per_post = db.execute(
        text(
            """SELECT avg(c), avg(n) FROM (
                   SELECT sum(cost_usd) AS c, count(*) AS n FROM provider_usage
                    WHERE job_id IS NOT NULL AND created_at > now() - interval '30 days'
                    GROUP BY job_id) x"""
        )
    ).one()
    if per_post[0] is not None:
        out.append(
            (
                "sb_cost_per_post_usd",
                "Custo médio por artigo (USD, 30 dias)",
                {},
                float(per_post[0]),
            )
        )
        out.append(
            (
                "sb_calls_per_post",
                "Chamadas de IA por artigo (média, 30 dias)",
                {},
                float(per_post[1]),
            )
        )

    # --- Orçamentos: fração usada (provedores com teto e blogs com teto)
    out.extend(budget_ratios(db))
    return out


def _ratio(used: Decimal | int, cap: Decimal | int | None) -> float | None:
    return float(Decimal(used) / Decimal(cap)) if cap and Decimal(cap) > 0 else None


def budget_ratios(db: Session) -> list[Sample]:
    """`sb_budget_used_ratio{scope,name,period}`: 0–1+ (≥ 0,8 dispara o alerta de orçamento)."""
    from app.services.ai import quota

    out: list[Sample] = []
    help_ = "Fração usada do teto (chamadas ou USD)"
    for row in db.execute(
        text(
            "SELECT name, daily_call_limit, daily_budget_usd, monthly_budget_usd FROM ai_providers "
            "WHERE enabled"
        )
    ).all():
        name, calls_cap, day_cap, month_cap = row
        used = quota.provider_usage(db, name)
        for period, ratio in (
            ("calls_day", _ratio(used.calls_today, calls_cap)),
            ("cost_day", _ratio(used.cost_today, day_cap)),
            ("cost_month", _ratio(used.cost_month, month_cap)),
        ):
            if ratio is not None:
                out.append(
                    (
                        "sb_budget_used_ratio",
                        help_,
                        {"scope": "provider", "name": name, "period": period},
                        ratio,
                    )
                )
    for site, day_cap, month_cap in db.execute(
        text("SELECT site, daily_budget_usd, monthly_budget_usd FROM ai_site_budgets")
    ).all():
        used = quota.site_usage(db, site)
        for period, ratio in (
            ("cost_day", _ratio(used.cost_today, day_cap)),
            ("cost_month", _ratio(used.cost_month, month_cap)),
        ):
            if ratio is not None:
                out.append(
                    (
                        "sb_budget_used_ratio",
                        help_,
                        {"scope": "site", "name": site, "period": period},
                        ratio,
                    )
                )
    return out
