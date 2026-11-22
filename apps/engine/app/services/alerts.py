"""Alertas do motor por e-mail (E12.3): o que é preciso saber sem estar a olhar para o painel.

Cada verificação devolve o que está a falhar agora (`Firing`). O avaliador compara com o estado
guardado no Redis: avisa uma vez (quando a falha dura mais que o `for_s` da regra), repete de 6 em
6 horas enquanto durar e avisa a recuperação. Vários alertas novos no mesmo minuto vão num só
e-mail. Se uma verificação falhar, o estado dela fica como estava (não se declara recuperação por
não se ter conseguido ver). Corre no scheduler do motor, que não depende do n8n nem do Growth
Engine; o monitoramento da máquina continua a ser do Growth Engine.

Alertas: blog ou painel em baixo, provedor de IA desautenticado, fila parada, orçamento a 80 %,
certificado a menos de 14 dias, disco a mais de 85 %, Redis ou Postgres em baixo, backup
sem sucesso há mais de 26 h (só depois da 1.ª pulsação do backup, R52).
"""

from __future__ import annotations

import contextlib
import json
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.cache import redis_client
from app.core.config import settings
from app.core.db import engine, session_scope
from app.core.logging import get_logger
from app.services import backup_beat, probes, telemetry, vault
from app.services.ai import gateway
from app.services.notify import mail

log = get_logger(__name__)

REPEAT_S = 6 * 3600
RETRY_S = 600
BUDGET_THRESHOLD = 0.8
CERT_WARN_DAYS = 14
DISK_WARN_PCT = 85.0
_STATE = "sb:alerts:state"
Sender = Callable[[str, str, str, str], str]
_memory: dict[
    str, Any
] = {}  # só se o Redis estiver em baixo: o aviso do Redis tem de sair na mesma


@dataclass(frozen=True)
class Firing:
    key: str
    title: str
    detail: str
    for_s: int = 0  # só avisa se a falha durar pelo menos isto


# --- Verificações -------------------------------------------------------------------------------


def check_sites(db: Session) -> list[Firing]:
    out = []
    for name, result in probes.results(db).items():
        if result is not None and not result["ok"]:
            what = (
                f"HTTP {result['status']}"
                if result["status"]
                else (result["error"] or "sem resposta")
            )
            out.append(
                Firing(f"site_down:{name}", f"{name} fora do ar", f"{result['url']}: {what}", 120)
            )
    return out


def check_providers(db: Session) -> list[Firing]:
    rows = db.execute(
        text(
            "SELECT p.name, h.status FROM ai_providers p "
            "JOIN ai_provider_health h ON h.provider_id = p.id "
            "WHERE p.enabled AND h.status = 'not_authenticated'"
        )
    ).all()
    return [
        Firing(
            f"provider_auth:{name}",
            f"provedor de IA desautenticado: {name}",
            "Sem login, o motor não escreve nem revê. Ver o painel Provedores de IA.",
            600,
        )
        for name, _status in rows
    ]


def check_queue(db: Session) -> list[Firing]:
    stuck = {
        labels["kind"]: value
        for name, _h, labels, value in telemetry.samples(db)
        if name == "sb_jobs_stuck"
    }
    bad = [
        f"{kind}: {int(n)}"
        for kind, n in stuck.items()
        if kind in ("queued_waiting", "lease_expired") and n
    ]
    return (
        [Firing("queue_stopped", "fila parada", "Jobs presos (" + ", ".join(bad) + ")", 300)]
        if bad
        else []
    )


def check_budget(db: Session) -> list[Firing]:
    return [
        Firing(
            f"budget:{labels['scope']}:{labels['name']}:{labels['period']}",
            f"orçamento a {int(value * 100)} %: {labels['name']} ({labels['period']})",
            "Chegou a 80 % do teto; ao atingir 100 % o motor deixa de usar este provedor ou blog.",
        )
        for name, _h, labels, value in telemetry.budget_ratios(db)
        if value >= BUDGET_THRESHOLD
    ]


