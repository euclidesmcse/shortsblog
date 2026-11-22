"""Newsletter dos posts publicados (E17, D-062): cadastro, dupla confirmação, saída e envio diário.

Uma lista para os 6 blogs. Quem assina recebe 1 e-mail por dia com o link de cada post aprovado e
publicado desde a edição anterior (ver `edition_start`), no idioma que escolheu.

- **Cadastro** (`subscribe`): resposta igual exista o e-mail ou não; limite por IP e por endereço;
  campo isca. A pessoa fica `pending` e recebe um e-mail com o link de confirmação.
- **Dupla confirmação** (`confirm`): link assinado (HMAC, `newsletter_confirm_hours`), que abre uma
  página com botão; só o POST confirma (os scanners de e-mail seguem os GET). Cada novo e-mail de
  confirmação muda `token_version`: os links anteriores deixam de valer.
- **Saída com 1 clique** (`unsubscribe`): link assinado no rodapé de todo e-mail, sem validade e sem
  login, e os cabeçalhos `List-Unsubscribe` + `List-Unsubscribe-Post` (RFC 8058). O e-mail é
  apagado; fica só o hash, que permite reinscrever.
- **Envio diário** (`send_daily`): um envio por assinante e edição (`newsletter_sends`),
  idempotente; nada se dia sem post; `unknown` (SEND_UNKNOWN) não se repete; respeita o limite
  diário do Brevo e avisa o operador aos 80 %.

Os links dos e-mails apontam para o blog de origem do assinante (`/wp-json/sb/v1/newsletter/…`):
quem os atende é o mu-plugin `sb-newsletter.php`, que fala com o motor pela rede interna. Os logs
nunca levam o e-mail.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.cache import QUEUES, redis_client
from app.core.config import settings
from app.core.db import session_scope
from app.core.localtime import local_zone
from app.core.logging import get_logger
from app.core.security import hash_ip
from app.services import vault
from app.services.notify import mail, sends
from app.services.sitepub import POLYLANG as _LANG_SLUG  # idioma do motor → slug do Polylang
from app.services.sites import LANGUAGES

log = get_logger(__name__)

_EMAIL = re.compile(r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+$")
Sender = Callable[..., str]


class Closed(Exception):
    """O cadastro está fechado (`newsletter_enabled` desligado)."""


# --- E-mail e hash --------------------------------------------------------------------------------


def normalize(email: str) -> str | None:
    """Forma única do endereço (minúsculas, sem espaços), ou None se não parece um e-mail."""
    value = (email or "").strip().lower()
    if len(value) > 254 or not _EMAIL.match(value) or ".." in value:
        return None
    return value


def email_hash(email: str) -> str:
    """O que fica de quem saiu: não devolve o e-mail, mas reconhece-o se voltar a assinar."""
    return hmac.new(settings.sb_secret_key.encode(), f"newsletter|{email}".encode(),
                    hashlib.sha256).hexdigest()


# --- Links assinados ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Token:
    subscriber_id: int
    action: str          # confirm | unsubscribe
    expires: int         # 0 = sem validade (saída)
    version: int


def _sign(subscriber_id: int, action: str, expires: int, version: int, digest: str) -> str:
    message = f"nl|{subscriber_id}|{action}|{expires}|{version}|{digest}".encode()
    mac = hmac.new(settings.sb_secret_key.encode(), message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac[:24]).decode().rstrip("=")


def make_token(subscriber_id: int, action: str, *, version: int, digest: str,
               now: datetime | None = None) -> str:
    expires = 0
    if action == "confirm":
        moment = now or datetime.now(UTC)
        expires = int((moment + timedelta(hours=settings.newsletter_confirm_hours)).timestamp())
    signature = _sign(subscriber_id, action, expires, version, digest)
    return f"{subscriber_id}.{action[0]}.{expires}.{version}.{signature}"


def _read_token(db: Session, token: str, action: str, now: datetime) -> dict[str, Any] | None:
    """A linha do assinante se o token é válido para a ação; None se adulterado ou vencido."""
    try:
        raw_id, kind, raw_exp, raw_version, signature = (token or "").split(".")
        subscriber_id, expires, version = int(raw_id), int(raw_exp), int(raw_version)
    except ValueError:
        return None
    if kind != action[0]:
        return None
    row = db.execute(text("SELECT * FROM subscribers WHERE id = :i"),
                     {"i": subscriber_id}).mappings().first()
    if row is None:
        return None
    expected = _sign(subscriber_id, action, expires, version, row["email_hash"])
    if not hmac.compare_digest(expected, signature):
        return None
    if action == "confirm" and (version != row["token_version"]
                                or expires <= int(now.timestamp())):
        return None
    return dict(row)


def link(site_base: str, language: str, action: str, token: str) -> str:
    """Endereço no blog de origem que atende a ação (mu-plugin `sb-newsletter.php`).

    Fica em `/wp-json/`, que o Cloudflare não guarda em cache (regra `sb-bypass-wp`).
    """
    query = urlencode({"t": token, "l": _LANG_SLUG.get(language, "pt")})
    return f"{site_base.rstrip('/')}/wp-json/sb/v1/newsletter/{action}?{query}"


# --- Limites --------------------------------------------------------------------------------------


def _over_limit(key: str, limit: int, window_s: int) -> bool:
    count = redis_client.incr(key)
    if count == 1:
        redis_client.expire(key, window_s)
    return int(count) > limit


# --- Cadastro -------------------------------------------------------------------------------------


def subscribe(db: Session, *, email: str, language: str, site_key: str, ip: str = "",
              honeypot: str = "", now: datetime | None = None) -> dict[str, Any]:
    """Pedido de cadastro. Responde sempre `{"ok": True}` (não diz se o e-mail já existia).

    Erros de forma (e-mail, idioma, blog) devolvem `{"ok": False, "error": …}`; o limite por IP
    devolve `{"ok": False, "error": "limite"}`. Levanta `Closed` se o cadastro estiver fechado.
    """
    if not settings.newsletter_enabled:
        raise Closed
    moment = now or datetime.now(UTC)
    if honeypot:
        log.info("Newsletter: cadastro com o campo isca preenchido, ignorado")
        return {"ok": True}
    address = normalize(email)
    if address is None:
        return {"ok": False, "error": "email"}
    if language not in LANGUAGES:
        return {"ok": False, "error": "idioma"}
    known = db.execute(text("SELECT 1 FROM sites WHERE site_key = :s"), {"s": site_key}).first()
    if not known:
        return {"ok": False, "error": "blog"}
    ip_digest = hash_ip(ip) if ip else None
    if ip_digest and _over_limit(f"nl:rate:ip:{ip_digest}", settings.newsletter_rate_ip_hour, 3600):
        log.info("Newsletter: limite de cadastros por IP atingido")
        return {"ok": False, "error": "limite"}
    digest = email_hash(address)
    if _over_limit(f"nl:rate:email:{digest}", settings.newsletter_rate_email_day, 86400):
        return {"ok": True}          # limite por endereço: não se manda outro e-mail, sem o dizer
    row = db.execute(text("SELECT id, status FROM subscribers WHERE email_hash = :h"),
                     {"h": digest}).first()
    if row is not None and row.status in ("confirmed", "suppressed"):
        # Já assina, ou o endereço devolveu bounce/reclamação: nada a fazer, sem o revelar.
        return {"ok": True}
    values = {"e": address, "h": digest, "l": language, "s": site_key, "ip": ip_digest,
              "c": settings.newsletter_consent_version, "n": moment}
    if row is None:
        subscriber_id = db.execute(text(
            """INSERT INTO subscribers (email, email_hash, language, origin_site, ip_hash,
                                        consent_version, created_at, updated_at)
               VALUES (:e, :h, :l, :s, :ip, :c, :n, :n) RETURNING id"""), values).scalar_one()
    else:
        subscriber_id = row.id
        db.execute(text(
            """UPDATE subscribers SET email = :e, language = :l, origin_site = :s, ip_hash = :ip,
                      consent_version = :c, status = 'pending', unsubscribed_at = NULL,
                      token_version = token_version + 1, updated_at = :n
                WHERE id = :i"""), {**values, "i": subscriber_id})
    _enqueue_confirmation(subscriber_id)
    return {"ok": True}


def _enqueue_confirmation(subscriber_id: int) -> None:
    QUEUES["fast"].enqueue(send_confirmation, subscriber_id, job_timeout=120)


def send_confirmation(subscriber_id: int, send: Sender | None = None,
                      now: datetime | None = None) -> str:
    """E-mail com o link de confirmação (corre no worker `fast`)."""
    with session_scope() as db:
        row = db.execute(text(
            """SELECT s.id, s.email, s.email_hash, s.language, s.token_version, s.status,
                      t.base_url, t.name
                 FROM subscribers s JOIN sites t ON t.site_key = s.origin_site
                WHERE s.id = :i"""), {"i": subscriber_id}).first()
    if row is None or row.status != "pending" or not row.email:
        return "nada a enviar"
    token = make_token(row.id, "confirm", version=row.token_version, digest=row.email_hash,
                       now=now)
    words = COPY[row.language]
    url = link(row.base_url, row.language, "confirm", token)
    body = _layout(row.language, f"""
