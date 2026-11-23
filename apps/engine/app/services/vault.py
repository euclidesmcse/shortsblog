"""E18.3b (D-104, D-109): cofre do sistema — a «engrenagem» do admin.

Segredos e parâmetros de integração editados pelo portal ficam na tabela `system_secrets`,
SEMPRE cifrados (Fernet derivado do `SB_SECRET_KEY`, propósito próprio). O banco tem precedência
sobre o ambiente: sem linha no banco, vale o env de antes — por isso o deploy com o cofre vazio
não muda nada, e a migração dos nossos segredos é feita item a item (`app.core.vault_cli`).

Leitura com cache de 60 s por processo (os workers veem a mudança em até 1 min, como o
sem-leitura). A auditoria guarda a chave do item, nunca o valor de um segredo.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as dtime
from typing import Any

from cryptography.fernet import InvalidToken
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import engine
from app.core.logging import get_logger
from app.services import users
from app.services.ai.secrets import fernet_for

log = get_logger(__name__)

PURPOSE = "sb-system-secrets"
CACHE_SECONDS = 60
SMTP_KEY_PREFIX = "xsmtpsib-"      # chave SMTP do Brevo: a API v3 não a aceita (responde 401)
SET_ACTION = "system.secret.set"
CLEAR_ACTION = "system.secret.clear"
ROTATE_ACTION = "system.token.rotate"
END_GRACE_ACTION = "system.token.end_grace"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")


class VaultError(ValueError):
    """Valor recusado (item desconhecido, formato inválido)."""


@dataclass(frozen=True)
class Item:
    key: str
    label: str
    group: str             # "email" | "wordpress" | "token" | "limit"
    secret: bool
    kind: str = "text"     # text | email | int | host | slots
    help: str = ""
    where: tuple[str, ...] = ()   # tokens: onde mais atualizar ao trocar
    lo: int = 1                   # kind int: faixa aceita
    hi: int = 65535


EMAIL_ITEMS: tuple[Item, ...] = (
    Item("mail_from_email", "E-mail do remetente", "email", False, "email",
         "Endereço do From dos avisos (domínio autenticado no Brevo)."),
    Item("mail_from_name", "Nome do remetente", "email", False),
    Item("approval_notify_email", "Destino dos avisos ao operador", "email", False, "email",
         "Aprovações, resumo diário, alertas do vigia."),
    Item("brevo_api_key", "Chave do Brevo", "email", True, "text",
         "Chave da API (xkeysib-…) ou chave SMTP (xsmtpsib-…, vira a senha do relay)."),
    Item("smtp_host", "Servidor SMTP", "email", False, "host"),
    Item("smtp_port", "Porta SMTP", "email", False, "int"),
    Item("smtp_user", "Usuário SMTP", "email", False),
    Item("smtp_password", "Senha SMTP", "email", True, "text",
         "Vazia: usa a chave SMTP do Brevo, se for uma."),
)
_EMAIL = {i.key: i for i in EMAIL_ITEMS}

GRACE_HOURS = 24
TOKEN_ITEMS: tuple[Item, ...] = (
    Item("sb_api_token", "Token da API de máquina", "token", True, "text",
         "As chamadas do n8n ao motor (`Authorization: Bearer`).",
         ("n8n: credencial «Header Auth» usada pelos workflows SB-* (valor `Bearer <token>`)",
          "Infisical `platform/prod` → `SB_API_TOKEN` (o compose ainda exige a variável)")),
    Item("newsletter_api_token", "Token da newsletter (WordPress → motor)", "token", True,
         "text", "Os 6 blogs chamam o motor com ele (cadastro, confirmação, descadastro e o relay "
         "da pauta).",
         ("Infisical `platform/prod` → `NEWSLETTER_API_TOKEN`",
          "`deploy/stacks/wordpress/secrets.sh render` e recriar os 6 WordPress "
          "(vira `SB_NEWSLETTER_TOKEN` no wp-config)")),
    Item("newsletter_webhook_token", "Chave do webhook de eventos do Brevo", "token", True,
         "text", "Bounce e reclamação da newsletter (Brevo → WordPress → motor).",
         ("Brevo → Transactional → Webhooks: a chave `key=` no endereço do webhook",
          "Infisical `platform/prod` → `NEWSLETTER_WEBHOOK_TOKEN`")),
    Item("pauta_webhook_token", "Chave do webhook de pauta (inbound)", "token", True, "text",
         "E-mails de newsletters de fabricantes que viram pauta (D-094).",
         ("Brevo → Inbound parsing: a chave `key=` no endereço do webhook",
          "Infisical `platform/prod` → `PAUTA_WEBHOOK_TOKEN`")),
)
_TOKEN = {i.key: i for i in TOKEN_ITEMS}

# Limites e horários (entrega 3): parâmetros, não segredos — a auditoria leva antes/depois.
LLM_JOB_CAP_MIN = 15               # artigo limpo gasta ~9–15 chamadas: abaixo disso nada sai
SLOT_COUNT = 5                     # `daily_target` vai de 0 a 5 (CHECK em `sites`)
SLOT_MIN_GAP_MIN = 60              # o deslocamento por blog vai até 59 min
SLOT_LAST = dtime(23, 0)           # + deslocamento de até 59 min: tudo no mesmo dia local
DEFAULT_SLOTS = (dtime(7, 0), dtime(10, 30), dtime(12, 30), dtime(15, 30), dtime(18, 30))
LIMIT_ITEMS: tuple[Item, ...] = (
    Item("llm_job_cap", "Chamadas de IA por job por dia", "limit", False, "int",
         f"0 desliga; senão {LLM_JOB_CAP_MIN}–500. Artigo limpo gasta ~9–15; no teto o job espera "
         "a cota do dia renovar.", lo=0, hi=500),
    Item("searxng_daily_cap", "Buscas na web por dia (todos os jobs)", "limit", False, "int",
         "Teto diário do reforço de pesquisa (SearXNG). 0 desliga a busca.", lo=0, hi=1000),
    Item("searxng_job_cap", "Buscas na web por job por dia", "limit", False, "int",
         "Um job sozinho não esgota o teto do dia. 0 desliga o orçamento por job.", lo=0, hi=200),
    Item("slot_times", "Horários-base dos slots", "limit", False, "slots",
         "5 horários HH:MM na hora local do blog, em ordem, com 1 h entre eles; cada blog soma o "
         "seu deslocamento. Só vale para os dias ainda não planejados."),
)
_LIMIT = {i.key: i for i in LIMIT_ITEMS}
# O `site_key` segue o validador de `sites.Site` (`^[a-z][a-z0-9-]{1,30}$`, com hífen). Antes
# (R71) aqui era `[a-z0-9_]+`: um blog com hífen não tinha item no cofre nem lia o ambiente.
_WP_KEY = re.compile(r"^wp\.([a-z][a-z0-9-]{1,30})\.(user|password)$")


def wp_env_name(site_key: str, field: str) -> str:
    """`SB_WP_<SITE>_APP_USER|APP_PASSWORD`: maiúsculas e hífen → `_` (nome de variável válido;
    é o que o `install.sh` e o Helm chart do pacote gravam). `field`: user | password."""
    suffix = "APP_USER" if field == "user" else "APP_PASSWORD"
    return f"SB_WP_{site_key.upper().replace('-', '_')}_{suffix}"


def wp_items(site_key: str) -> tuple[Item, Item]:
    return (Item(f"wp.{site_key}.user", "Usuário do WordPress", "wordpress", False),
            Item(f"wp.{site_key}.password", "Application Password", "wordpress", True, "text",
                 "Gerada no perfil do usuário publicador (não é a senha de login)."))


def item(key: str) -> Item:
    if key in _EMAIL:
        return _EMAIL[key]
    if key in _TOKEN:
        return _TOKEN[key]
    if key in _LIMIT:
        return _LIMIT[key]
    match = _WP_KEY.match(key)
    if match:
        return wp_items(match.group(1))[0 if match.group(2) == "user" else 1]
    raise VaultError(f"Item desconhecido: {key}")


def env_value(key: str) -> str:
    """O valor de antes do cofre: o ambiente (é a reserva enquanto o item não está no banco)."""
    match = _WP_KEY.match(key)
    if match:
        return os.environ.get(wp_env_name(match.group(1), match.group(2)), "")
    value = getattr(settings, key, "")
    return "" if value is None else str(value)


# --- Cifra ----------------------------------------------------------------
def encrypt(value: str, secret_key: str | None = None) -> str:
    return fernet_for(PURPOSE, secret_key).encrypt(value.encode()).decode()


def decrypt(token: str, secret_key: str | None = None) -> str:
    return fernet_for(PURPOSE, secret_key).decrypt(token.encode()).decode()


# --- Leitura (com cache) ----------------------------------------------------------------
_lock = threading.Lock()
_cache: dict[str, Any] = {"at": 0.0, "rows": {}}


def invalidate() -> None:
    with _lock:
        _cache["at"] = 0.0
        _cache["rows"] = {}


def _rows() -> dict[str, dict[str, Any]]:
    with _lock:
        if time.monotonic() - _cache["at"] < CACHE_SECONDS:
            return dict(_cache["rows"])
    try:
        with engine.connect() as conn:
            rows = {r["key"]: dict(r) for r in conn.execute(text(
                "SELECT key, value_enc, last4, previous_enc, previous_until, updated_at, "
                "updated_by FROM system_secrets"
            )).mappings()}
    except Exception as exc:  # noqa: BLE001 — sem banco, vale o ambiente (não derruba o envio)
        log.warning("Cofre ilegível (%s): usando o ambiente", type(exc).__name__)
        return {}
    with _lock:
        _cache["at"] = time.monotonic()
        _cache["rows"] = rows
    return dict(rows)


def effective(key: str) -> tuple[str, str]:
    """(valor, origem): origem `banco`, `ambiente` ou `` (faltando)."""
    row = _rows().get(key)
    if row:
        try:
            return decrypt(row["value_enc"]), "banco"
        except InvalidToken:
            log.error("Cofre: %s não abre com o SB_SECRET_KEY atual; usando o ambiente", key)
    value = env_value(key)
    return value, ("ambiente" if value else "")


def get(key: str) -> str:
    return effective(key)[0]


def get_int(key: str, default: int) -> int:
    try:
        return int(get(key) or default)
    except ValueError:
        return default


def limit(key: str) -> int:
    """Um limite numérico (cofre > ambiente); valor do cofre fora da faixa → o do ambiente.

    O ambiente vale como está (é a configuração de antes, sem a faixa da tela)."""
    value, source = effective(key)
    fallback = int(getattr(settings, key))
    if source != "banco":
        return fallback
    try:
        return int(validate(item(key), value))
    except VaultError:
        log.error("Cofre: %s com valor inválido; usando %d do ambiente", key, fallback)
        return fallback


def parse_slots(value: str) -> tuple[dtime, ...]:
    """«07:00,10:30,…» → horários. Exige 5, em ordem, com 1 h entre eles, até as 23:00."""
    parts = [p.strip() for p in re.split(r"[,;\s]+", value.strip()) if p.strip()]
    if len(parts) != SLOT_COUNT:
        raise VaultError(f"Horários dos slots: são {SLOT_COUNT} horários (há {len(parts)})")
    times = []
    for p in parts:
        m = re.fullmatch(r"([01]?\d|2[0-3]):([0-5]\d)", p)
        if not m:
            raise VaultError(f"Horários dos slots: «{p}» não é HH:MM")
        times.append(dtime(int(m.group(1)), int(m.group(2))))
    for a, b in zip(times, times[1:], strict=False):
        if (b.hour * 60 + b.minute) - (a.hour * 60 + a.minute) < SLOT_MIN_GAP_MIN:
            raise VaultError(f"Horários dos slots: em ordem e com pelo menos "
                             f"{SLOT_MIN_GAP_MIN} min entre eles ({a:%H:%M} → {b:%H:%M})")
    if times[-1] > SLOT_LAST:
        raise VaultError(f"Horários dos slots: o último vai até {SLOT_LAST:%H:%M} (o "
                         "deslocamento do blog não pode passar da meia-noite)")
    return tuple(times)


def format_slots(times: tuple[dtime, ...]) -> str:
    return ",".join(f"{t:%H:%M}" for t in times)


def slot_times() -> tuple[dtime, ...]:
    """Horários-base dos slots (cofre > ambiente > fábrica); valor estragado cai para o seguinte."""
    value, source = effective("slot_times")
    candidates = [value, settings.slot_times] if source == "banco" else [settings.slot_times]
    for candidate in candidates:
        try:
            return parse_slots(candidate)
        except VaultError as exc:
            log.error("slot_times inválido (%s); tentando a reserva", exc)
    return DEFAULT_SLOTS


def check_limits(values: dict[str, str]) -> None:
    """Coerência entre os limites (o candidato completo, depois da validação de cada um)."""
    daily, per_job = int(values["searxng_daily_cap"]), int(values["searxng_job_cap"])
    if daily and per_job > daily:
        raise VaultError(f"Buscas por job ({per_job}) maior que o teto do dia ({daily})")


def mail_ready() -> bool:
    """Há remetente e canal (Brevo ou SMTP), no cofre ou no ambiente."""
    return bool(get("mail_from_email") and (get("brevo_api_key") or get("smtp_host")))


def token_matches(key: str, presented: str) -> bool:
    """O token apresentado é o atual ou, durante a carência de uma troca, o anterior."""
    return token_match(key, presented) != ""


def token_match(key: str, presented: str) -> str:
    """Qual token foi apresentado: «current», «previous» (na carência de uma troca) ou "".
    Saber qual é o que diz se o outro lado já passou ao novo (R61: o Brevo demorou a aplicar
    a configuração nova do webhook, e só a chave nova prova que já se pode acabar a carência)."""
    current = get(key)
    if not current or not presented:
        return ""
    if hmac.compare_digest(presented.encode(), current.encode()):
        return "current"
    row = _rows().get(key)
    if not row or not row.get("previous_enc") or not row.get("previous_until"):
        return ""
    if row["previous_until"] <= datetime.now(UTC):
        return ""
    try:
        previous = decrypt(row["previous_enc"])
    except InvalidToken:
        return ""
    return "previous" if hmac.compare_digest(presented.encode(), previous.encode()) else ""


def bearer_matches(key: str, authorization: str) -> bool:
    prefix = "Bearer "
    return authorization.startswith(prefix) and token_matches(key, authorization[len(prefix):])


def wp_credentials(site_key: str) -> tuple[str, str]:
    return get(f"wp.{site_key}.user"), get(f"wp.{site_key}.password")


# --- Exibição ----------------------------------------------------------------
def mask(last4: str) -> str:
    return f"••••{last4}" if last4 else "••••"


def describe(it: Item) -> dict[str, Any]:
    """O que a tela mostra de um item: origem e valor (mascarado se for segredo)."""
    row = _rows().get(it.key)
    unreadable = False
    if row:
        try:
            value = decrypt(row["value_enc"])
            source = "banco"
        except InvalidToken:
            unreadable = True
            value, source = env_value(it.key), "ambiente"
    else:
        value = env_value(it.key)
        source = "ambiente" if value else ""
    if it.key == "smtp_password" and not value and get("brevo_api_key").startswith(
            SMTP_KEY_PREFIX):
        source = "brevo"            # a chave SMTP do Brevo serve de senha do relay
    shown = (mask(value[-4:]) if value else "") if it.secret else value
    return {"key": it.key, "label": it.label, "secret": it.secret, "kind": it.kind,
            "help": it.help, "source": source, "shown": shown, "unreadable": unreadable,
            "in_env": bool(env_value(it.key)),
            "updated_at": row["updated_at"] if row else None,
            "updated_by": row["updated_by"] if row else "",
            "where": it.where,
            "grace_until": _grace_until(row)}


def _grace_until(row: dict[str, Any] | None) -> datetime | None:
    until = (row or {}).get("previous_until")
    return until if (row or {}).get("previous_enc") and until and until > datetime.now(UTC) \
        else None


def version(db: Session, keys: list[str]) -> str:
    """Versão do que a tela mostrou (edição simultânea, como na Configuração)."""
    rows = {r[0]: str(r[1]) for r in db.execute(
        text("SELECT key, updated_at FROM system_secrets WHERE key = ANY(:k)"),
        {"k": list(keys)})}
    raw = "|".join(f"{k}:{rows.get(k, '')}" for k in sorted(keys))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# --- Validação e escrita ----------------------------------------------------------------
def validate(it: Item, value: str) -> str:
    value = value.strip()
    if not value:
        raise VaultError(f"{it.label}: valor vazio (use «voltar ao ambiente» para tirar do banco)")
    if len(value) > 4000 or any(ord(c) < 32 for c in value):
        raise VaultError(f"{it.label}: valor inválido")
    if it.kind == "email" and not _EMAIL_RE.match(value):
        raise VaultError(f"{it.label}: e-mail inválido")
    if it.kind == "host" and not _HOST_RE.match(value):
        raise VaultError(f"{it.label}: servidor inválido")
    if it.kind == "int":
        if not value.isdigit() or not it.lo <= int(value) <= it.hi:
            raise VaultError(f"{it.label}: número inválido (de {it.lo} a {it.hi})")
        value = str(int(value))
        if it.key == "llm_job_cap" and 0 < int(value) < LLM_JOB_CAP_MIN:
            raise VaultError(f"{it.label}: 0 (desliga) ou pelo menos {LLM_JOB_CAP_MIN} — um "
                             "artigo limpo gasta ~9–15 chamadas")
    if it.kind == "slots":
        value = format_slots(parse_slots(value))
    return value


def _check_site(db: Session, key: str) -> None:
    match = _WP_KEY.match(key)
    if match and not db.execute(text("SELECT 1 FROM sites WHERE site_key = :k"),
                                {"k": match.group(1)}).first():
        raise VaultError(f"Blog desconhecido: {match.group(1)}")


def set_value(db: Session, key: str, value: str, actor: str) -> bool:
    """Grava (cifrado) e audita SEM o valor. Devolve False se nada mudou."""
    it = item(key)
    _check_site(db, key)
    value = validate(it, value)
    current = db.execute(text("SELECT value_enc FROM system_secrets WHERE key = :k FOR UPDATE"),
                         {"k": key}).scalar()
    before = ""
    if current:
        try:
            before = decrypt(current)
        except InvalidToken:
            before = ""
    if current and before == value:
        return False
    db.execute(text(
        "INSERT INTO system_secrets (key, value_enc, last4, updated_at, updated_by) "
        "VALUES (:k, :v, :l, now(), :a) ON CONFLICT (key) DO UPDATE SET "
        "value_enc = EXCLUDED.value_enc, last4 = EXCLUDED.last4, updated_at = now(), "
        "updated_by = EXCLUDED.updated_by"),
        {"k": key, "v": encrypt(value), "l": value[-4:] if it.secret else "", "a": actor[:120]})
    details: dict[str, Any] = {"key": key, "secret": it.secret, "had_db_value": bool(current)}
    if not it.secret:               # parâmetro (servidor, remetente): o valor ajuda e não é segredo
        details.update(before=before or env_value(key), after=value)
    users.audit(db, actor, SET_ACTION, f"secret:{key}", details)
    invalidate()
    return True


def clear(db: Session, key: str, actor: str) -> bool:
    """Tira o item do banco: volta a valer o ambiente (se houver)."""
    it = item(key)
    gone = db.execute(text("DELETE FROM system_secrets WHERE key = :k RETURNING key"),
                      {"k": key}).first()
    if not gone:
        return False
    users.audit(db, actor, CLEAR_ACTION, f"secret:{key}",
                {"key": key, "secret": it.secret, "env_fallback": bool(env_value(key))})
    invalidate()
    return True


# --- Tokens: troca com carência -------------------------------------------------
def rotate_token(db: Session, key: str, actor: str, grace_hours: int = GRACE_HOURS) -> str:
    """Gera um token novo e o devolve (mostrado UMA vez). O anterior (do cofre ou do ambiente)
    continua aceito por `grace_hours`, para dar tempo de atualizar o outro lado."""
    it = item(key)
    if it.group != "token":
        raise VaultError(f"{it.label}: não é um token")
    previous = get(key)
    new = secrets.token_urlsafe(32)
    until = datetime.now(UTC) + timedelta(hours=grace_hours) if previous and grace_hours else None
    db.execute(text(
        "INSERT INTO system_secrets (key, value_enc, last4, previous_enc, previous_until, "
        "updated_at, updated_by) VALUES (:k, :v, :l, :p, :u, now(), :a) "
        "ON CONFLICT (key) DO UPDATE SET value_enc = EXCLUDED.value_enc, last4 = EXCLUDED.last4, "
        "previous_enc = EXCLUDED.previous_enc, previous_until = EXCLUDED.previous_until, "
        "updated_at = now(), updated_by = EXCLUDED.updated_by"),
        {"k": key, "v": encrypt(new), "l": new[-4:],
         "p": encrypt(previous) if until else None, "u": until, "a": actor[:120]})
    users.audit(db, actor, ROTATE_ACTION, f"secret:{key}",
                {"key": key, "grace_until": until.isoformat() if until else None})
    invalidate()
    return new


def end_grace(db: Session, key: str, actor: str) -> bool:
    """Encerra a carência: o token anterior deixa de valer agora."""
    gone = db.execute(text(
        "UPDATE system_secrets SET previous_enc = NULL, previous_until = NULL, updated_at = now() "
        "WHERE key = :k AND previous_enc IS NOT NULL RETURNING key"), {"k": key}).first()
    if not gone:
        return False
    users.audit(db, actor, END_GRACE_ACTION, f"secret:{key}", {"key": key})
    invalidate()
    return True


# --- Rotação do SB_SECRET_KEY ----------------------------------------------------------------
def rekey(db: Session, old_key: str, new_key: str) -> dict[str, int]:
    """Re-cifra o cofre e as API keys de IA da chave velha para a nova, numa transação.

    Tudo tem de abrir com a chave velha antes de qualquer escrita: se algo não abre, nada muda.
    """
    from app.services.ai import secrets as ai_secrets

    vault_rows = db.execute(text(
        "SELECT key, value_enc, previous_enc FROM system_secrets FOR UPDATE")).mappings().all()
    ai_rows = db.execute(text(
        "SELECT id, api_key_enc FROM ai_providers WHERE api_key_enc IS NOT NULL "
        "AND api_key_enc <> '' FOR UPDATE")).mappings().all()
    plain_vault = []
    for r in vault_rows:
        try:
            previous = decrypt(r["previous_enc"], old_key) if r["previous_enc"] else None
            plain_vault.append((r["key"], decrypt(r["value_enc"], old_key), previous))
        except InvalidToken:
            raise VaultError(f"{r['key']} não abre com a chave atual: nada foi alterado") from None
    plain_ai = [(r["id"], ai_secrets.decrypt(r["api_key_enc"], old_key)) for r in ai_rows]
    for key, value, previous in plain_vault:
        db.execute(text("UPDATE system_secrets SET value_enc = :v, previous_enc = :p "
                        "WHERE key = :k"),
                   {"k": key, "v": encrypt(value, new_key),
                    "p": encrypt(previous, new_key) if previous is not None else None})
    for provider_id, value in plain_ai:
        db.execute(text("UPDATE ai_providers SET api_key_enc = :v WHERE id = :i"),
                   {"i": provider_id, "v": ai_secrets.encrypt(value, new_key)})
    users.audit(db, "cli", "system.rekey", "secret_key",
                {"vault_items": len(plain_vault), "ai_keys": len(plain_ai)})
    invalidate()
    return {"vault_items": len(plain_vault), "ai_keys": len(plain_ai)}