def check_certs(db: Session) -> list[Firing]:
    days = probes.cert_days(probes.hostnames(db))
    return [
        Firing(
            f"cert:{host}",
            f"certificado de {host} a {int(d)} dias",
            "Renovação automática do Cloudflare/Traefik falhou? Ver o runbook de Cloudflare.",
        )
        for host, d in days.items()
        if d < CERT_WARN_DAYS
    ]


def check_disk(db: Session) -> list[Firing]:
    usage = shutil.disk_usage("/")
    pct = usage.used / usage.total * 100
    return (
        [
            Firing(
                "disk_high", f"disco a {pct:.0f} %", "Limpar imagens Docker e volumes antigos.", 300
            )
        ]
        if pct > DISK_WARN_PCT
        else []
    )


def check_infra(db: Session) -> list[Firing]:
    out = []
    try:
        redis_client.ping()
    except Exception as exc:  # noqa: BLE001
        out.append(Firing("redis_down", "Redis em baixo", type(exc).__name__, 60))
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        out.append(Firing("postgres_down", "Postgres em baixo", type(exc).__name__, 60))
    return out


def check_backup(db: Session, now: float | None = None) -> list[Firing]:
    """R52: o backup diário parou? Só vale depois da 1.ª pulsação (opt-in do operador)."""
    last = backup_beat.last_ok(db)
    if last is None:
        return []
    hours = ((now or time.time()) - last) / 3600
    if hours <= settings.backup_alert_hours:
        return []
    return [Firing("backup_stale", f"backup sem sucesso há {hours:.0f} h",
                   "Ver o backup.log e o timer sb-backup (systemctl --user status sb-backup).")]


def check_ai_storage(db: Session) -> list[Firing]:
    """R31: algum volume de estado das CLIs passou do teto? Gateway mudo = sem alerta aqui
    (a queda dos provedores já avisa por `provider_auth`)."""
    try:
        with httpx.Client(transport=gateway._transport, timeout=15) as client:
            response = client.get(settings.ai_gateway_url.rstrip("/") + "/health/storage")
        sizes = response.json() if response.status_code == 200 else {}
    except (httpx.HTTPError, ValueError):
        return []
    limit = settings.ai_storage_alert_mb * 1024 * 1024
    out = []
    for cli, size in sorted(sizes.items()) if isinstance(sizes, dict) else []:
        if isinstance(size, int) and size > limit:
            out.append(Firing(
                f"ai_storage:{cli}", f"volume da CLI {cli} com {size // (1024 * 1024)} MB",
                f"Acima de {settings.ai_storage_alert_mb} MB. A limpeza diária do gateway "
                "(R31, `housekeeping.RULES`) só apaga pastas conhecidas: ver o que cresceu "
                f"com `docker run --rm -v sb_ai_{cli}:/v:ro alpine du -d 3 -h /v`."))
    return out


CHECKS: dict[str, Callable[[Session], list[Firing]]] = {
    "site_down": check_sites,
    "provider_auth": check_providers,
    "queue_stopped": check_queue,
    "budget": check_budget,
    "cert": check_certs,
    "disk_high": check_disk,
    "infra": check_infra,
    "backup": check_backup,
    "ai_storage": check_ai_storage,
}
_PREFIXES = {
    "site_down": ("site_down:",),
    "provider_auth": ("provider_auth:",),
    "queue_stopped": ("queue_stopped",),
    "budget": ("budget:",),
    "cert": ("cert:",),
    "disk_high": ("disk_high",),
    "infra": ("redis_down", "postgres_down"),
    "backup": ("backup_stale",),
    "ai_storage": ("ai_storage:",),
}


# --- Estado -------------------------------------------------------------------------------------


def _load() -> dict[str, dict[str, Any]]:
    try:
        raw = redis_client.get(_STATE)
        return json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return dict(_memory.get("state", {}))


