"""Links de decisão no e-mail (E10.3, R7): assinados, com validade de 72 h e de uso único.

O token leva a identificação do link, a validade e uma assinatura HMAC-SHA256 (chave
`SB_SECRET_KEY`) sobre o link, o job, a ação e a validade; não se guarda. Abrir o link (GET)
nunca decide: mostra uma página com um botão POST, porque os scanners de e-mail seguem links
GET. O uso único está na base: `consume` gasta o link (e os irmãos do mesmo job) numa única
instrução.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings

TTL_HOURS = 72
ACTIONS = ("approve", "reject")


@dataclass(frozen=True)
class Link:
    id: int
    job_id: int
    action: str
    expires_at: datetime


class InvalidLink(Exception):
    """Token adulterado, desconhecido, expirado ou já gasto (não se diz qual, de propósito)."""


def _sign(link_id: int, job_id: int, action: str, exp: int) -> str:
    key = settings.sb_secret_key.encode()
    mac = hmac.new(key, f"link|{link_id}|{job_id}|{action}|{exp}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac[:24]).decode().rstrip("=")


def create(db: Session, job_id: int, action: str, now: datetime | None = None) -> str:
    """Cria um link e devolve o token para ir no e-mail."""
    if action not in ACTIONS:
        raise ValueError(f"ação desconhecida: {action!r}")
    moment = now or datetime.now(UTC)
    expires = moment + timedelta(hours=TTL_HOURS)
    link_id = db.execute(
        text(
            "INSERT INTO approval_links (job_id, action, expires_at) "
            "VALUES (:j, :a, :e) RETURNING id"
        ),
        {"j": job_id, "a": action, "e": expires},
    ).scalar_one()
    exp = int(expires.timestamp())
    return f"{link_id}.{exp}.{_sign(link_id, job_id, action, exp)}"


def lookup(db: Session, token: str, now: datetime | None = None) -> Link:
    """Valida o token (assinatura, validade, ainda por gastar). Não altera nada."""
    moment = now or datetime.now(UTC)
    try:
        raw_id, raw_exp, signature = token.split(".")
        link_id, exp = int(raw_id), int(raw_exp)
    except (ValueError, AttributeError):
        raise InvalidLink from None
    row = db.execute(
        text("SELECT job_id, action, expires_at, used_at FROM approval_links WHERE id = :i"),
        {"i": link_id},
    ).first()
    if row is None:
        raise InvalidLink
    job_id, action, expires_at, used_at = row
    expected = _sign(link_id, job_id, action, exp)
    if not hmac.compare_digest(expected, signature) or exp != int(expires_at.timestamp()):
        raise InvalidLink
    if used_at is not None or expires_at <= moment:
        raise InvalidLink
    return Link(id=link_id, job_id=job_id, action=action, expires_at=expires_at)


def consume(db: Session, link: Link, user: str, now: datetime | None = None) -> bool:
    """Gasta o link e os irmãos do job, de uma vez. `False` se alguém se adiantou (uso único)."""
    moment = now or datetime.now(UTC)
    won = db.execute(
        text(
            "UPDATE approval_links SET used_at = :n, used_by = :u "
            "WHERE id = :i AND used_at IS NULL AND expires_at > :n RETURNING id"
        ),
        {"n": moment, "u": user, "i": link.id},
    ).first()
    if won is None:
        return False
    db.execute(
        text(
            "UPDATE approval_links SET used_at = :n, used_by = :u "
            "WHERE job_id = :j AND used_at IS NULL"
        ),
        {"n": moment, "u": f"{user} (outro link do mesmo job)", "j": link.job_id},
    )
    return True
