"""Fuso da instalação (E19): o «dia local» e os horários que o motor mostra às pessoas.

Os horários de operação (lotes de aprovação, resumo diário, newsletter, plan-day) disparam no
fuso do motor (`TZ`, `settings.tz`; o pacote usa o SB_TZ). Tudo o que conta «o dia de hoje»
ou escreve uma hora para alguém ler segue esse mesmo fuso — nunca um fixo. A nossa produção
usa `TZ=America/Sao_Paulo`, então nada muda para ela.

Os slots de publicação NÃO passam por aqui: eles seguem o fuso de cada blog (D-066).
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_UTC = ZoneInfo("UTC")


def _zone(name: str) -> ZoneInfo | None:
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _city(name: str) -> str:
    """Rótulo padrão: a cidade do identificador IANA (`America/Sao_Paulo` → `Sao Paulo`)."""
    return name.strip().rsplit("/", 1)[-1].replace("_", " ")


def local_zone() -> ZoneInfo:
    """O fuso da instalação (`TZ`); inválido ou vazio = UTC, com aviso (nada quebra por isso)."""
    zone = _zone(settings.tz or "UTC")
    if zone is None:
        log.warning("TZ desconhecido (%s): usando UTC", settings.tz)
        return _UTC
    return zone


def panel_zones(spec: str | None = None) -> list[tuple[ZoneInfo, str]]:
    """Fusos do portal (`settings.panel_timezones`, ou o `TZ` da instalação), com rótulo.

    Formato `fuso[=rótulo]` separados por vírgula. Fuso desconhecido é ignorado com aviso; sem
    nenhum válido, UTC (a tela nunca quebra por isso)."""
    raw = settings.panel_timezones if spec is None else spec
    zones: list[tuple[ZoneInfo, str]] = []
    for item in (raw or settings.tz or "UTC").split(","):
        name, _, label = item.strip().partition("=")
        if not name.strip():
            continue
        zone = _zone(name)
        if zone is None:
            log.warning("PANEL_TIMEZONES: fuso desconhecido ignorado: %s", name)
            continue
        zones.append((zone, label.strip() or _city(name)))
    return zones or [(_UTC, "UTC")]


def local_label() -> str:
    """Rótulo do fuso da instalação: o do PANEL_TIMEZONES, se ele estiver lá (a nossa produção:
    «SP»), senão a cidade do identificador."""
    zone = local_zone()
    for candidate, label in panel_zones():
        if candidate.key == zone.key:
            return label
    return _city(zone.key)


def clock(moment: datetime) -> str:
    """`HH:MM <rótulo>` no fuso da instalação (ex.: «14:30 SP»)."""
    return f"{moment.astimezone(local_zone()):%H:%M} {local_label()}"
