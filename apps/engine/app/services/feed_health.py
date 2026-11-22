"""Fontes que pararam: feed com erro seguido ou sem pauta nova há dias (22/11).

Os 11 feeds da ONU News (Saúde, Política, Finanças) deram erro em cada leitura durante semanas
(até ~500 seguidas) sem que ninguém notasse: a falha de uma fonte não pára a descoberta e o
resumo diário não a mostrava. Aqui junta-se, por blog ligado:

- `failing`: fonte ligada cuja última leitura falhou e que já leva `feed_fail_alert_runs`
  leituras seguidas com erro (a descoberta corre de hora em hora: 24 ≈ um dia);
- `silent`: fonte que responde, mas não trouxe nenhuma pauta nova em `feed_silent_days` dias
  (feed abandonado, endereço que mudou, itens sem data, ordem estranha) — pode ser só uma fonte
  pouco ativa; é para olhar, não um erro.

Só leitura. O resumo diário mostra a secção quando há alguma coisa.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings


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
