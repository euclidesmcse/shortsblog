"""R52: pulsação do backup — o motor avisa por e-mail se o backup diário parar.

O backup roda fora do motor (timer `sb-backup` no servidor; no pacote, o que o operador
quiser). No fim de cada sucesso, ele chama:

    docker exec sb-engine-api python -m app.services.backup_beat [epoch]

e o motor guarda o instante em `settings` (`backup_last_ok`). O alerta `backup_stale`
(`services/alerts.py`) dispara quando o último sucesso passa de `BACKUP_ALERT_HOURS` (26 h:
um dia e uma folga para o backup que atrasou). Sem nenhuma pulsação registrada, não há
alerta: quem não liga o backup ao motor não recebe aviso falso.
"""

from __future__ import annotations

import json
import sys
import time

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import get_setting, session_scope

KEY = "backup_last_ok"


def record(epoch: float | None = None) -> float:
    """Grava o instante do último backup bem-sucedido (epoch em segundos)."""
    moment = float(epoch if epoch is not None else time.time())
    with session_scope() as db:
        db.execute(
            text("""INSERT INTO settings (key, value, description)
                    VALUES (:k, CAST(:v AS JSONB), :d)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"""),
            {"k": KEY, "v": json.dumps(moment),
             "d": "Último backup bem-sucedido (epoch), pulsação do backup (R52)"},
        )
    return moment


def last_ok(db: Session) -> float | None:
    """Instante do último backup bem-sucedido, ou None se o backup nunca pulsou."""
    value = get_setting(db, KEY)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def main(argv: list[str]) -> int:
    try:
        epoch = float(argv[0]) if argv else None
    except ValueError:
        print(f"uso: python -m app.services.backup_beat [epoch]; recebido {argv[0]!r}",
              file=sys.stderr)
        return 2
    print(f"backup_last_ok = {record(epoch):.0f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
