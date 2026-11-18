"""E18.1 (D-104): usuários do portal, autenticação e trilha de auditoria.

- Senhas só como hash argon2id; nunca em logs nem no `audit_log` (R68/R69).
- Bloqueio: `panel_login_max_failures` erros seguidos bloqueiam o usuário por
  `panel_login_lock_minutes`; cada IP tem um teto de tentativas na mesma janela (Redis).
  Toda falha devolve a mesma mensagem, exista o email ou não, e um email desconhecido
  também paga o custo de um hash (sem pista pelo tempo de resposta).
- Bootstrap: sem nenhum usuário, o `PANEL_PASSWORD` antigo cria o 1.º admin, que é
  obrigado a trocar a senha antes de navegar; a partir daí o `PANEL_PASSWORD` não vale mais.
"""
from __future__ import annotations

import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.cache import redis_client
from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

ROLES = ("admin", "operador")
MIN_PASSWORD = 12
FAILED = "Email ou senha incorretos, ou acesso bloqueado por tentativas. Aguarde alguns minutos."
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_hasher = PasswordHasher()  # argon2id com os parâmetros recomendados da biblioteca
_DUMMY_HASH = _hasher.hash(secrets.token_urlsafe(16))

_COLUMNS = ("id, email, name, role, active, must_change_password, session_version, "
            "locked_until, last_login_at, created_by, created_at")


class UserError(ValueError):
    """Regra violada; a mensagem é segura para mostrar na tela."""


# --- senhas -------------------------------------------------------------------
def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, candidate: str) -> bool:
    try:
        return _hasher.verify(password_hash, candidate)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def check_strength(password: str, email: str = "") -> None:
    if len(password) < MIN_PASSWORD:
        raise UserError(f"A senha precisa de pelo menos {MIN_PASSWORD} caracteres.")
    if email and email.split("@")[0].lower() in password.lower():
        raise UserError("A senha não pode conter o nome do email.")
    if len(set(password)) < 6:
        raise UserError("A senha é repetitiva demais.")


def temporary_password(email: str = "") -> str:
    """Aleatória e que passe no `check_strength` (um email curto, como `n@…`, fazia a senha
    sorteada conter o nome do email e a criação falhar de vez em quando)."""
    while True:
        password = secrets.token_urlsafe(12)
        try:
            check_strength(password, email)
        except UserError:
            continue
        return password


def normalize_email(email: str) -> str:
    value = (email or "").strip().lower()
    if not _EMAIL.match(value):
        raise UserError("Email inválido.")
    return value


# --- auditoria ----------------------------------------------------------------
def audit(db: Session, actor: str, action: str, target: str = "",
          details: dict[str, Any] | None = None) -> None:
    db.execute(text("INSERT INTO audit_log (actor, action, target, details) "
                    "VALUES (:a, :ac, :t, CAST(:d AS jsonb))"),
               {"a": actor[:120], "ac": action, "t": target[:200],
                "d": json.dumps(details or {}, ensure_ascii=False, default=str)})


def recent_audit(db: Session, limit: int = 50) -> list[dict[str, Any]]:
    rows = db.execute(text("SELECT at, actor, action, target, details FROM audit_log "
                           "ORDER BY id DESC LIMIT :n"), {"n": limit}).mappings()
    return [dict(r) for r in rows]


# --- consulta -----------------------------------------------------------------
def count(db: Session) -> int:
    return int(db.execute(text("SELECT count(*) FROM users")).scalar_one())


def get(db: Session, user_id: int) -> dict[str, Any] | None:
    row = db.execute(text(f"SELECT {_COLUMNS} FROM users WHERE id = :i"),
                     {"i": user_id}).mappings().first()
    return dict(row) if row else None


