"""E-mails ao Euclides (E10.5): pedidos de aprovação por lotes e resumo diário às 21:00.

Os dois chegam por rotas que o n8n chama (`approval-batch` às 06:30, 10:00, 13:00, 16:00 e
20:00; `digest` às 21:00, no fuso do motor — `TZ`, `core/localtime.py`; a nossa produção usa
America/Sao_Paulo). Cada envio tem uma chave única na base (dia,
hora do lote, destinatário): chamar duas vezes não manda dois e-mails. Um resultado incerto
(`SEND_UNKNOWN`) fica registado como `unknown` e não se repete.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import session_scope
from app.core.localtime import local_zone
from app.core.logging import get_logger
from app.services import (
    approval_links,
    feed_health,
    read_failures,
    reserve,
    vault,
    wp_updates,
)
from app.services.notify import mail
from app.services.notify.sends import Sender
from app.services.notify.sends import claim as _claim
from app.services.notify.sends import deliver as _deliver

log = get_logger(__name__)

_FONT = "font-family:system-ui,-apple-system,Segoe UI,sans-serif;color:#1a1a2e"
_PUBLISHED, _REJECTED = "PUBLISHED_CONFIRMED", "REJECTED"
_PENDING = ("AWAITING_APPROVAL", "APPROVED", "PUBLISHING", "VERIFY_REQUIRED")
_BLOCKED = ("BLOCKED", "RETRY_WAIT")
_FAILED = ("FAILED", "EXPIRED")


def _esc(value: object) -> str:
    return html.escape(str(value if value is not None else ""))


def slot_of(local: datetime) -> str:
    """Hora do lote (`HH:00` ou `HH:30`): identifica o envio, mesmo que o n8n chegue atrasado."""
    return f"{local:%H}:{'30' if local.minute >= 30 else '00'}"


def _one_line(reason: object) -> str:
    """O motivo num só fôlego: a parte antes do « | » (o resto é diagnóstico), até 160 letras.

    O `blocked_reason` completo continua no job e no painel; o e-mail só precisa de dizer
    o bastante para o Euclides saber que há um job parado e porquê (caso do 194: 35 h
    bloqueado sem ninguém saber, porque o lote só listava os à espera de aprovação).
    """
    head = " ".join(str(reason or "").split()).split(" | ")[0]
    if len(head) <= 160:
        return head
    return head[:159].rsplit(" ", 1)[0] + "…"


_GONE = {"BLOCKED": "bloqueado", "RETRY_WAIT": "à espera de nova tentativa",
         "FAILED": "falhou", "EXPIRED": "expirou sem decisão"}


def _missing_jobs(db: Session, today: Any) -> list[dict[str, Any]]:
    """Jobs que deviam estar no lote e não estão: parados num estado de problema.

    BLOCKED e RETRY_WAIT de hoje e de ontem (continuam acionáveis até expirarem);
    FAILED e EXPIRED só de hoje, para não repetir o desfecho em todos os lotes seguintes.
    A reserva guardada (D-115) não é slot: os problemas dela não entram aqui.
    """
    return [dict(r) for r in db.execute(
        text("""SELECT j.id, j.site_key, j.status, j.local_date, j.blocked_reason,
                       (SELECT title FROM job_translations t
                         WHERE t.job_id = j.id AND t.language = 'pt-BR') AS title
                  FROM publication_jobs j JOIN sites s ON s.site_key = j.site_key
                 WHERE s.enabled AND NOT j.reserve AND (
                       (j.status IN ('BLOCKED', 'RETRY_WAIT') AND j.local_date >= :yesterday)
                    OR (j.status IN ('FAILED', 'EXPIRED') AND j.local_date = :today))
                 ORDER BY j.site_key, j.id"""),
        {"today": today, "yesterday": today - timedelta(days=1)},
    ).mappings()]


def _button(url: str, label: str, color: str) -> str:
    return (
        f'<a href="{_esc(url)}" style="display:inline-block;margin:4px 6px 0 0;padding:8px 14px;'
        f'background:{color};color:#fff;text-decoration:none;border-radius:6px;font-size:14px">'
        f"{_esc(label)}</a>"
    )


# --- Pedidos de aprovação -----------------------------------------------------------------------


def approval_batch(now: datetime | None = None, send: Sender | None = None) -> dict[str, Any]:
    """Um e-mail com os artigos à espera de decisão, com os links Aprovar, Rejeitar e Ler."""
    recipient = vault.get("approval_notify_email")
    if not recipient:
        return {"sent": False, "reason": "APPROVAL_NOTIFY_EMAIL não configurado"}
    moment = now or datetime.now(UTC)
    local = moment.astimezone(local_zone())
    slot = slot_of(local)
    where = {"local_date": local.date(), "slot": slot, "recipient": recipient}
    with session_scope() as db:
        rows = (
            db.execute(
                text("""SELECT j.id, j.site_key, j.review_report, j.reserve, j.local_date,
                           NOT j.reserve AND j.local_date < (now() AT TIME ZONE s.timezone)::date
                               AS late,
                           (SELECT title FROM job_translations t
                             WHERE t.job_id = j.id AND t.language = 'pt-BR') AS title
                      FROM publication_jobs j JOIN sites s ON s.site_key = j.site_key
                     WHERE j.status = 'AWAITING_APPROVAL'
                     ORDER BY j.site_key, j.id""")
            )
            .mappings()
            .all()
        )
        # Os que não vieram também contam (8/11): um lote vazio com um job bloqueado
        # não é «nada pendente», é um problema que o Euclides tem de ver.
        missing = _missing_jobs(db, local.date())
        reserves = reserve.overview(db) if missing else {}
        if not rows and not missing:
            return {"sent": False, "reason": "nada pendente", "jobs": 0}
        # O payload do claim vira colunas de `approval_batches`: `missing` não entra lá,
        # fica só no resultado e no e-mail.
        if not _claim(db, "approval_batches", where, {"status": "pending", "jobs": len(rows)}):
            return {"sent": False, "reason": f"o lote das {slot} já foi enviado"}
        base = settings.public_base_url.rstrip("/")
        items: list[tuple[dict[str, Any], str, str]] = []
        for row in rows:
            approve = approval_links.create(db, row["id"], "approve", moment)
            reject = approval_links.create(db, row["id"], "reject", moment)
            items.append((dict(row), f"{base}/a/{approve}", f"{base}/a/{reject}"))
    body = _approval_html(items, base, missing, reserves)
    count = len(items)
    if count:
        subject = (
            f"{count} artigo{'s' if count > 1 else ''} à espera da sua aprovação"
            f" | {local:%Y-%m-%d} {slot}"
        )
        if missing:
            subject += f" | {len(missing)} não {'vieram' if len(missing) > 1 else 'veio'}"
    else:
        subject = (
            f"Nenhum artigo à espera e {len(missing)} não "
            f"{'vieram' if len(missing) > 1 else 'veio'} | {local:%Y-%m-%d} {slot}"
        )
    status = _deliver(
        "approval_batches",
        where,
        recipient,
        subject,
        body,
        send or mail.send_transactional,
        lambda ref: {"sent_at_utc": datetime.now(UTC)},
    )
    return {"sent": status == "sent", "status": status, "jobs": count, "slot": slot,
            "missing": len(missing)}


def _covered(row: dict[str, Any]) -> bool:
    return reserve.is_covered(row.get("blocked_reason"))


def _reserve_note(site_key: str, reserves: dict[str, dict[str, Any]]) -> str:
    """O que acontece com o horário deste blog se o job não sair (D-115)."""
    ready = reserves.get(site_key)
    if ready and ready["status"] == "APPROVED":
        return ("Se não sair a tempo, a reserva aprovada "
                f"«{_one_line(ready['title'] or '')}» publica no horário.")
    if ready and ready["status"] == "AWAITING_APPROVAL":
        return (f"A reserva deste blog (job #{ready['id']}) está à espera da sua aprovação "
                "neste lote: aprovada, cobre o horário se este job não sair.")
    if ready:
        return "A reserva deste blog ainda está a ser produzida."
    return "Este blog ainda não tem reserva."


def _approval_html(items: list[tuple[dict[str, Any], str, str]], base: str,
                   missing: list[dict[str, Any]] | None = None,
                   reserves: dict[str, dict[str, Any]] | None = None) -> str:
    parts = [f'<div style="{_FONT};max-width:620px">']
    if items:
        parts += [
            f'<h2 style="margin:0 0 4px">{len(items)} à espera da sua aprovação</h2>',
            '<p style="color:#667;font-size:14px;margin:0 0 18px">Nada é publicado sem o seu '
            "clique. Cada link abre uma página que pede confirmação, vale 72 horas e só "
            "funciona uma vez.</p>",
        ]
    else:
        parts.append('<h2 style="margin:0 0 4px">Nenhum artigo à espera neste lote</h2>')
    site = None
    for row, approve, reject in items:
        if row["site_key"] != site:
            site = row["site_key"]
            parts.append(
                f'<h3 style="margin:18px 0 6px;text-transform:capitalize">{_esc(site)}</h3>'
            )
        score = (row["review_report"] or {}).get("score")
        kind = (" · <b>reserva</b>: artigo atemporal guardado para o dia em que o post do blog "
                "não sair (D-115)" if row.get("reserve") else "")
        if row.get("late"):
            kind = (f" · <b>atrasado</b>: o dia dele ({row['local_date']:%d/%m}) já passou; "
                    "aprovado, não sai junto com o de hoje: só cobre um horário que falhar")
        parts.append(
            f'<div style="margin:0 0 14px;padding:12px;border:1px solid #e4e7ec;border-radius:8px">'
            f'<div style="font-size:16px;font-weight:600">'
            f"{_esc(row['title'] or '(sem título)')}</div>"
            f'<div style="color:#667;font-size:13px;margin:2px 0 6px">job #{row["id"]}'
            f"{f' · nota {score}' if score is not None else ''}{kind}</div>"
            + _button(approve, "Aprovar", "#0f9d58")
            + _button(reject, "Rejeitar", "#d63b3b")
            + _button(f"{base}/dashboard/review/{row['id']}", "Ler no painel", "#5f6672")
            + "</div>"
        )
    if missing:
        parts.append(
            '<h3 style="margin:22px 0 4px;color:#9a3412">Não vieram neste lote</h3>'
            '<p style="color:#667;font-size:13px;margin:0 0 8px">Jobs parados num estado '
            "de problema: não há nada para aprovar deles, mas convém saber porquê.</p>"
        )
    for row in missing or []:
        detail = f" — {_esc(_one_line(row['blocked_reason']))}" if row["blocked_reason"] else ""
        covered = _covered(row)
        state = (str(row["blocked_reason"]).split(" (job")[0] if covered
                 else _GONE.get(row["status"], row["status"]))
        note = "" if covered else (
            f'<div style="color:#365;font-size:13px;margin-top:4px">'
            f"{_esc(_reserve_note(row['site_key'], reserves or {}))}</div>")
        parts.append(
            f'<div style="margin:0 0 8px;padding:10px 12px;border:1px solid '
            f'{"#cfe3d4" if covered else "#f0c9c9"};'
            f'border-radius:8px;font-size:14px"><b style="text-transform:capitalize">'
            f'{_esc(row["site_key"])}</b> · job #{row["id"]} ({row["local_date"]:%d/%m}) · '
            f'{_esc(state)}'
            f'<div style="color:#667;font-size:13px;margin-top:2px">'
            f"{_esc(row['title'] or '(sem título)')}{detail}</div>{note}</div>"
        )
    parts.append("</div>")
    return "".join(parts)


# --- Resumo diário -------------------------------------------------------------------------------


def build_digest(db: Session, local_date: Any) -> dict[str, Any]:
    """Os números do dia, por blog (só leitura)."""
    sites = (
        db.execute(
            text("SELECT site_key, name, enabled, daily_target FROM sites ORDER BY site_key")
        )
        .mappings()
        .all()
    )
    jobs = (
        db.execute(
            text("""SELECT j.id, j.site_key, j.status, j.blocked_reason, j.wordpress_url,
                           j.published_at_utc,
                       (SELECT title FROM job_translations t
                         WHERE t.job_id = j.id AND t.language = 'pt-BR') AS title
                  FROM publication_jobs j WHERE j.local_date = :d AND NOT j.reserve
                 ORDER BY j.site_key, j.id"""),
            {"d": local_date},
        )
        .mappings()
        .all()
    )
    blogs = []
    for site in sites:
        # O slot coberto pela reserva (D-115) não é falha: o post da reserva conta como
        # publicado (ela assume a data do slot).
        mine = [dict(j) for j in jobs if j["site_key"] == site["site_key"]
                and not _covered(dict(j))]
        blogs.append(
            {
                "key": site["site_key"],
                "name": site["name"],
                "enabled": site["enabled"],
                "target": site["daily_target"],
                "published": [j for j in mine if j["status"] == _PUBLISHED],
                "pending": [j for j in mine if j["status"] in _PENDING],
                "blocked": [j for j in mine if j["status"] in _BLOCKED],
                "failed": [j for j in mine if j["status"] in _FAILED],
                "rejected": [j for j in mine if j["status"] == _REJECTED],
                "other": len(
                    [
                        j
                        for j in mine
                        if j["status"]
                        not in (_PUBLISHED, _REJECTED, *_PENDING, *_BLOCKED, *_FAILED)
                    ]
                ),
            }
        )
    return {
        "date": local_date,
        "blogs": blogs,
        "published": sum(len(b["published"]) for b in blogs),
        "target": sum(b["target"] for b in blogs),
        "wp_updates": wp_updates.summary(db),
        "feeds": feed_health.summary(db),
        "read_failures": read_failures.summary(),
    }


def _updates_html(info: dict[str, Any] | None) -> list[str]:
    """R47: plugins e temas com atualização à espera (só depois da 1.ª conferência)."""
    if info is None:
        return []
    parts = ['<h3 style="margin:22px 0 4px">Atualizações do WordPress</h3>']
    notes = []
    if info["stale"]:
        notes.append("a conferência (scripts/wp-updates-check.sh) não corre há mais de 3 dias")
    if info["failed"]:
        notes.append("sem resposta do wp-cli em: " + ", ".join(info["failed"]))
    if notes:
        parts.append(f'<p style="color:#9a3412;font-size:14px;margin:0 0 6px">'
                     f"⚠️ {_esc('; '.join(notes))}.</p>")
    if not info["items"]:
        parts.append('<p style="color:#667;font-size:14px;margin:0">Tudo em dia.</p>')
        return parts
    parts.append('<ul style="margin:0;padding-left:18px;font-size:14px">')
    for item in info["items"]:
        idle = " (inativo)" if item["status"] == "inactive" else ""
        where = ("todos os blogs" if len(item["sites"]) >= 6
                 else ", ".join(item["sites"]))
        parts.append(f"<li>{_esc(item['kind'])} <b>{_esc(item['name'])}</b>{_esc(idle)}: "
                     f"{_esc(item['version'])} → {_esc(item['update_version'])} "
                     f'<span style="color:#667">({_esc(where)})</span></li>')
    parts.append("</ul>")
    parts.append('<p style="color:#667;font-size:13px;margin:4px 0 0">Aplicar à mão, um blog de '
                 "cada vez, com o teste do blog a seguir (runbook do WordPress, R47).</p>")
    return parts


_FEED_ERRORS = {"http_status": "erro HTTP", "robots_disallowed": "o robots.txt proíbe o robô",
                "timeout": "sem resposta", "network_error": "falha de rede",
                "blocked_address": "endereço bloqueado", "too_large": "grande demais"}


def _days(count: int) -> str:
    return f"{count} dia" if count == 1 else f"{count} dias"


def _feeds_html(info: dict[str, list[dict[str, Any]]] | None) -> list[str]:
    """Fontes paradas (feed_health): erro seguido ou sem pauta nova há dias."""
    if not info or not (info["failing"] or info["silent"]):
        return []
    parts = ['<h3 style="margin:22px 0 4px">Fontes paradas</h3>']
    if info["failing"]:
        parts.append('<p style="color:#9a3412;font-size:14px;margin:0 0 4px">Com erro em todas '
                     "as leituras (a descoberta corre de hora em hora):</p>"
                     '<ul style="margin:0 0 8px;padding-left:18px;font-size:14px">')
        for row in info["failing"]:
            why = _FEED_ERRORS.get(row["status"], row["status"])
            parts.append(f"<li><b>{_esc(row['site'])}</b> · {_esc(row['name'])} — {_esc(why)}, "
                         f"{row['runs']} leituras seguidas (~{_days(row['runs'] // 24)})</li>")
        parts.append("</ul>")
    if info["silent"]:
        parts.append('<p style="color:#667;font-size:14px;margin:0 0 4px">Respondem, mas sem '
                     f"pauta nova há {settings.feed_silent_days} dias ou mais (pode ser só "
                     "pouca atividade):</p>"
                     '<ul style="margin:0;padding-left:18px;font-size:14px;color:#445">')
        for row in info["silent"]:
            when = _days(row["days"]) if row["days"] is not None else "nunca trouxe pauta"
            parts.append(f"<li>{_esc(row['site'])} · {_esc(row['name'])} ({_esc(when)})</li>")
        parts.append("</ul>")
    return parts


_READ_ERRORS = {**_FEED_ERRORS, "ai_disallowed": "robots.txt proíbe robôs de IA",
                "not_https": "redireciona para HTTP", "content_type": "não é página nem PDF",
                "no_text": "sem texto (paywall ou só JavaScript)", "http_403": "403 (bloqueio)",
                "http_202": "202 (desafio anti-robô)", "http_404": "404", "http_429": "429"}


def _read_failures_html(rows: list[dict[str, Any]] | None) -> list[str]:
    """Sítios cujas páginas-fonte mais falham na leitura (read_failures, últimos 7 dias)."""
    if not rows:
        return []
    parts = ['<h3 style="margin:22px 0 4px">Sítios que não se deixam ler</h3>',
             '<p style="color:#667;font-size:14px;margin:0 0 4px">Páginas-fonte que a pesquisa '
             "tentou ler nos últimos 7 dias (falhas · leituras certas). Um sítio que só falha "
             "pode virar «só pauta» (sem-leitura.yaml) para não gastar tentativas:</p>"
             '<ul style="margin:0;padding-left:18px;font-size:14px">']
    for row in rows:
        why = ", ".join(f"{_READ_ERRORS.get(k, k)} ×{n}" for k, n in row["kinds"].items())
        parts.append(f"<li><b>{_esc(row['site'])}</b> — {row['fails']} · {row['ok']} "
                     f'<span style="color:#667">({_esc(why)})</span></li>')
    parts.append("</ul>")
    return parts


def _digest_html(data: dict[str, Any]) -> str:
    parts = [
        f'<div style="{_FONT};max-width:680px">',
        f'<h2 style="margin:0 0 4px">Resumo diário · {data["date"]:%d/%m/%Y}</h2>',
        f'<p style="margin:0 0 14px;font-size:15px"><b>{data["published"]}</b> de '
        f"{data['target']} artigos publicados.</p>",
    ]
    if settings.wp_publish_mode != "live":
        parts.append(
            '<p style="background:#fff7e0;padding:8px 12px;border-radius:6px;font-size:14px">'
            "Modo rascunho: o que é aprovado cria rascunhos no WordPress e nada fica público "
            "até ao piloto (E14).</p>"
        )
    for b in data["blogs"]:
        parts.append(
            f'<h3 style="margin:18px 0 4px">{_esc(b["name"])} '
            f'<span style="color:#667;font-weight:400;font-size:13px">'
            f"({_esc(b['key'])})</span></h3>"
        )
        if not b["enabled"]:
            parts.append('<p style="color:#667;font-size:14px;margin:0">Blog desligado.</p>')
            continue
        line = f"{len(b['published'])} de {b['target']} publicados"
        if len(b["published"]) < b["target"]:
            line = f"⚠️ {line}"
        parts.append(
            f'<p style="margin:0 0 4px;font-size:14px"><b>{_esc(line)}</b></p><ul style="margin:0;'
            f'padding-left:18px;font-size:14px">'
        )
        for j in b["published"]:
            when = (
                j["published_at_utc"].astimezone(local_zone()).strftime("%H:%M")
                if j["published_at_utc"]
                else ""
            )
            parts.append(
                f'<li>{_esc(when)} <a href="{_esc(j["wordpress_url"] or "#")}">'
                f"{_esc(j['title'] or '(sem título)')}</a></li>"
            )
        for label, key in (
            ("à espera", "pending"),
            ("bloqueados", "blocked"),
            ("falhados", "failed"),
        ):
            for j in b[key]:
                why = f" — {_esc(_one_line(j['blocked_reason']))}" if j["blocked_reason"] else ""
                parts.append(
                    f'<li style="color:#667">{label}: {_esc(j["title"] or j["status"])}{why}</li>'
                )
        if b["rejected"]:
            parts.append(f'<li style="color:#667">{len(b["rejected"])} rejeitado(s) por si</li>')
        if b["other"]:
            parts.append(f'<li style="color:#667">{b["other"]} em produção ou planeado(s)</li>')
        parts.append("</ul>")
    parts += _updates_html(data.get("wp_updates"))
    parts += _feeds_html(data.get("feeds"))
    parts += _read_failures_html(data.get("read_failures"))
    parts.append("</div>")
    return "".join(parts)


def daily_digest(now: datetime | None = None, send: Sender | None = None) -> dict[str, Any]:
    """O resumo do dia. Chave única `(local_date, recipient)`: uma segunda chamada não reenvia."""
    recipient = vault.get("approval_notify_email")
    if not recipient:
        return {"sent": False, "reason": "APPROVAL_NOTIFY_EMAIL não configurado"}
    local = (now or datetime.now(UTC)).astimezone(local_zone())
    where = {"local_date": local.date(), "recipient": recipient}
    with session_scope() as db:
        if not _claim(db, "daily_digest", where, {"status": "pending"}):
            return {"sent": False, "reason": "o resumo de hoje já foi enviado"}
        data = build_digest(db, local.date())
    subject = (
        f"Resumo diário dos blogs | {local:%Y-%m-%d} | "
        f"{data['published']}/{data['target']} publicados"
    )
    status = _deliver(
        "daily_digest",
        where,
        recipient,
        subject,
        _digest_html(data),
        send or mail.send_transactional,
        lambda ref: {"message_reference": ref, "sent_at_utc": datetime.now(UTC)},
    )
    return {"sent": status == "sent", "status": status, "subject": subject}