def _save(state: dict[str, dict[str, Any]]) -> None:
    _memory["state"] = state
    with contextlib.suppress(Exception):
        redis_client.set(_STATE, json.dumps(state))


def active() -> dict[str, dict[str, Any]]:
    """Alertas em curso (para o painel): chave → estado."""
    return _load()


# --- Avaliação ----------------------------------------------------------------------------------


def _email(sender: Sender, subject: str, items: list[tuple[str, str]], footer: str) -> str:
    style = "font-family:system-ui,sans-serif;max-width:600px"
    body = "".join(
        f'<li><b>{title}</b><br><span style="color:#667">{detail}</span></li>'
        for title, detail in items
    )
    note = f'<p style="color:#667;font-size:13px">{footer}</p>'
    html = f'<div style="{style}"><ul style="padding-left:18px">{body}</ul>{note}</div>'
    return sender(vault.get("approval_notify_email"), "", subject, html)


def evaluate(
    now: float | None = None,
    send: Sender | None = None,
    checks: dict[str, Callable[[Session], list[Firing]]] | None = None,
) -> dict[str, Any]:
    moment = now or time.time()
    sender = send or mail.send_transactional
    state = _load()
    firing: dict[str, Firing] = {}
    kept: set[str] = set()  # chaves cuja verificação falhou: o estado fica como estava
    for name, check in (checks or CHECKS).items():
        try:
            with session_scope() as db:
                for item in check(db):
                    firing[item.key] = item
        except Exception:  # noqa: BLE001 — uma verificação avariada não derruba as outras
            log.exception("Alerta %s: a verificação falhou", name)
            kept.update(k for k in state if k.startswith(_PREFIXES.get(name, (name,))))
    to_send: list[str] = []
    for key, item in firing.items():
        st = state.setdefault(key, {"first": moment, "alerted": None, "attempt": 0})
        if moment - st["first"] < item.for_s:
            continue
        wait, since = (
            (RETRY_S, st["attempt"]) if st["alerted"] is None else (REPEAT_S, st["alerted"])
        )
        if moment - since >= wait:
            to_send.append(key)
    resolved = [
        k
        for k, st in state.items()
        if k not in firing and k not in kept and st["alerted"] is not None
    ]
    result: dict[str, Any] = {"firing": sorted(firing), "sent": [], "resolved": resolved}
    if to_send and vault.get("approval_notify_email"):
        for key in to_send:
            state[key]["attempt"] = moment
        items = [(firing[k].title, firing[k].detail) for k in to_send]
        subject = f"ShortsBlog: {len(items)} alerta{'s' if len(items) > 1 else ''} — {items[0][0]}"
        try:
            _email(
                sender,
                subject,
                items,
                "O aviso repete-se de 6 em 6 horas e quando voltar ao normal.",
            )
            outcome = "avisou"
        except mail.MailUnknown as exc:
            log.warning("Alertas: resultado incerto do envio (%s)", exc)
            outcome = "incerto"
        except mail.MailError as exc:
            log.warning("Alertas: envio falhou, repete em %d min (%s)", RETRY_S // 60, exc)
            outcome = "falhou"
        if outcome in ("avisou", "incerto"):
            for key in to_send:
                state[key]["alerted"] = moment
            result["sent"] = to_send
            log.warning("Alertas enviados: %s", ", ".join(to_send))
    if resolved and vault.get("approval_notify_email"):
        try:
            plural = "s" if len(resolved) > 1 else ""
            _email(
                sender,
                f"ShortsBlog: {len(resolved)} alerta{plural} resolvido{plural}",
                [(k, "voltou ao normal") for k in resolved],
                "",
            )
            log.warning("Alertas resolvidos avisados: %s", ", ".join(resolved))
        except mail.MailError as exc:
            log.warning("Aviso de resolução falhou: %s", exc)
    for key in [k for k in list(state) if k not in firing and k not in kept]:
        del state[key]
    _save(state)
    return result
