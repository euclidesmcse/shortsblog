"""Post de reserva por blog (D-115): o blog não fica sem post quando o do dia não sai.

O post do dia é produzido 14 h antes do slot (D-066). Quando nada dá certo — fontes que não
chegam, revisão ou tradução que esgotam, redator que recusa, teto de chamadas do job —, o slot
ficava vazio (4 de 88 em 7–21/11; o job 372 da Saúde perdeu o horário no teto `llm_job_cap`).

A reserva é um artigo INTEMPORAL (guia, explicador, contexto) produzido com folga, fora das
horas de produção dos slots, pelo mesmo caminho de qualquer job (pesquisa, gates, revisão de
outra família, tradução) e aprovado pelo Euclides no lote normal. Aprovada, fica guardada
(`reserve = true`, `status = 'APPROVED'`). A reconciliação (`cover`) entrega-a ao slot que
não vai sair: ela passa a ter a hora do slot, o `publish-due` publica-a como qualquer
aprovado, e o slot original expira «coberto pela reserva». Na volta seguinte, `ensure`
encomenda outra reserva para o blog.

Regras:
- uma reserva viva por blog (a produzir, à espera de aprovação ou aprovada);
- a reserva usa pautas próprias (`origin = 'reserve'`): os slots nunca as pegam;
- o slot só é coberto quando já não vai sair: BLOCKED, FAILED ou REJECTED; em RETRY_WAIT
  cuja próxima tentativa já cai depois do slot; ou com a hora do slot passada e o job ainda
  por produzir. À espera de aprovação NUNCA é coberto (a decisão é do Euclides);
- vale `reserve_valid_days`; vencida, expira e é trocada;
- a cota mundial (D-110) é conferida de novo na hora de cobrir: a reserva escrita há dias
  não entra se o país dela já estiver com a cota cheia na janela.

Post ATRASADO (22/11, job 372): aprovado depois do dia dele, não sai junto com o post do dia
(eram dois posts do blog em poucas horas). Fica guardado e cobre o próximo slot que falhar,
ANTES da reserva (a notícia envelhece; a reserva aguenta 30 dias). Passadas
`late_hold_hours` do slot original sem cobrir nada, expira. Atrasado = APPROVED, não reserva,
ainda sem slot coberto e com `local_date` anterior a hoje no fuso do blog (`LATE_SQL`).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.localtime import local_zone
from app.core.logging import get_logger
from app.services import discovery, jobs, planner, sites

log = get_logger(__name__)

ACTOR = "reserve"
SLOT = 0
# Reserva viva: ainda pode chegar a cobrir um slot.
_ALIVE = ("PLANNED", "QUEUED", "CLAIMED", "RESEARCHING", "WRITING", "VALIDATING",
          "TRANSLATING", "AWAITING_APPROVAL", "APPROVED", "RETRY_WAIT")
_DEAD = ("BLOCKED", "FAILED", "REJECTED", "EXPIRED")
# Estados do slot coberto que ainda aceitam a transição para EXPIRED.
_EXPIRABLE = ("PLANNED", "QUEUED", "RETRY_WAIT", "BLOCKED")
COVERED_PREFIX = "coberto pela reserva"
LATE_PREFIX = "coberto pelo post atrasado"
# Job atrasado (ver o cabeçalho). Usa `j` (publication_jobs), `s` (sites) e o parâmetro `:now`.
LATE_SQL = ("(j.status = 'APPROVED' AND NOT j.reserve AND j.covers_job_id IS NULL "
            "AND j.local_date < (CAST(:now AS timestamptz) AT TIME ZONE s.timezone)::date)")
# Datas tentadas para a identidade `(site, data, 0)` de uma reserva nova.
_DATE_TRIES = 3


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(UTC)


def start_at(now: datetime, *, wait: bool = False) -> datetime:
    """Quando a reserva começa a ser produzida: já, se estamos na janela da manhã
    (`reserve_hour` a `reserve_hour` + 6 h, hora local da instalação); senão, na próxima
    abertura dela. A produção dos slots corre da tarde à madrugada (D-066): de manhã a
    reserva não disputa a vez nem a cota com eles. Com `wait`, nunca já: a próxima abertura
    (a reserva anterior morreu há pouco; outra tentativa no mesmo dia queimaria a cota)."""
    local = now.astimezone(local_zone())
    opening = local.replace(hour=settings.reserve_hour % 24, minute=0, second=0, microsecond=0)
    if not wait and opening <= local < opening + timedelta(hours=6):
        return now
    if local >= opening:
        opening += timedelta(days=1)
    return opening.astimezone(UTC)


def ensure(db: Session, now: datetime | None = None) -> list[int]:
    """Encomenda uma reserva a cada blog ligado que não tem nenhuma viva. Idempotente.

    A substituta da que bloqueou só começa na próxima janela da manhã (`start_at`): um blog
    difícil gasta no máximo uma tentativa de reserva por dia."""
    if not settings.reserve_posts:
        return []
    moment = _now(now)
    created: list[int] = []
    for site in sites.enabled(db):
        alive = db.execute(
            text("""SELECT 1 FROM publication_jobs WHERE site_key = :s AND reserve
                     AND status = ANY(:alive) LIMIT 1"""),
            {"s": site["site_key"], "alive": list(_ALIVE)}).first()
        if alive:
            continue
        # A última reserva do blog morreu (bloqueio, rejeição, validade) há menos de um dia:
        # a próxima espera a abertura seguinte da janela.
        recent_death = db.execute(
            text("""SELECT 1 FROM publication_jobs
                     WHERE site_key = :s AND slot_number = 0 AND covers_job_id IS NULL
                       AND status = ANY(:dead) AND updated_at > :since LIMIT 1"""),
            {"s": site["site_key"], "dead": list(_DEAD),
             "since": moment - timedelta(hours=20)}).first()
        due = moment + timedelta(days=settings.reserve_valid_days)
        today = planner.local_today(str(site["timezone"]), moment)
        # A data é só a identidade da reserva: se hoje já tem uma (a que acabou de cobrir o
        # slot, ou uma que bloqueou), usa-se o dia seguinte — sem esperar o calendário.
        job_id = None
        for offset in range(_DATE_TRIES):
            job_id = db.execute(
                text("""INSERT INTO publication_jobs
                          (site_key, local_date, slot_number, due_at_utc, produce_after_utc,
                           reserve)
                        VALUES (:s, :d, :n, :due, :after, true)
                        ON CONFLICT (site_key, local_date, slot_number) DO NOTHING
                        RETURNING id"""),
                {"s": site["site_key"], "d": today + timedelta(days=offset), "n": SLOT,
                 "due": due, "after": start_at(moment, wait=recent_death is not None)},
            ).scalar()
            if job_id is not None:
                break
        if job_id is None:
            continue
        jobs.record_event(db, int(job_id), "planned", to_status="PLANNED", actor=ACTOR,
                          details={"reserve": True, "valid_until": due.isoformat()})
        created.append(int(job_id))
    if created:
        log.info("Reserva (D-115): %d encomendada(s): %s", len(created), created)
    return created


def _candidates(db: Session, moment: datetime) -> list[Any]:
    """Slots que já não vão sair a tempo e ainda não foram cobertos."""
    return list(db.execute(
        text("""SELECT j.id, j.site_key, j.status, j.due_at_utc, j.local_date
                  FROM publication_jobs j JOIN sites s ON s.site_key = j.site_key
                 WHERE s.enabled AND NOT j.reserve AND j.due_at_utc >= :oldest
                   AND NOT EXISTS (SELECT 1 FROM publication_jobs r WHERE r.covers_job_id = j.id)
                   AND (j.status IN ('BLOCKED', 'FAILED', 'REJECTED')
                     OR (j.status IN ('PLANNED', 'QUEUED') AND j.due_at_utc <= :now)
                     OR (j.status = 'RETRY_WAIT'
                         AND NOT EXISTS (SELECT 1 FROM approvals a
                                          WHERE a.job_id = j.id AND a.decision = 'approved')
                         AND (j.due_at_utc <= :now OR j.retry_after_utc >= j.due_at_utc)))
                 ORDER BY j.due_at_utc, j.id
                 FOR UPDATE OF j SKIP LOCKED"""),
        {"now": moment, "oldest": moment - timedelta(hours=settings.job_expire_hours)},
    ).all())


def cover(db: Session, now: datetime | None = None) -> list[tuple[int, int]]:
    """Entrega a reserva aprovada de cada blog ao slot que não vai sair. Devolve
    os pares (slot, reserva). Sem reserva pronta, o slot segue o caminho de sempre."""
    if not settings.reserve_posts:
        return []
    moment = _now(now)
    done: list[tuple[int, int]] = []
    for job_id, site_key, status, due, local_date in _candidates(db, moment):
        late = _late_for(db, site_key, local_date, moment)
        if late is not None:
            _assign(db, late, job_id, status, due, site_key, local_date, late=True)
            done.append((int(job_id), int(late)))
            continue
        ready = db.execute(
            text("""SELECT j.id, i.country FROM publication_jobs j
                      LEFT JOIN ideas i ON i.id = j.idea_id
                     WHERE j.site_key = :s AND j.reserve AND j.status = 'APPROVED'
                       AND j.due_at_utc > :now
                     ORDER BY j.id FOR UPDATE OF j SKIP LOCKED"""),
            {"s": site_key, "now": moment}).all()
        counts = discovery.country_counts(db, site_key, moment)
        reserve_id = next(
            (rid for rid, country in ready
             if not country
             or not discovery.over_quota(discovery.country_code(country), counts)),
            None)
        if reserve_id is None:
            if ready:
                log.warning("Reserva (D-115): a de %s não cobre o job %d — cota por país "
                            "cheia (D-110)", site_key, job_id)
            continue
        _assign(db, reserve_id, job_id, status, due, site_key, local_date, late=False)
        done.append((int(job_id), int(reserve_id)))
    return done


def cover_deadline(status: str, due: datetime) -> datetime:
    """Até quando um slot que não vai sair ainda pode ser coberto: até a expiração dele na
    reconciliação (`dispatcher.reconcile`) — `late_production_hours` depois do horário se nem foi
    produzido, `job_expire_hours` nos outros casos (bloqueado, falhado, rejeitado)."""
    hours = (settings.late_production_hours if status in ("PLANNED", "QUEUED", "RETRY_WAIT")
             else settings.job_expire_hours)
    return due + timedelta(hours=hours)


def _late_for(db: Session, site_key: str, slot_date: Any, moment: datetime) -> int | None:
    """O post atrasado mais novo do blog que ainda pode cobrir este slot (dia posterior ao dele,
    dentro de `late_hold_hours`). A cota D-110 já o conta (foi escolhido e entrou na janela)."""
    found = db.execute(
        text(f"""SELECT j.id FROM publication_jobs j JOIN sites s ON s.site_key = j.site_key
                  WHERE j.site_key = :s AND {LATE_SQL} AND j.local_date < :d
                    AND j.due_at_utc >= :oldest
                  ORDER BY j.due_at_utc DESC, j.id DESC
                  LIMIT 1 FOR UPDATE OF j SKIP LOCKED"""),
        {"s": site_key, "now": moment, "d": slot_date,
         "oldest": moment - timedelta(hours=settings.late_hold_hours)}).scalar()
    return int(found) if found is not None else None


def _assign(db: Session, cover_id: int, job_id: int, status: str, due: datetime,
            site_key: str, local_date: Any, *, late: bool) -> None:
    """O job `cover_id` (reserva ou atrasado) assume o horário do slot `job_id`."""
    if late:
        # O atrasado guarda a data dele (`(site, data, slot)` do slot coberto já existe).
        db.execute(
            text("""UPDATE publication_jobs SET due_at_utc = :due, covers_job_id = :j,
                           updated_at = now() WHERE id = :r"""),
            {"due": due, "j": job_id, "r": cover_id})
    else:
        # A reserva assume a data do slot (resumo diário, telas e contagens pelo dia certo),
        # a não ser que outra reserva já ocupe `(site, data, 0)` — aí fica a data dela.
        db.execute(
            text("""UPDATE publication_jobs
                       SET reserve = false, due_at_utc = :due, covers_job_id = :j,
                           local_date = CASE WHEN EXISTS (
                               SELECT 1 FROM publication_jobs o
                                WHERE o.site_key = :s AND o.local_date = :d
                                  AND o.slot_number = 0 AND o.id <> :r)
                             THEN local_date ELSE :d END,
                           updated_at = now()
                     WHERE id = :r"""),
            {"due": due, "j": job_id, "r": cover_id, "s": site_key, "d": local_date})
    kind = "late" if late else "reserve"
    jobs.record_event(db, int(cover_id), f"{kind}_assigned", actor=ACTOR,
                      details={"covers": int(job_id), "slot_status": status,
                               "due_at_utc": due.isoformat()})
    reason = f"{LATE_PREFIX if late else COVERED_PREFIX} (job {cover_id})"
    if status in _EXPIRABLE:
        jobs.transition(db, int(job_id), "EXPIRED", actor=ACTOR, blocked_reason=reason,
                        details={"reason": f"covered_by_{kind}", kind: int(cover_id),
                                 "was": status})
    else:
        jobs.record_event(db, int(job_id), f"covered_by_{kind}", actor=ACTOR,
                          details={kind: int(cover_id), "was": status})
    log.warning("D-115: job %d (%s) coberto pelo %s %d", job_id, status,
                "post atrasado" if late else "reserva", cover_id)


def is_late(db: Session, job_id: int, now: datetime | None = None) -> bool:
    """O job é um post atrasado à espera de um slot para cobrir (ver o cabeçalho)."""
    return db.execute(
        text(f"""SELECT 1 FROM publication_jobs j JOIN sites s ON s.site_key = j.site_key
                  WHERE j.id = :i AND {LATE_SQL}"""),
        {"i": job_id, "now": _now(now)}).first() is not None


def is_covered(reason: str | None) -> bool:
    """O motivo registado no slot diz que outro job o cobriu (reserva ou atrasado)."""
    return str(reason or "").startswith((COVERED_PREFIX, LATE_PREFIX))


def overview(db: Session) -> dict[str, dict[str, Any]]:
    """Reserva viva de cada blog ligado (a mais adiantada), para e-mails e telas."""
    rows = db.execute(
        text("""SELECT DISTINCT ON (j.site_key) j.site_key, j.id, j.status, j.due_at_utc,
                       (SELECT title FROM job_translations t
                         WHERE t.job_id = j.id AND t.language = 'pt-BR') AS title
                  FROM publication_jobs j JOIN sites s ON s.site_key = j.site_key
                 WHERE s.enabled AND j.reserve AND j.status = ANY(:alive)
                 ORDER BY j.site_key, (j.status = 'APPROVED') DESC, j.id"""),
        {"alive": list(_ALIVE)}).mappings().all()
    return {str(r["site_key"]): dict(r) for r in rows}
