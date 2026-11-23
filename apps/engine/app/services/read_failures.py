"""Falhas de leitura das páginas-fonte, por sítio (22/11).

A pesquisa lê o artigo-fonte e as repetições noutros feeds; o que falha (bloqueio 403,
desafio anti-robô, robots.txt, paywall, página só de JavaScript) só ia para o log do worker,
que some a cada deploy — não havia como saber que sítios nunca se deixam ler. Aqui conta-se,
por dia, cada leitura certa e cada falha (com o motivo) do sítio, no Redis (com AOF), por
`KEEP_DAYS`; o resumo diário lista os sítios que mais falham na última semana.

Contar nunca atrapalha a pesquisa: erro do Redis é engolido.
"""
from __future__ import annotations

import contextlib
from collections import Counter, defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Any

import redis

from app.core.cache import redis_client
from app.core.config import settings

KEEP_DAYS = 15
# Motivos esperados (23/11): o robots.txt do sítio proíbe os robôs de IA e o motor obedece —
# o feed serve só de pauta (D-070). Não há nada a corrigir, então não entram no alerta; o resumo
# mostra-os numa linha informativa (`by_design`).
BY_DESIGN = frozenset({"ai_disallowed"})
_FAIL = "sb:readfail:"
_OK = "sb:readok:"


def _day(now: datetime | None) -> date:
    return (now or datetime.now(UTC)).date()


def _bump(key: str, field: str) -> None:
    with contextlib.suppress(redis.RedisError):
        pipe = redis_client.pipeline()
        pipe.hincrby(key, field, 1)
        pipe.expire(key, KEEP_DAYS * 86_400)
        pipe.execute()


def failed(site: str, kind: str, now: datetime | None = None) -> None:
    if site:
        _bump(f"{_FAIL}{_day(now).isoformat()}", f"{site}\t{kind}")


def read_ok(site: str, now: datetime | None = None) -> None:
    if site:
        _bump(f"{_OK}{_day(now).isoformat()}", site)


def _collect(now: datetime | None, days: int) -> tuple[dict[str, Counter[str]], Counter[str]]:
    today = _day(now)
    fails: dict[str, Counter[str]] = defaultdict(Counter)
    oks: Counter[str] = Counter()
    for offset in range(days):
        stamp = (today - timedelta(days=offset)).isoformat()
        for field, count in dict(redis_client.hgetall(f"{_FAIL}{stamp}")).items():
            site, _, kind = str(field).partition("\t")
            fails[site][kind] += int(count)
        for name, count in dict(redis_client.hgetall(f"{_OK}{stamp}")).items():
            oks[str(name)] += int(count)
    return fails, oks


def summary(now: datetime | None = None, days: int = 7) -> list[dict[str, Any]]:
    """Sítios com pelo menos `read_fail_alert_min` falhas em `days` dias, os piores primeiro:
    falhas por motivo e leituras certas no mesmo período. Os motivos `BY_DESIGN` não contam."""
    try:
        fails, oks = _collect(now, days)
    except redis.RedisError:
        return []
    real = {site: Counter({k: n for k, n in kinds.items() if k not in BY_DESIGN})
            for site, kinds in fails.items()}
    ranked = sorted(((sum(kinds.values()), site) for site, kinds in real.items()),
                    key=lambda pair: (-pair[0], pair[1]))
    return [{"site": site, "fails": total, "ok": oks[site],
             "kinds": dict(real[site].most_common())}
            for total, site in ranked
            if total >= settings.read_fail_alert_min][: settings.read_fail_alert_top]


def by_design(now: datetime | None = None, days: int = 7) -> list[tuple[str, int]]:
    """Sítios recusados de propósito (robots.txt contra IA) em `days` dias: (sítio, vezes)."""
    try:
        fails, _ = _collect(now, days)
    except redis.RedisError:
        return []
    counts = [(site, sum(n for k, n in kinds.items() if k in BY_DESIGN))
              for site, kinds in fails.items()]
    return sorted(((s, n) for s, n in counts if n), key=lambda p: (-p[1], p[0]))
