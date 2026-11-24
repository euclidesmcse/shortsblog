"""Fontes que pararam: feed com erro seguido ou sem pauta nova há dias (22/11).

Os 11 feeds da ONU News (Saúde, Política, Finanças) deram erro em cada leitura durante semanas
(até ~500 seguidas) sem que ninguém notasse: a falha de uma fonte não pára a descoberta e o
resumo diário não a mostrava. Aqui junta-se, por blog ligado:

- `failing`: fonte ligada cuja última leitura falhou e que já leva `feed_fail_alert_runs`
  leituras seguidas com erro (a descoberta corre de hora em hora: 24 ≈ um dia);
- `silent`: fonte que responde, mas não trouxe nenhuma pauta nova em `feed_silent_days` dias
  (feed abandonado, endereço que mudou, itens sem data, ordem estranha) — pode ser só uma fonte
  pouco ativa; é para olhar, não um erro.

`newcomers` (23/11): fontes ligadas há até `feed_new_days` dias e o que já trouxeram — ideias,
as que passaram a triagem, a nota média, as pegadas por um job e os posts publicados. É o número
que decide a próxima poda ou troca de feeds, sem consulta à mão.

`intake` (24/11, D-118): por blog, as pautas de feed que entraram no dia local, quantas a triagem
já leu, quantas tiraram nota ≥ `triage_min_relevance` e quantas o teto diário por feed deixou de
fora (tabela `feed_capped`), com os feeds mais cortados.

Só leitura. O resumo diário mostra as secções quando há alguma coisa.
"""
from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.localtime import local_zone


def summary(db: Session, now: datetime | None = None) -> dict[str, list[dict[str, Any]]]:
    moment = now or datetime.now(UTC)
    rows = db.execute(
        text("""SELECT s.site_key, s.name, s.last_status, s.fail_count,
                       (SELECT max(i.created_at) FROM ideas i WHERE i.source_id = s.id)
                           AS last_idea, s.created_at
                  FROM sources s JOIN sites t ON t.site_key = s.site_key
                 WHERE s.enabled AND t.enabled
                 ORDER BY s.site_key, s.name"""),
    ).mappings().all()
    failing: list[dict[str, Any]] = []
    silent: list[dict[str, Any]] = []
    quiet_since = moment - timedelta(days=settings.feed_silent_days)
    for row in rows:
        if (row["last_status"] or "ok") != "ok":
            if row["fail_count"] >= settings.feed_fail_alert_runs:
                failing.append({"site": row["site_key"], "name": row["name"],
                                "status": row["last_status"], "runs": int(row["fail_count"])})
            continue
        last = row["last_idea"]
        # Fonte nova ainda não teve tempo de trazer nada.
        if (last is None and row["created_at"] < quiet_since) or (
                last is not None and last < quiet_since):
            silent.append({"site": row["site_key"], "name": row["name"],
                           "days": (moment - last).days if last else None})
    return {"failing": failing, "silent": silent}


_PUBLISHED = ("PUBLISHED_CONFIRMED", "VERIFY_REQUIRED")


def newcomers(db: Session, now: datetime | None = None) -> list[dict[str, Any]]:
    """Fontes ligadas há até `feed_new_days` dias (blogs ligados), das mais antigas às novas."""
    moment = now or datetime.now(UTC)
    rows = db.execute(
        text("""SELECT s.site_key, s.name, s.created_at,
                       count(i.id) AS ideas,
                       count(i.id) FILTER (WHERE i.relevance >= :min) AS good,
                       round(avg(i.relevance), 1) AS average,
                       count(i.id) FILTER (WHERE i.status IN ('taken', 'used')) AS taken,
                       (SELECT count(*) FROM publication_jobs j JOIN ideas k ON k.id = j.idea_id
                         WHERE k.source_id = s.id AND j.status IN :published) AS posts
                  FROM sources s JOIN sites t ON t.site_key = s.site_key
                  LEFT JOIN ideas i ON i.source_id = s.id
                 WHERE s.enabled AND t.enabled AND s.created_at >= :since
                 GROUP BY s.id ORDER BY s.site_key, s.created_at, s.name""").bindparams(
            bindparam("published", expanding=True)),
        {"min": settings.triage_min_relevance, "published": list(_PUBLISHED),
         "since": moment - timedelta(days=settings.feed_new_days)},
    ).mappings().all()
    return [{"site": r["site_key"], "name": r["name"], "since": r["created_at"],
             "ideas": int(r["ideas"]), "good": int(r["good"]), "taken": int(r["taken"]),
             "average": float(r["average"]) if r["average"] is not None else None,
             "posts": int(r["posts"])} for r in rows]


def intake(db: Session, local_date: date, top: int = 3) -> list[dict[str, Any]]:
    """Entrada de pautas de feed no dia local, por blog ligado (só ideias primárias)."""
    start = datetime.combine(local_date, time.min, tzinfo=local_zone())
    rows = db.execute(
        text("""SELECT t.site_key,
                       count(i.id) AS ideas,
                       count(i.id) FILTER (WHERE i.relevance IS NOT NULL) AS triaged,
                       count(i.id) FILTER (WHERE i.relevance >= :min) AS good
                  FROM sites t
                  LEFT JOIN ideas i ON i.site_key = t.site_key AND i.origin = 'rss'
                        AND i.duplicate_of IS NULL AND i.created_at >= :a AND i.created_at < :b
                 WHERE t.enabled
                 GROUP BY t.site_key ORDER BY t.site_key"""),
        {"min": settings.triage_min_relevance, "a": start, "b": start + timedelta(days=1)},
    ).mappings().all()
    capped = db.execute(
        text("""SELECT s.site_key, s.name, c.ideas FROM feed_capped c
                  JOIN sources s ON s.id = c.source_id
                 WHERE c.local_date = :d ORDER BY s.site_key, c.ideas DESC, s.name"""),
        {"d": local_date},
    ).mappings().all()
    out = []
    for row in rows:
        mine = [c for c in capped if c["site_key"] == row["site_key"]]
        out.append({"site": row["site_key"], "ideas": int(row["ideas"]),
                    "triaged": int(row["triaged"]), "good": int(row["good"]),
                    "capped": sum(int(c["ideas"]) for c in mine),
                    "top_capped": [(c["name"], int(c["ideas"])) for c in mine[:top]]})
    return out