def list_users(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(text(f"SELECT {_COLUMNS} FROM users ORDER BY active DESC, email"))
    return [dict(r) for r in rows.mappings()]


def _active_admins(db: Session) -> int:
    return int(db.execute(text(
        "SELECT count(*) FROM users WHERE role = 'admin' AND active")).scalar_one())


# --- criação e manutenção ------------------------------------------------------
def create(db: Session, *, email: str, name: str, role: str, password: str, actor: str,
           must_change: bool = True) -> int:
    email = normalize_email(email)
    if role not in ROLES:
        raise UserError("Papel inválido.")
    check_strength(password, email)
    if db.execute(text("SELECT 1 FROM users WHERE email = :e"), {"e": email}).first():
        raise UserError("Já existe um usuário com esse email.")
    user_id = int(db.execute(text(
        "INSERT INTO users (email, name, role, password_hash, must_change_password, created_by) "
        "VALUES (:e, :n, :r, :h, :m, :c) RETURNING id"),
        {"e": email, "n": name.strip()[:120], "r": role, "h": hash_password(password),
         "m": must_change, "c": actor[:120]}).scalar_one())
    audit(db, actor, "user.create", email, {"role": role})
    return user_id


def set_active(db: Session, user_id: int, active: bool, actor: str) -> None:
    user = _require(db, user_id)
    if not active and user["role"] == "admin" and user["active"] and _active_admins(db) <= 1:
        raise UserError("Não é possível desativar o último admin ativo.")
    db.execute(text("UPDATE users SET active = :a, session_version = session_version + 1, "
                    "updated_at = now() WHERE id = :i"), {"a": active, "i": user_id})
    audit(db, actor, "user.activate" if active else "user.deactivate", user["email"])


def set_role(db: Session, user_id: int, role: str, actor: str) -> None:
    user = _require(db, user_id)
    if role not in ROLES:
        raise UserError("Papel inválido.")
    if user["role"] == "admin" and role != "admin" and user["active"] and _active_admins(db) <= 1:
        raise UserError("Não é possível rebaixar o último admin ativo.")
    db.execute(text("UPDATE users SET role = :r, session_version = session_version + 1, "
                    "updated_at = now() WHERE id = :i"), {"r": role, "i": user_id})
    audit(db, actor, "user.role", user["email"], {"from": user["role"], "to": role})


def reset_password(db: Session, user_id: int, actor: str) -> str:
    """Senha provisória (mostrada uma vez ao admin); o usuário troca no 1.º acesso."""
    user = _require(db, user_id)
    password = temporary_password(user["email"])
    db.execute(text("UPDATE users SET password_hash = :h, must_change_password = true, "
                    "session_version = session_version + 1, failed_logins = 0, "
                    "locked_until = NULL, updated_at = now() WHERE id = :i"),
               {"h": hash_password(password), "i": user_id})
    audit(db, actor, "user.reset_password", user["email"])
    return password


def change_password(db: Session, user_id: int, current: str, new: str) -> int:
    """Troca pela própria pessoa; devolve a nova `session_version` (as outras sessões caem)."""
    row = db.execute(text("SELECT email, password_hash FROM users WHERE id = :i AND active"),
                     {"i": user_id}).mappings().first()
    if not row or not verify_password(row["password_hash"], current):
        raise UserError("A senha atual não confere.")
    if new == current:
        raise UserError("A nova senha precisa ser diferente da atual.")
    check_strength(new, row["email"])
    version = int(db.execute(text(
        "UPDATE users SET password_hash = :h, must_change_password = false, "
        "session_version = session_version + 1, updated_at = now() WHERE id = :i "
        "RETURNING session_version"), {"h": hash_password(new), "i": user_id}).scalar_one())
    audit(db, row["email"], "user.change_password", row["email"])
    return version


def _require(db: Session, user_id: int) -> dict[str, Any]:
    user = get(db, user_id)
    if not user:
        raise UserError("Usuário não encontrado.")
    return user


# --- sessão -------------------------------------------------------------------
def session_user(db: Session, user_id: int, version: int) -> dict[str, Any] | None:
    """Usuário da sessão, se ativo e com a mesma versão do cookie."""
    user = get(db, user_id)
    if not user or not user["active"] or user["session_version"] != version:
        return None
    return user


# --- autenticação ---------------------------------------------------------------
def _ip_key(ip: str) -> str:
    return f"sb:login:ip:{ip}"


def ip_blocked(ip: str) -> bool:
    try:
        return int(redis_client.get(_ip_key(ip)) or 0) >= settings.panel_login_ip_limit
    except Exception:  # Redis fora não abre a porta nem fecha o login: segue o bloqueio por usuário
        log.warning("Login: Redis indisponível para o limite por IP")
        return False


def _count_ip_failure(ip: str) -> None:
    try:
        key = _ip_key(ip)
        redis_client.incr(key)
        redis_client.expire(key, settings.panel_login_lock_minutes * 60)
    except Exception:
        log.warning("Login: Redis indisponível para o limite por IP")


def authenticate(db: Session, email: str, password: str, ip: str) -> dict[str, Any]:
    """Devolve o usuário ou levanta `UserError(FAILED)` — a mesma mensagem para tudo."""
    if ip_blocked(ip):
        log.warning("Login: IP com tentativas demais")
        raise UserError(FAILED)
    try:
        email = normalize_email(email)
    except UserError:
        verify_password(_DUMMY_HASH, password)
        _count_ip_failure(ip)
        raise UserError(FAILED) from None
    row = db.execute(text("SELECT id, password_hash, active, locked_until FROM users "
                          "WHERE email = :e"), {"e": email}).mappings().first()
    now = datetime.now(UTC)
    if not row:
        verify_password(_DUMMY_HASH, password)
        _count_ip_failure(ip)
        raise UserError(FAILED)
    ok = verify_password(row["password_hash"], password)
    locked = row["locked_until"] is not None and row["locked_until"] > now
    if not ok or locked or not row["active"]:
        if not ok and not locked:
            lock_after = settings.panel_login_max_failures
            until = now + timedelta(minutes=settings.panel_login_lock_minutes)
            # Bloqueio vencido: a contagem recomeça do zero (senão o 1.º erro depois dos
            # 15 min bloqueava de novo — achado da revisão E18).
            db.execute(text(
                "UPDATE users SET "
                "failed_logins = CASE WHEN locked_until <= :now THEN 1 "
                "                     ELSE failed_logins + 1 END, "
                "locked_until = CASE "
                "  WHEN (CASE WHEN locked_until <= :now THEN 1 ELSE failed_logins + 1 END) >= :n "
                "    THEN :u "
                "  WHEN locked_until <= :now THEN NULL "
                "  ELSE locked_until END "
                "WHERE id = :i"), {"n": lock_after, "u": until, "i": row["id"], "now": now})
        _count_ip_failure(ip)
        log.warning("Login: falha para o usuário %s (%s)", row["id"],
                    "bloqueado" if locked else "inativo" if not row["active"] else "senha")
        raise UserError(FAILED)
    db.execute(text("UPDATE users SET failed_logins = 0, locked_until = NULL, "
                    "last_login_at = now() WHERE id = :i"), {"i": row["id"]})
    user = get(db, int(row["id"]))
    assert user is not None
    return user


def bootstrap_admin(db: Session, email: str, password: str, ip: str) -> dict[str, Any]:
    """1.º acesso sem usuários: o `PANEL_PASSWORD` cria o admin (com troca obrigatória)."""
    import hmac

    expected = settings.panel_password
    if count(db) or not expected or not hmac.compare_digest(password.encode(), expected.encode()):
        _count_ip_failure(ip)
        raise UserError(FAILED)
    email = normalize_email(email)
    user_id = int(db.execute(text(
        "INSERT INTO users (email, name, role, password_hash, must_change_password, created_by, "
        "last_login_at) VALUES (:e, '', 'admin', :h, true, 'bootstrap', now()) RETURNING id"),
        {"e": email, "h": hash_password(password)}).scalar_one())
    audit(db, email, "user.bootstrap_admin", email)
    log.info("Login: 1.º admin criado a partir do PANEL_PASSWORD (usuário %s)", user_id)
    user = get(db, user_id)
    assert user is not None
    return user
