"""Máquina de envio idempotente partilhada (digests E10.5 e newsletter E17).

Cada envio tem uma chave única na tabela (`where`): reservar com INSERT … ON CONFLICT,
enviar, e registar `sent`, `failed` (de certeza) ou `unknown` (SEND_UNKNOWN). Voltam a
tentar o `failed` e o `pending` órfão de uma execução que caiu entre a reserva e o
registro — melhor um e-mail raro em dobro do que um destinatário sem ele.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import session_scope
from app.core.logging import get_logger
from app.services.notify import mail

log = get_logger(__name__)

Sender = Callable[[str, str, str, str], str]


def record(db: Session, table: str, where: dict[str, Any], status: str, **fields: Any) -> None:
    sets = ", ".join(f"{k} = :{k}" for k in ["status", *fields])
    clause = " AND ".join(f"{k} = :w_{k}" for k in where)
    db.execute(
        text(f"UPDATE {table} SET {sets} WHERE {clause}"),  # noqa: S608 — nomes fixos, daqui
        {"status": status, **fields, **{f"w_{k}": v for k, v in where.items()}},
    )


def claim(db: Session, table: str, where: dict[str, Any], extra: dict[str, Any]) -> bool:
    """Reserva o envio. `False` se já foi enviado (ou ficou incerto): `failed` e `pending`
    órfão voltam a tentar."""
    columns = ", ".join([*where, *extra])
    values = ", ".join(f":{k}" for k in [*where, *extra])
    won = db.execute(
        text(
            f"INSERT INTO {table} ({columns}) VALUES ({values}) ON CONFLICT DO NOTHING RETURNING 1"
        ),  # noqa: S608
        {**where, **extra},
    ).first()
    if won:
        return True
    clause = " AND ".join(f"{k} = :{k}" for k in where)
    status = db.execute(text(f"SELECT status FROM {table} WHERE {clause}"), where).scalar()  # noqa: S608
    if status in ("failed", "pending"):
        record(db, table, where, "pending", **{k: v for k, v in extra.items() if k != "status"})
        return True
    return False


def deliver(
    table: str,
    where: dict[str, Any],
    recipient: str,
    subject: str,
    body: str,
    send: Sender,
    extra_ok: Callable[[str], dict[str, Any]],
) -> str:
    """Envia e regista o resultado: `sent`, `failed` (de certeza) ou `unknown` (SEND_UNKNOWN)."""
    try:
        reference = send(recipient, "", subject, body)
        status, fields = "sent", {**extra_ok(reference), "error": None}
    except mail.MailUnknown as exc:
        log.warning("E-mail incerto (%s): %s", table, exc)
        status, fields = "unknown", {"error": str(exc)[:300]}
    except mail.MailError as exc:
        log.warning("E-mail falhou (%s): %s", table, exc)
        status, fields = "failed", {"error": str(exc)[:300]}
    with session_scope() as db:
        record(db, table, where, status, **fields)
    return status