<p style="margin:0 0 12px">{_esc(words["confirm_intro"])}</p>
<p style="margin:20px 0">{_button(url, words["confirm_button"])}</p>
<p style="margin:0 0 8px;color:#555;font-size:13px">{_esc(words["confirm_expire"].format(
    hours=settings.newsletter_confirm_hours))}</p>
<p style="margin:0;color:#555;font-size:13px">{_esc(words["confirm_ignore"])}</p>""")
    text_body = (f"{words['confirm_intro']}\n\n{url}\n\n"
                 f"{words['confirm_expire'].format(hours=settings.newsletter_confirm_hours)}\n"
                 f"{words['confirm_ignore']}\n")
    sender = send or mail.send_transactional
    try:
        sender(row.email, "", words["confirm_subject"], body, from_email=_from_email(),
               from_name=settings.newsletter_from_name, text_body=text_body)
    except mail.MailError as exc:      # inclui MailUnknown: o leitor pode pedir de novo
        log.warning("Newsletter: e-mail de confirmação não saiu (%s)", type(exc).__name__)
        return "falhou"
    return "enviado"


# --- Confirmação e saída --------------------------------------------------------------------------


def check(db: Session, token: str, action: str, now: datetime | None = None) -> dict[str, Any]:
    """O que a página do link deve mostrar, sem mudar nada: `ready`, `done` ou `invalid`."""
    moment = now or datetime.now(UTC)
    row = _read_token(db, token, action, moment)
    if row is None:
        return {"state": "invalid"}
    done = (row["status"] == "confirmed") if action == "confirm" else (
        row["status"] in ("unsubscribed", "suppressed"))
    if action == "confirm" and row["status"] != "pending" and not done:
        return {"state": "invalid"}
    return {"state": "done" if done else "ready", "language": row["language"]}


def confirm(db: Session, token: str, now: datetime | None = None) -> dict[str, Any]:
    moment = now or datetime.now(UTC)
    row = _read_token(db, token, "confirm", moment)
    if row is None:
        return {"state": "invalid"}
    if row["status"] == "confirmed":
        return {"state": "done", "language": row["language"]}
    won = db.execute(text(
        """UPDATE subscribers SET status = 'confirmed', confirmed_at = :n, updated_at = :n
            WHERE id = :i AND status = 'pending' AND token_version = :v RETURNING id"""),
        {"n": moment, "i": row["id"], "v": row["token_version"]}).first()
    if won is None:
        return {"state": "invalid"}
    log.info("Newsletter: assinatura confirmada (id %s)", row["id"])
    return {"state": "done", "language": row["language"]}


def unsubscribe(db: Session, token: str, now: datetime | None = None) -> dict[str, Any]:
    """Saída de 1 clique: apaga o e-mail e fica o hash. Repetir não faz mal."""
    moment = now or datetime.now(UTC)
    row = _read_token(db, token, "unsubscribe", moment)
    if row is None:
        return {"state": "invalid"}
    if row["status"] in ("pending", "confirmed"):
        db.execute(text(
            """UPDATE subscribers SET status = 'unsubscribed', email = NULL, ip_hash = NULL,
                      unsubscribed_at = :n, updated_at = :n WHERE id = :i"""),
            {"n": moment, "i": row["id"]})
        log.info("Newsletter: saída (id %s)", row["id"])
    return {"state": "done", "language": row["language"]}


# Eventos do webhook do Brevo que tiram o endereço da lista.
SUPPRESS_EVENTS = {"hard_bounce", "spam", "blocked", "invalid_email", "unsubscribed",
                   "complaint"}


def brevo_event(db: Session, event: str, email: str, now: datetime | None = None) -> bool:
    """Bounce definitivo ou reclamação: `suppressed`, sem e-mail. True se mudou alguém."""
    if event not in SUPPRESS_EVENTS:
        return False
    address = normalize(email)
    if address is None:
        return False
    changed = db.execute(text(
        """UPDATE subscribers SET status = 'suppressed', email = NULL, ip_hash = NULL,
                  suppressed_at = :n, suppressed_reason = :r, updated_at = :n
            WHERE email_hash = :h AND status IN ('pending', 'confirmed') RETURNING id"""),
        {"n": now or datetime.now(UTC), "r": event, "h": email_hash(address)}).first()
    if changed:
        log.info("Newsletter: endereço suprimido pelo Brevo (%s, id %s)", event, changed.id)
    return changed is not None


# --- Envio diário ---------------------------------------------------------------------------------


def _from_email() -> str:
    return settings.newsletter_from_email or vault.get("mail_from_email")


def edition_start(db: Session, now: datetime) -> datetime:
    """Desde quando a edição de `now` recolhe posts.

    Desde o momento em que a edição ANTERIOR foi montada (a 1.ª reserva dela em
    `newsletter_sends`): o post aprovado e publicado depois do envio das 07:00 entra na edição
    seguinte, em vez de não sair em nenhuma (16/11: saúde e política às 10:37 UTC). Sem edição
    anterior, o começo do dia em SP; e nunca antes de `newsletter_catchup_hours`.
    """
    local = now.astimezone(local_zone())
    day_start = datetime(local.year, local.month, local.day, tzinfo=local_zone()).astimezone(UTC)
    previous = db.execute(text(
        """SELECT min(created_at) FROM newsletter_sends
            WHERE edition_date = (SELECT max(edition_date) FROM newsletter_sends
                                   WHERE edition_date < :d)"""),
        {"d": local.date()}).scalar_one()
    if previous is None:
        return day_start
    floor = now - timedelta(hours=settings.newsletter_catchup_hours)
    return max(min(previous, day_start), floor)


def edition_posts(db: Session, now: datetime) -> list[dict[str, Any]]:
    """Posts publicados desde a edição anterior até agora, com título, resumo e link."""
    start = edition_start(db, now)
    rows = db.execute(text(
        """SELECT j.id, j.published_at_utc, j.site_key, s.name AS site_name, t.language,
                  t.title, t.excerpt, t.wordpress_url, im.wordpress_media_urls, im.alt_texts
             FROM publication_jobs j
             JOIN sites s ON s.site_key = j.site_key
             JOIN job_translations t ON t.job_id = j.id
             LEFT JOIN LATERAL (SELECT wordpress_media_urls, alt_texts FROM image_assets a
                                 WHERE a.job_id = j.id ORDER BY a.id LIMIT 1) im ON TRUE
            WHERE j.status = 'PUBLISHED_CONFIRMED' AND j.published_at_utc >= :a
              AND j.published_at_utc <= :b AND t.wordpress_url IS NOT NULL
            ORDER BY j.published_at_utc, j.id"""), {"a": start, "b": now}).mappings().all()
    posts: dict[int, dict[str, Any]] = {}
    for row in rows:
        post = posts.setdefault(row["id"], {"site": row["site_name"],
                                            "site_key": row["site_key"], "by_language": {}})
        covers = row["wordpress_media_urls"] or {}
        alts = row["alt_texts"] or {}
        post["by_language"][row["language"]] = {
            "title": row["title"], "excerpt": row["excerpt"] or "", "url": row["wordpress_url"],
            "cover": str(covers.get(row["language"]) or ""),
            "cover_alt": str(alts.get(row["language"]) or "")}
    return list(posts.values())


def _in_language(post: dict[str, Any], language: str) -> dict[str, str] | None:
    return post["by_language"].get(language) or post["by_language"].get("pt-BR")


def sent_today(db: Session, now: datetime) -> int:
    """E-mails da newsletter que contam para o limite diário do Brevo (enviados ou incertos)."""
    since = now - timedelta(hours=24)
    return int(db.execute(text(
        """SELECT count(*) FROM newsletter_sends
            WHERE status IN ('sent', 'unknown') AND COALESCE(sent_at_utc, created_at) >= :a"""),
        {"a": since}).scalar_one())


def send_daily(now: datetime | None = None, send: Sender | None = None,
               pause: Callable[[float], None] = time.sleep) -> dict[str, Any]:
    """A edição do dia para cada assinante confirmado. Chamar de novo não duplica."""
    moment = now or datetime.now(UTC)
    edition = moment.astimezone(local_zone()).date()
    if not settings.newsletter_enabled:
        return {"edition": edition.isoformat(), "sent": 0, "reason": "newsletter desligada"}
    with session_scope() as db:
        posts = edition_posts(db, moment)
        if not posts:
            return {"edition": edition.isoformat(), "sent": 0, "reason": "sem posts no dia"}
        subscribers = db.execute(text(
            """SELECT s.id, s.email, s.email_hash, s.language, t.base_url
                 FROM subscribers s JOIN sites t ON t.site_key = s.origin_site
                WHERE s.status = 'confirmed' AND s.email IS NOT NULL ORDER BY s.id""")).all()
        used = sent_today(db, moment)
    capacity = max(settings.newsletter_daily_limit - used, 0)
    sender = send or mail.send_transactional
    counts = {"sent": 0, "failed": 0, "unknown": 0, "skipped": 0, "deferred": 0,
              "no_language": 0}
    streak = 0
    for sub in subscribers:
        if not any(_in_language(post, sub.language) for post in posts):
            # Nenhum post no idioma do assinante (nem o pt-BR de reserva): melhor e-mail
            # nenhum do que uma edição vazia; não reserva nem gasta capacidade (revisão E14).
            counts["no_language"] += 1
            continue
        where = {"edition_date": edition, "subscriber_id": sub.id}
        with session_scope() as db:
            if capacity <= 0:
                # Limite do Brevo: quem ainda não recebeu fica para a próxima chamada
                # (`failed` e `pending` órfão ainda não receberam).
                done = db.execute(text(
                    """SELECT 1 FROM newsletter_sends WHERE edition_date = :d
                          AND subscriber_id = :s AND status NOT IN ('failed', 'pending')"""),
                    {"d": edition, "s": sub.id}).first()
                counts["skipped" if done else "deferred"] += 1
                continue
            # `created_at` = o momento em que a edição foi montada: é dele que a edição
            # seguinte parte (`edition_start`).
            if not sends.claim(db, "newsletter_sends", where,
                               {"posts": len(posts), "error": None, "created_at": moment}):
                counts["skipped"] += 1           # já enviado (ou incerto) nesta edição
                continue
        capacity -= 1
        subject, body, text_body, unsubscribe_url = _edition_email(sub, posts, edition)
        headers = {"List-Unsubscribe": f"<{unsubscribe_url}>",
                   "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}

        def dispatch(to: str, name: str, subject_: str, body_: str,
                     headers_: dict[str, str] = headers, text_: str = text_body) -> str:
            return sender(to, name, subject_, body_, headers=headers_,
                          from_email=_from_email(), from_name=settings.newsletter_from_name,
                          text_body=text_)

        status = sends.deliver(
            "newsletter_sends", where, sub.email, subject, body, dispatch,
            lambda ref: {"message_ref": str(ref)[:200], "sent_at_utc": datetime.now(UTC)})
        counts[status] += 1
        streak += 1
        if streak >= settings.newsletter_batch_size:
            pause(settings.newsletter_batch_pause_s)
            streak = 0
    _warn_limit(moment, used + counts["sent"] + counts["unknown"], counts["deferred"])
    log.info("Newsletter %s: %s", edition, counts)
    return {"edition": edition.isoformat(), "posts": len(posts), **counts}


def _warn_limit(now: datetime, used: int, deferred: int) -> None:
    """Aviso ao operador (uma vez por dia) ao passar 80 % do limite diário do Brevo."""
    limit = settings.newsletter_daily_limit
    if limit <= 0 or (used < 0.8 * limit and not deferred):
        return
    key = f"nl:limit-warned:{now.astimezone(local_zone()).date().isoformat()}"
    if not redis_client.set(key, "1", nx=True, ex=2 * 86400):
        return
    recipient = vault.get("approval_notify_email")
    if not recipient:
        return
    try:
        mail.send_transactional(
            recipient, "", "[ShortsBlog] Newsletter perto do limite diário do Brevo",
            f"<p>A newsletter já usou {used} de {limit} e-mails nas últimas 24 h"
            + (f"; {deferred} assinantes ficaram para a próxima execução" if deferred else "")
            + ". Rever o plano do Brevo (D-OPEN-12).</p>")
    except mail.MailError as exc:
        log.warning("Newsletter: aviso de limite não saiu (%s)", type(exc).__name__)


# --- Painel (E17.8) -------------------------------------------------------------------------------


def panel_summary(db: Session, now: datetime | None = None) -> dict[str, Any]:
    """Contagens para o painel Saúde: assinantes por estado e as últimas edições. Sem e-mails."""
    moment = now or datetime.now(UTC)
    by_status: dict[str, int] = {str(k): int(v) for k, v in db.execute(text(
        "SELECT status, count(*) FROM subscribers GROUP BY status")).all()}
    editions = [dict(r) for r in db.execute(text(
        """SELECT edition_date,
                  count(*) FILTER (WHERE status = 'sent') AS sent,
                  count(*) FILTER (WHERE status = 'failed') AS failed,
                  count(*) FILTER (WHERE status = 'unknown') AS unknown,
                  max(posts) AS posts
             FROM newsletter_sends WHERE edition_date >= :cut
            GROUP BY edition_date ORDER BY edition_date DESC LIMIT 7"""),
        {"cut": (moment - timedelta(days=30)).date()}).mappings()]
    return {"enabled": settings.newsletter_enabled,
            "status": {k: by_status.get(k, 0) for k in
                       ("pending", "confirmed", "unsubscribed", "suppressed")},
            "editions": editions, "used_24h": sent_today(db, moment),
            "limit": settings.newsletter_daily_limit}


# --- Texto dos e-mails ----------------------------------------------------------------------------


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def _button(url: str, label: str) -> str:
    return (f'<a href="{_esc(url)}" style="display:inline-block;padding:10px 18px;'
            f'background:{_esc(settings.brand_primary_color)};color:#fff;text-decoration:none;'
            f'border-radius:6px;font-size:15px">{_esc(label)}</a>')


def _layout(language: str, inner: str, footer: str = "") -> str:
    words = COPY[language]
    return (
        f'<div style="font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#1a1a2e;'
        f'max-width:600px;margin:0 auto;padding:16px;line-height:1.5">'
        f'<p style="margin:0 0 16px;font-size:18px;font-weight:600">{_esc(settings.brand_name)}</p>'
        f'{inner}'
        f'<hr style="border:none;border-top:1px solid #ddd;margin:24px 0 12px">'
        f'<p style="margin:0;color:#666;font-size:12px">{_esc(words["sender"])}</p>{footer}</div>')


# Cor do selo de cada blog no e-mail (fallback: a cor da marca).
SITE_COLORS = {"cloud": "#0b3d91", "ia": "#6d28d9", "financas": "#047857",
               "culinaria": "#a84714", "saude": "#0f7f86", "politica": "#b91c1c"}

_WEEKDAYS = {
    "pt-BR": ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira",
              "sexta-feira", "sábado", "domingo"),
    "en": ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"),
    "es": ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"),
    "zh-CN": ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"),
}
_MONTHS = {
    "pt-BR": ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
              "agosto", "setembro", "outubro", "novembro", "dezembro"),
    "en": ("January", "February", "March", "April", "May", "June", "July",
           "August", "September", "October", "November", "December"),
    "es": ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
           "agosto", "septiembre", "octubre", "noviembre", "diciembre"),
}


def _date_label(language: str, day: Any) -> str:
    weekday = _WEEKDAYS[language][day.weekday()]
    if language == "zh-CN":
        return f"{day.year}年{day.month}月{day.day}日 · {weekday}"
    month = _MONTHS[language][day.month - 1]
    if language == "en":
        return f"{weekday} · {month} {day.day}, {day.year}"
    return f"{weekday} · {day.day} de {month} de {day.year}"


def _trim(value: str, limit: int) -> str:
    """Encurta no fim de palavra (ou no duro, p/ idiomas sem espaço), com reticência."""
    if len(value) <= limit:
        return value
    cut = value[:limit].rsplit(" ", 1)[0].rstrip(" ,;:·—-")
    return (cut or value[:limit].rstrip()) + "…"


def _join(words: dict[str, str], names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + f" {words['and']} " + names[-1]


def _subject(words: dict[str, str], titles: list[str], edition: Any) -> str:
    """Assunto com os títulos do dia (opção A da proposta de 4/11): o 1.º sempre; o 2.º se
    couber; o resto vira «e mais N». Sem posto, fica o assunto antigo com a data."""
    if not titles:
        return words["edition_subject"].format(date=edition.isoformat())
    subject = _trim(titles[0], 70)
    rest = len(titles) - 1
    if rest and len(subject) <= 58:
        second = _trim(titles[1], 96 - len(subject) - 3)
        if len(second) >= 12:  # um toco menor que isto não ajuda a abrir o e-mail
            subject = f"{subject} · {second}"
            rest -= 1
    if rest:
        subject += words["subject_more"].format(n=rest)
    return subject


def _edition_email(sub: Any, posts: list[dict[str, Any]], edition: Any) -> tuple[str, str, str,
                                                                                   str]:
    words = COPY[sub.language]
    token = make_token(sub.id, "unsubscribe", version=0, digest=sub.email_hash)
    unsubscribe_url = link(sub.base_url, sub.language, "unsubscribe", token)
    chosen = [(post, item) for post in posts
              if (item := _in_language(post, sub.language)) is not None]
    cards, lines = [], []
    for position, (post, item) in enumerate(chosen):
        color = SITE_COLORS.get(str(post.get("site_key") or ""), settings.brand_primary_color)
        cover = ""
        if item.get("cover"):
            cover = (f'<a href="{_esc(item["url"])}">'
                     f'<img src="{_esc(item["cover"])}" alt="{_esc(item.get("cover_alt", ""))}"'
                     f' width="488" border="0" style="display:block;width:100%;height:auto;'
                     f'border-radius:8px;margin:10px 0 2px"></a>')
        border = "" if position == 0 else "border-top:1px solid #edeff4;"
        cards.append(
            f'<tr><td style="padding:18px 28px;{border}">'
            f'<span style="display:inline-block;background:{color};color:#ffffff;font-size:11px;'
            f'font-weight:700;letter-spacing:.07em;text-transform:uppercase;padding:3px 9px;'
            f'border-radius:999px">{_esc(post["site"])}</span>{cover}'
            f'<p style="margin:9px 0 5px;font-size:17px;font-weight:600;line-height:1.35">'
            f'<a href="{_esc(item["url"])}" style="color:#15192c;text-decoration:none">'
            f'{_esc(item["title"])}</a></p>'
            f'<p style="margin:0 0 9px;color:#4a5066;font-size:14px">{_esc(item["excerpt"])}</p>'
            f'<a href="{_esc(item["url"])}" style="color:{color};font-weight:600;font-size:14px;'
            f'text-decoration:none">{_esc(words["read"])} &#8594;</a></td></tr>')
        lines.append(f"{post['site']}: {item['title']}\n{item['url']}\n")
    subject = _subject(words, [item["title"] for _, item in chosen], edition)
    count = (words["one_post"] if len(chosen) == 1
             else words["n_posts"].format(n=len(chosen)))
    preheader = words["preheader"].format(
        count=count, sites=_join(words, list(dict.fromkeys(p["site"] for p, _ in chosen))))
    brand = settings.brand_primary_color
    body = (
        f'<div style="display:none;max-height:0;overflow:hidden;mso-hide:all">'
        f'{_esc(preheader)}</div>'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"'
        f' style="background:#eef0f4"><tr><td align="center" style="padding:20px 8px">'
        f'<table role="presentation" width="600" cellpadding="0" cellspacing="0"'
        f' style="width:100%;max-width:600px;background:#ffffff;border-radius:10px;'
        f'overflow:hidden;font-family:system-ui,-apple-system,Segoe UI,sans-serif">'
        f'<tr><td style="background:{brand};padding:22px 28px">'
        f'<p style="margin:0;color:#ffffff;font-size:22px;font-weight:700">'
        f'{_esc(settings.brand_name)}</p>'
        f'<p style="margin:4px 0 0;color:#b9c9ea;font-size:12px;letter-spacing:.06em;'
        f'text-transform:uppercase">{_esc(_date_label(sub.language, edition))} · '
        f'{_esc(count)}</p></td></tr>'
        f'<tr><td style="padding:20px 28px 4px;color:#3c4257;font-size:15px">'
        f'{_esc(words["edition_intro"])}</td></tr>'
        + "".join(cards)
        + f'<tr><td style="background:#f6f7fa;padding:16px 28px 20px;color:#7a8097;'
        f'font-size:12px">{_esc(words["sender"])}<br>{_esc(words["why"])} '
        f'<a href="{_esc(unsubscribe_url)}" style="color:#7a8097">{_esc(words["leave"])}</a>'
        f'</td></tr></table></td></tr></table>')
    text_body = (f"{words['edition_intro']}\n\n" + "\n".join(lines)
                 + f"\n{words['why']} {words['leave']}: {unsubscribe_url}\n")
    return subject, body, text_body, unsubscribe_url


COPY: dict[str, dict[str, str]] = {
    "pt-BR": {
        "confirm_subject": "Confirme a sua inscrição na newsletter do ShortsBlog",
        "confirm_intro": "Recebemos um pedido para inscrever este e-mail na newsletter diária do "
                         "ShortsBlog: um e-mail por dia com os links dos posts publicados nos "
                         "nossos blogs. Para confirmar, clique no botão.",
        "confirm_button": "Confirmar inscrição",
        "confirm_expire": "O link vale por {hours} horas.",
        "confirm_ignore": "Se não foi você, ignore este e-mail: sem a confirmação, nada será "
                          "enviado.",
        "edition_subject": "ShortsBlog — os posts de {date}",
        "edition_intro": "Os posts mais recentes dos blogs do ShortsBlog:",
        "subject_more": " — e mais {n}",
        "preheader": "{count} nos blogs do ShortsBlog: {sites}.",
        "one_post": "1 post novo",
        "n_posts": "{n} posts novos",
        "read": "Ler no blog",
        "and": "e",
        "why": "Você recebe este e-mail porque se inscreveu na newsletter do ShortsBlog.",
        "leave": "Cancelar a inscrição",
        "sender": "ShortsBlog — newsletter diária dos blogs ShortsBlog.",
    },
    "en": {
        "confirm_subject": "Confirm your ShortsBlog newsletter subscription",
        "confirm_intro": "We received a request to subscribe this address to the ShortsBlog daily "
                         "newsletter: one e-mail a day with links to the posts published on our "
                         "blogs. To confirm, click the button.",
        "confirm_button": "Confirm subscription",
        "confirm_expire": "The link is valid for {hours} hours.",
        "confirm_ignore": "If this wasn't you, ignore this e-mail: without confirmation, nothing "
                          "will be sent.",
        "edition_subject": "ShortsBlog — posts of {date}",
        "edition_intro": "The latest posts on the ShortsBlog blogs:",
        "subject_more": " — and {n} more",
        "preheader": "{count} on the ShortsBlog blogs: {sites}.",
        "one_post": "1 new post",
        "n_posts": "{n} new posts",
        "read": "Read the post",
        "and": "and",
        "why": "You are receiving this e-mail because you subscribed to the ShortsBlog newsletter.",
        "leave": "Unsubscribe",
        "sender": "ShortsBlog — daily newsletter of the ShortsBlog blogs.",
    },
    "es": {
        "confirm_subject": "Confirma tu suscripción al boletín de ShortsBlog",
        "confirm_intro": "Recibimos una solicitud para suscribir esta dirección al boletín diario "
                         "de ShortsBlog: un correo al día con los enlaces de los posts publicados "
                         "en nuestros blogs. Para confirmar, haz clic en el botón.",
        "confirm_button": "Confirmar suscripción",
        "confirm_expire": "El enlace es válido durante {hours} horas.",
        "confirm_ignore": "Si no fuiste tú, ignora este correo: sin la confirmación, no se "
                          "enviará nada.",
        "edition_subject": "ShortsBlog — los posts del {date}",
        "edition_intro": "Los posts más recientes de los blogs de ShortsBlog:",
        "subject_more": " — y {n} más",
        "preheader": "{count} en los blogs de ShortsBlog: {sites}.",
        "one_post": "1 post nuevo",
        "n_posts": "{n} posts nuevos",
        "read": "Leer en el blog",
        "and": "y",
        "why": "Recibes este correo porque te suscribiste al boletín de ShortsBlog.",
        "leave": "Cancelar la suscripción",
        "sender": "ShortsBlog — boletín diario de los blogs ShortsBlog.",
    },
    "zh-CN": {
        "confirm_subject": "请确认订阅 ShortsBlog 简报",
        "confirm_intro": "我们收到将此邮箱订阅 ShortsBlog 每日简报的请求:每天一封邮件,"
                         "包含我们各博客当天发布文章的链接。请点击按钮确认。",
        "confirm_button": "确认订阅",
        "confirm_expire": "该链接在 {hours} 小时内有效。",
        "confirm_ignore": "如果不是您本人操作,请忽略此邮件:未经确认,我们不会发送任何内容。",
        "edition_subject": "ShortsBlog — {date} 的文章",
        "edition_intro": "ShortsBlog 各博客的最新文章:",
        "subject_more": "——另有 {n} 篇",
        "preheader": "ShortsBlog 各博客{count}:{sites}。",
        "one_post": "新文章 1 篇",
        "n_posts": "新文章 {n} 篇",
        "read": "阅读全文",
        "and": "和",
        "why": "您收到此邮件是因为您订阅了 ShortsBlog 简报。",
        "leave": "退订",
        "sender": "ShortsBlog — ShortsBlog 博客每日简报。",
    },
}
