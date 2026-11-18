"""Planeador diário (E4.3).

Para cada site ligado cria os slots do dia: 07:00, 10:30, 12:30, 15:30 e 18:30
(de fábrica; editáveis na tela Sistema, E18.3b)
na hora local do site (`Europe/Lisbon` desde a D-066; antes America/Sao_Paulo,
D-004), cada um desfasado dos minutos do site (0, 7, 14, 21, 28, 35) para os seis
blogs não arrancarem ao mesmo tempo. Os instantes ficam em UTC; `local_date` é a
data editorial local.

A produção pode começar `PRODUCTION_LEAD_HOURS` antes do slot (D-066: 14 h). Sem data,
o planeador cria hoje e amanhã: o post das 07:00 é produzido à tarde da véspera e
aprovado à noite, por isso o dia seguinte tem de existir antes da meia-noite.

Correr duas vezes no mesmo dia não duplica nada: a chave única
`(site_key, local_date, slot_number)` faz da segunda corrida um não-evento.

Slots cujo horário já passou não são criados (D-034): se o planeador correr
tarde, ou um site for ligado a meio do dia, o dia fica com menos posts em vez
de despejar de uma vez os que ficaram para trás.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.services import jobs, sites, vault

log = get_logger(__name__)

# Hora-base de cada slot, na hora local do site (spec §5.2; o 1.º às 07:00, D-066). Valor de
# fábrica: desde a E18.3b vale `vault.slot_times()` (tela Sistema > ambiente `SLOT_TIMES`).
SLOT_TIMES = vault.DEFAULT_SLOTS


@dataclass(frozen=True)
class Slot:
    site_key: str
    local_date: date
    slot_number: int
    due_at_utc: datetime
    produce_after_utc: datetime


@dataclass(frozen=True)
class PlanResult:
    created: int
    existing: int
    sites: int
    # Slots não criados por o horário já ter passado (D-034).
    skipped: int = 0


def local_today(timezone: str, now: datetime | None = None) -> date:
    """A data editorial de hoje no fuso do site."""
    return (now or datetime.now(UTC)).astimezone(ZoneInfo(timezone)).date()


def slots_for(site: dict[str, object], local_date: date) -> list[Slot]:
    zone = ZoneInfo(str(site["timezone"]))
    offset = timedelta(minutes=int(site["slot_offset_minutes"]))  # type: ignore[call-overload]
    lead = timedelta(hours=settings.production_lead_hours)
    result = []
    bases = vault.slot_times()
    for number, base in enumerate(bases[: int(site["daily_target"])], start=1):  # type: ignore[call-overload]
        local = datetime.combine(local_date, base, tzinfo=zone) + offset
        due = local.astimezone(UTC)
        result.append(Slot(str(site["site_key"]), local_date, number, due, due - lead))
    return result


def plan_day(db: Session, local_date: date | None = None, *,
             now: datetime | None = None, actor: str = "planner") -> PlanResult:
    """Cria os slots em falta do dia para todos os sites ligados. Idempotente.

    Sem `local_date`, planeia hoje e amanhã no fuso de cada site (D-066).
    """
    created = existing = skipped = 0
    moment = now or datetime.now(UTC)
    enabled = sites.enabled(db)
    for site in enabled:
        today = local_today(str(site["timezone"]), moment)
        days = [local_date] if local_date else [today, today + timedelta(days=1)]
        for slot in (s for day in days for s in slots_for(site, day)):
            if slot.due_at_utc <= moment:
                # Já existe (foi planeado a horas) ou já passou: em nenhum caso se cria.
                skipped += 1
                continue
            job_id = db.execute(
                text("""INSERT INTO publication_jobs
                          (site_key, local_date, slot_number, due_at_utc, produce_after_utc)
                        VALUES (:s, :d, :n, :due, :after)
                        ON CONFLICT (site_key, local_date, slot_number) DO NOTHING
                        RETURNING id"""),
                {"s": slot.site_key, "d": slot.local_date, "n": slot.slot_number,
                 "due": slot.due_at_utc, "after": slot.produce_after_utc},
            ).scalar()
            if job_id is None:
                existing += 1
                continue
            created += 1
            jobs.record_event(db, job_id, "planned", to_status="PLANNED", actor=actor,
                              details={"due_at_utc": slot.due_at_utc.isoformat()})
    if created or skipped:
        log.info("Planeador: %d slots criados, %d já existiam, %d saltados por já terem "
                 "passado, em %d sites", created, existing, skipped, len(enabled))
    return PlanResult(created=created, existing=existing, sites=len(enabled), skipped=skipped)
