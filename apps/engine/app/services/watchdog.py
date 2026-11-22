"""Vigia do agendador (E11.4): avisa se os horários deixarem de chamar o motor.

Na nossa instalação o n8n é quem faz o motor andar; no pacote (E19.2) é o agendador interno
(`INTERNAL_SCHEDULER_ENABLED=true`), que regista as chamadas do mesmo modo. Se ele parar, ou se
alguém desativar os workflows, nada é planeado nem publicado e ninguém dá por isso. Cada rota
`/api/sched/*` regista a sua última chamada no Redis; o scheduler do motor (outro processo, que não
depende do n8n) corre `check` a cada minuto e manda um e-mail quando uma rota passa o limite,
repete de 6 em 6 horas enquanto durar e avisa quando recupera.

Só arma depois da 1.ª chamada de `dispatch`: enquanto o n8n nunca falou com o motor, não há nada a
vigiar. Não cobre a falha do servidor inteiro (para isso há o monitoramento da máquina) nem a
perda do Redis (o vigia fica desarmado até ao próximo `dispatch`).
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from app.core.cache import redis_client
from app.core.config import settings
from app.core.logging import get_logger
from app.services import vault
from app.services.notify import mail

log = get_logger(__name__)

# Segundos sem chamada a partir dos quais se considera parado (o n8n chama a cada 1 min, 5 min, 1 h;
# o `plan-day` corre 1x por dia às 00:05 e o `digest` às 21:00 (no fuso do motor, `TZ`), daí as
# 25 h; o `approval-batch` corre às 06:30, 10:00, 13:00, 16:00 e 20:00 (D-066), o maior intervalo é
# de 10,5 h; ficam as 16 h; a `newsletter` (E17.5) corre 1x por dia: 25 h)
LIMITS = {"dispatch": 300, "publish-due": 300, "reconcile": 900, "discover": 4 * 3600,
          "plan-day": 25 * 3600, "digest": 25 * 3600, "approval-batch": 16 * 3600}
# Só vigiadas quando a função está ligada (senão o n8n ainda nem tem o workflow).
OPTIONAL_LIMITS = {"newsletter": 25 * 3600}
ARM_ROUTE = "dispatch"
REPEAT_S = 6 * 3600          # repete o aviso enquanto durar
RETRY_S = 600                # se o e-mail falhar de certeza, tenta de novo daqui a 10 minutos
_STATE = "sb:watchdog:state"
_ARMED_AT = "sb:watchdog:armed_at"
_ON_SINCE = "sb:watchdog:on_since:"     # desde quando o vigia vê ligada cada rota opcional
Sender = Callable[[str, str, str, str], str]


def record(route: str, now: float | None = None) -> None:
    """Regista a chamada de uma rota do agendador (o n8n está vivo e a falar com o motor)."""
    moment = now or time.time()
    redis_client.set(f"sb:sched:last:{route}", str(moment))
    if route == ARM_ROUTE:
        redis_client.setnx(_ARMED_AT, str(moment))     # desde quando se vigia


def ages(now: float | None = None) -> dict[str, float | None]:
    moment = now or time.time()
    out: dict[str, float | None] = {}
    for route in limits():
        raw = redis_client.get(f"sb:sched:last:{route}")
        out[route] = round(moment - float(raw), 1) if raw else None
    return out


def limits() -> dict[str, int]:
    extra = {"newsletter": OPTIONAL_LIMITS["newsletter"]} if settings.newsletter_enabled else {}
    return {**LIMITS, **extra}


def _on_for(moment: float) -> dict[str, float]:
    """Há quanto tempo cada rota opcional está ligada. Desligar esquece: religar conta do zero."""
    out: dict[str, float] = {}
    for route in OPTIONAL_LIMITS:
        key = _ON_SINCE + route
        if route in limits():
            redis_client.setnx(key, str(moment))
            out[route] = moment - float(redis_client.get(key) or moment)
        else:
            redis_client.delete(key)
    return out


def status(now: float | None = None) -> dict[str, Any]:
    """`armed`: já houve um `dispatch`. `stale`: rotas acima do limite. `ok`: armado e em dia."""
    moment = now or time.time()
    seen = ages(moment)
    limit = limits()
    armed = seen[ARM_ROUTE] is not None
    # uma rota opcional acabada de ligar não conta como parada antes de passar o limite desde então
    on_for = _on_for(moment)
    stale = [r for r, age in seen.items()
             if armed and age is not None and min(age, on_for.get(r, age)) > limit[r]]
    # uma rota ainda por chamar só conta como parada depois de vigiada mais tempo que o seu limite
    raw = redis_client.get(_ARMED_AT)
    watched = moment - float(raw) if raw else 0.0
    stale += [r for r, age in seen.items()
              if armed and age is None and min(watched, on_for.get(r, watched)) > limit[r]]
    return {"armed": armed, "ok": armed and not stale, "stale": sorted(set(stale)), "ages": seen}


def _load_state() -> dict[str, Any]:
    raw = redis_client.get(_STATE)
    return json.loads(raw) if raw else {}


def _save_state(state: dict[str, Any]) -> None:
    redis_client.set(_STATE, json.dumps(state))


def _fmt(seconds: float | None) -> str:
    if seconds is None:
        return "nunca"
    return f"{int(seconds // 60)} min" if seconds < 7200 else f"{seconds / 3600:.1f} h"


def _send(send: Sender, subject: str, lines: list[str]) -> str:
    style = "font-family:system-ui,sans-serif;max-width:560px"
    html = (f'<div style="{style}"><h2 style="margin:0 0 8px">{subject}</h2>'
            + "".join(f"<p>{line}</p>" for line in lines) + "</div>")
    return send(vault.get("approval_notify_email"), "", f"ShortsBlog: {subject}", html)


def _who() -> str:
    return "agendador interno" if settings.internal_scheduler_enabled else "n8n"


def _hint() -> str:
    """O que olhar primeiro, conforme quem agenda nesta instalação."""
    if settings.internal_scheduler_enabled:
        return ("Veja se o container <code>sb-engine-scheduler</code> está de pé e os logs dele "
                "(<code>docker logs sb-engine-scheduler</code>).")
    where = f" (<code>{settings.n8n_url}</code>)" if settings.n8n_url else ""
    return f"Veja se o n8n está de pé{where} e se os workflows <b>SB-*</b> estão ativos."


def check(now: float | None = None, send: Sender | None = None) -> dict[str, Any]:
    """Avalia e, se for caso, avisa. Chamado pelo scheduler a cada minuto."""
    moment = now or time.time()
    current = status(moment)
    if not current["armed"]:
        return {"action": "desarmado"}
    if not vault.get("approval_notify_email"):
        return {"action": "sem destinatário", "ok": current["ok"]}
    sender = send or mail.send_transactional
    state = _load_state()
    stale = current["stale"]
    if stale:
        state.setdefault("down_since", moment)
        last_alert, last_attempt = state.get("last_alert"), state.get("last_attempt", 0)
        # 1.º aviso: logo (ou 10 min depois de uma falha certa); depois, de 6 em 6 horas.
        wait = RETRY_S if last_alert is None else REPEAT_S
        since = moment - (last_attempt if last_alert is None else last_alert)
        if since < wait:
            _save_state(state)
            return {"action": "a aguardar", "stale": stale}
        state["last_attempt"] = moment
        detail = ", ".join(f"{r}: há {_fmt(current['ages'][r])}" for r in stale)
        try:
            _send(sender, f"o {_who()} deixou de chamar o motor",
                  [f"Sem chamadas do agendador: {detail}.",
                   "Enquanto isto durar, nada é planeado, entregue nem publicado. " + _hint(),
                   "O aviso repete-se de 6 em 6 horas e quando voltar ao normal."])
            state["last_alert"] = moment
            action = "avisou"
            log.warning("Vigia: aviso enviado (n8n parado: %s)", ", ".join(stale))
        except mail.MailUnknown as exc:        # pode ter saído: não repete antes de 6 horas
            log.warning("Aviso do vigia com resultado incerto: %s", exc)
            state["last_alert"] = moment
            action = "incerto"
        except mail.MailError as exc:
            log.warning("Aviso do vigia falhou (repete em %d min): %s", RETRY_S // 60, exc)
            action = "falhou"
        _save_state(state)
        return {"action": action, "stale": stale}
    # tudo em dia
    if state:
        if state.get("last_alert") is not None:    # só avisa a recuperação de um problema avisado
            try:
                _send(sender, f"o {_who()} voltou a chamar o motor",
                      [f"O agendador esteve parado cerca de {_fmt(moment - state['down_since'])} "
                       "e está outra vez em dia."])
                log.warning("Vigia: recuperação avisada")
            except mail.MailError as exc:
                log.warning("Aviso de recuperação falhou: %s", exc)
        redis_client.delete(_STATE)
        return {"action": "recuperou"}
    return {"action": "ok"}
