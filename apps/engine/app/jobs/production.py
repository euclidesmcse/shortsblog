"""Produção de um job (fila `content`): da ideia ao artigo à espera de aprovação (E6).

    CLAIMED → RESEARCHING → WRITING → VALIDATING → TRANSLATING → AWAITING_APPROVAL

Nunca publica: o último estado a que este código leva um job é
`AWAITING_APPROVAL` (E6.9, D-006). O que não passa nos gates fica `BLOCKED` com
o motivo; se os provedores de IA estiverem indisponíveis, o job vai para
`RETRY_WAIT` e tenta mais tarde.

Cada entrega tem um dono (`owner`). O worker renova a lease antes de cada passo
e passa o dono em cada transição: se o job já foi reentregue a outro, larga-o.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text

from app.core import cache, localtime
from app.core.config import settings
from app.core.db import session_scope
from app.core.logging import get_logger
from app.services import (
    article,
    discovery,
    dispatcher,
    gates,
    illustration,
    jobs,
    recipe,
    sites,
    vault,
    websearch,
)
from app.services.ai import chain, quota
from app.services.ai.prompting import External
from app.utils.text import slugify

log = get_logger(__name__)

ACTOR = "worker:content"
# Pautas sem notícia por trás: resgate do slot (D-101) e reserva (D-115). Sem filtro de idade
# nas fontes e com a busca no bolso próprio do orçamento do job.
_TIMELESS = ("rescue", "reserve")
TARGET_LANGUAGES = ("en", "es", "zh-CN")


class _Stale(Exception):
    """A lease já não é desta entrega."""


class _Blocked(Exception):
    """Um gate ou um passo recusou o artigo."""

    def __init__(self, reason: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.details = details or {}


class _NoProviders(Exception):
    """A pré-checagem não encontrou provedores para produzir (R27): esperar sem gastar nada."""


class _NoIdea(_Blocked):
    """Nenhuma ideia da fila passou na pesquisa: pode dar certo mais tarde, com ideias novas."""


class _TranslationExhausted(_Blocked):
    """Um gate de tradução esgotou as tentativas de um rascunho aprovado (D-100)."""


class _ReviewExhausted(_Blocked):
    """A revisão (ou um gate) esgotou as reescritas desta pauta: tenta-se outra (D-093)."""


def _step(job_id: int, owner: str, target: str, **fields: Any) -> None:
    """Renova a lease e muda de estado; se a lease se perdeu, para tudo."""
    with session_scope() as db:
        if not dispatcher.renew_lease(db, job_id, owner):
            raise _Stale
        try:
            jobs.transition(db, job_id, target, actor=ACTOR, owner=owner, **fields)
        except jobs.LeaseLost:
            raise _Stale from None


def _alive(job_id: int, owner: str) -> None:
    with session_scope() as db:
        if not dispatcher.renew_lease(db, job_id, owner):
            raise _Stale


def _owned(db: Any, job_id: int, owner: str) -> None:
    """Dentro da transação de uma escrita: se a lease já não é desta entrega, nada se grava."""
    if not dispatcher.renew_lease(db, job_id, owner):
        raise _Stale


def _corroboration_window(db: Any, now: datetime) -> list[tuple[str, str, str, Any]]:
    """Ideias recentes de qualquer blog: (ligação, chave do título, língua, entidades).

    Só depende do momento, não da ideia: calcula-se uma vez por `_research` (a consulta e a
    extração de entidades dos 500 títulos são o que custa) e cada ideia tentada apenas filtra.
    """
    rows = db.execute(
        text("""SELECT COALESCE(i.source_urls[1], i.canonical_url), i.title_key, i.topic,
                       COALESCE(s.lang, '')
                  FROM ideas i LEFT JOIN sources s ON s.id = i.source_id
                 WHERE i.canonical_url IS NOT NULL AND i.created_at >= :since
                 ORDER BY i.published_at DESC NULLS LAST LIMIT 500"""),
        {"since": now - timedelta(days=settings.idea_max_age_days + 1)},
    ).all()
    return [(url, key, lang, discovery.title_entities(topic)) for url, key, topic, lang in rows]


def _corroboration(window: list[tuple[str, str, str, Any]],
                   picked: discovery.Picked) -> list[str]:
    """Outras fontes da mesma história, de qualquer blog: ideias recentes de título parecido.

    Na mesma língua compara-se o título palavra a palavra; entre línguas, as entidades do
    título (E14.5g, D-072), com no máximo `corroboration_cross_max` ligações. A pesquisa
    confere depois se cada página trata mesmo do tema (D-059).
    """
    key = discovery.title_key(picked.topic)
    rows = [r for r in window if r[0] not in picked.source_urls]
    same = [url for url, other, _, _ in rows if discovery.similar(key, other)]
    if not picked.lang:
        return same
    # Entidades que aparecem em muitos títulos da janela não distinguem uma notícia.
    counts = Counter(e for _, _, _, found in rows for e in found)
    common = frozenset(e for e, n in counts.items() if n >= settings.corroboration_common)
    mine = discovery.title_entities(picked.topic)
    cross = list(dict.fromkeys(
        url for url, _, lang, found in rows
        if lang and lang != picked.lang and url not in same
        and discovery.same_story(mine, found, common)))
    return same + cross[:settings.corroboration_cross_max]


def _discard_idea(job_id: int, owner: str, idea_id: int,
                  event: str | None = None, **details: Any) -> None:
    """Descarta a ideia (e as suas repetições noutros feeds); com `event`, regista o porquê."""
    with session_scope() as db:
        _owned(db, job_id, owner)
        db.execute(text("UPDATE ideas SET status = 'discarded' "
                        "WHERE id = :i OR duplicate_of = :i"), {"i": idea_id})
        if event:
            jobs.record_event(db, job_id, event, actor=ACTOR,
                              details={"idea_id": idea_id, **details})


def _release_idea(job_id: int, owner: str, idea_id: int) -> None:
    """Devolve a pauta à fila: ficar sem provedor de IA não é culpa dela (R27).

    Sem isto, cada volta em RETRY_WAIT pegava uma pauta nova, repetia pesquisa e
    buscas e esvaziava a fila; a próxima tentativa retoma a mesma história.
    """
    with session_scope() as db:
        _owned(db, job_id, owner)
        db.execute(text("UPDATE ideas SET status = 'new' "
                        "WHERE (id = :i OR duplicate_of = :i) AND status = 'taken'"),
                   {"i": idea_id})


def _release_taken_idea(job_id: int, owner: str) -> None:
    """Como `_release_idea`, pela pauta registada no job (pós-redação); nunca levanta."""
    with session_scope() as db:
        db.execute(text("""UPDATE ideas i SET status = 'new'
                             FROM publication_jobs j
                            WHERE j.id = :j AND j.lease_owner = :o AND j.idea_id IS NOT NULL
                              AND (i.id = j.idea_id OR i.duplicate_of = j.idea_id)
                              AND i.status = 'taken'"""), {"j": job_id, "o": owner})


def _research(job_id: int, owner: str, site: dict[str, Any], now: datetime, *,
              reserve: bool = False) -> tuple[discovery.Picked, article.Research]:
    """Escolhe uma ideia com fontes suficientes e levanta os factos. Tenta várias ideias.

    A reserva (D-115) só usa as pautas dela, intemporais: não tria notícias."""
    minimum = int((site.get("config") or {}).get("min_sources", 2))
    recipe_mode = bool((site.get("config") or {}).get("recipe_mode"))   # D-080
    last = "sem ideias na fila"
    attempts: list[dict[str, Any]] = []   # diagnóstico do bloqueio: que ideias se tentaram
    # Triagem (D-040): uma chamada barata ordena as ideias novas e afasta as de outro assunto,
    # antes de se gastar uma pesquisa em cada uma.
    if not reserve:
        with session_scope() as db:
            triaged = discovery.triage(db, site, now)
        if triaged["scored"]:
            log.info("Triagem de %s: %s", site["site_key"], triaged)
    window: list[tuple[str, str, str, Any]] | None = None
    # R42: pautas cujo artigo-fonte falhou por motivo passageiro (rede, tempo, 5xx). Não são
    # descartadas: ficam presas durante este job (para o laço não as pegar de novo) e voltam
    # à fila no fim, para outro job tentar noutra hora.
    deferred: list[int] = []
    try:
        return _research_loop(job_id, owner, site, now, reserve=reserve, minimum=minimum,
                              recipe_mode=recipe_mode, attempts=attempts, deferred=deferred,
                              window=window, last=last)
    finally:
        for idea_id in deferred:
            try:
                _release_idea(job_id, owner, idea_id)
            except _Stale:
                break        # o job já não é nosso: a reconciliação devolve as pautas órfãs


def _research_loop(job_id: int, owner: str, site: dict[str, Any], now: datetime, *,
                   reserve: bool, minimum: int, recipe_mode: bool,
                   attempts: list[dict[str, Any]], deferred: list[int],
                   window: list[tuple[str, str, str, Any]] | None,
                   last: str) -> tuple[discovery.Picked, article.Research]:
    for _ in range(settings.idea_attempts):
        _alive(job_id, owner)
        with session_scope() as db:
            picked = discovery.pick(db, site["site_key"], now, reserve=reserve)
            if picked is not None and window is None:
                window = _corroboration_window(db, now)
        if picked is None:
            break
        extra = _corroboration(window or [], picked)
        # Fontes só de pauta (D-070): o site não deixa a página ir para a IA.
        skipped: list[str] = []
        transient: list[str] = []
        sources = article.read_sources([*picked.source_urls, *extra], now, skipped=skipped,
                                       with_recipe=recipe_mode, transient=transient)
        if (len(sources) < minimum and picked.source_urls
                and picked.source_urls[0] in transient):
            # O artigo-fonte não respondeu agora: sem gastar busca, a pauta fica para outra
            # hora (R42) em vez de morrer «sem fontes».
            last = f"artigo-fonte inalcançável por agora ({picked.source_urls[0][:120]})"
            attempts.append({"idea_id": picked.id, "topic": picked.topic[:160],
                             "reason": last, "pauta": 0, "transient": True})
            deferred.append(picked.id)
            continue
        via_search: set[str] = set()
        searched: dict[str, Any] | None = None
        if len(sources) < minimum and websearch.enabled():
            # Reforço (D-063): só quando faltam fontes. Falha da busca = seguir sem ela.
            tried = [*picked.source_urls, *extra]
            searched = {"idea_id": picked.id, "sources_before": len(sources)}
            try:
                # Pauta de resgate nasce sem fontes e só existe porque o resto da fila
                # falhou: busca no bolso próprio do orçamento do job (D-101).
                more = websearch.search(picked.topic, site=site, exclude=tried, need=minimum,
                                        stats=searched, lang=picked.lang, recipe=recipe_mode,
                                        job_id=job_id,
                                        pocket="rescue" if picked.origin in _TIMELESS else "")
            except chain.ChainExhausted:
                # Sem IA nenhuma a produção espera (R27); a pauta volta à fila.
                _release_idea(job_id, owner, picked.id)
                raise
            searched["links"] = len(more)
            if more:
                before = len(sources)
                # O filtro de frescor existe para que uma página velha não corrobore
                # NOTÍCIA de hoje. A pauta de resgate é intemporal por definição: as
                # suas melhores fontes são páginas oficiais de referência (diretrizes,
                # calendários, guias) com meses ou anos — sem filtro de idade para ela.
                timeless = recipe_mode or picked.origin in _TIMELESS or picked.timeless
                sources = article.read_sources(
                    more, now, fresh_urls=frozenset(more),
                    max_age_days=0 if timeless else settings.searxng_max_age_days,
                    skipped=skipped, with_recipe=recipe_mode, base=sources)
                via_search = {websearch.domain_of(u) for u in more}
                cache.incr_daily("searxng_reinforced")
                log.info("Reforço por busca: %d → %d fontes (ideia %s)", before, len(sources),
                         picked.id)
        found = None
        pauta = len({websearch.domain_of(u) for u in skipped})
        if len(sources) < minimum:
            last = f"fontes insuficientes: {len(sources)} de {minimum}" + (
                f" ({pauta} só de pauta pelos termos de uso)" if pauta else "")
        else:
            try:
                # Modo receita: as fontes só conferem a receita; não há fatos a levantar.
                found = (article.Research(sources=sources) if recipe_mode else
                         article.research(picked.topic, sources, site=site, job_id=job_id))
            except chain.ChainExhausted:
                # Sem provedor não é culpa da pauta: devolve-a e espera (R27).
                _release_idea(job_id, owner, picked.id)
                raise
            except article.StepFailed as exc:
                last = str(exc)
            else:
                # Só contam as fontes que sustentam factos (as outras já saíram da lista).
                if len(found.sources) < minimum:
                    last = (f"fontes insuficientes: {len(found.sources)} de {minimum} "
                            f"tratam do tema e sustentam factos ({len(sources)} páginas lidas)")
                    found = None
                else:
                    found.via_search = sorted(
                        {websearch.domain_of(s.url) for s in found.sources} & via_search)
        if searched is not None:
            _record_search(job_id, owner, searched, sources=len(sources), found=found)
        if found is None:
            attempts.append({"idea_id": picked.id, "topic": picked.topic[:160],
                             "reason": last[:200], "pauta": pauta})
            _discard_idea(job_id, owner, picked.id)
            continue
        return picked, found
    raise _NoIdea(last, {"ideas": attempts})


def _record_search(job_id: int, owner: str, searched: dict[str, Any], *, sources: int,
                   found: Any) -> None:
    """Grava a tentativa de reforço como evento do job (E14.5e).

    Os contadores do Redis somem em 48 h; o evento fica no banco para medir por mais tempo.
    `saved`: a ideia passou com pelo menos uma fonte vinda da busca.
    """
    used = list(found.via_search) if found is not None else []
    details = {**searched, "sources_after": sources, "used_domains": used,
               "saved": bool(used)}
    with session_scope() as db:
        _owned(db, job_id, owner)
        jobs.record_event(db, job_id, "search_reinforcement", actor=ACTOR, details=details)


def _step_retry(job_id: int, owner: str, *, step: str, reason: str,
                family: str | None) -> None:
    """Regista o tropeço técnico que ganhou repetição (B da D-101), para diagnóstico."""
    with session_scope() as db:
        _owned(db, job_id, owner)
        jobs.record_event(db, job_id, "step_retry", actor=ACTOR,
                          details={"step": step, "reason": reason[:300],
                                   "avoid_family": family})


def _recent_titles(site_key: str, job_id: int, now: datetime) -> list[str]:
    with session_scope() as db:
        return list(db.execute(
            text("""SELECT t.title FROM job_translations t
                      JOIN publication_jobs j ON j.id = t.job_id
                     WHERE j.site_key = :s AND t.is_original AND j.id <> :j
                       AND j.status NOT IN ('FAILED', 'REJECTED', 'BLOCKED')
                       AND t.created_at >= :since"""),
            {"s": site_key, "j": job_id,
             "since": now - timedelta(days=settings.duplicate_window_days)},
        ).scalars())


def _save(job_id: int, owner: str, language: str, *, title: str, slug: str, excerpt: str,
          html: str, original: bool, status: str = "ready") -> None:
    with session_scope() as db:
        _owned(db, job_id, owner)
        db.execute(
            text("""INSERT INTO job_translations
                      (job_id, language, is_original, title, slug, excerpt, html, status)
                    VALUES (:j, :l, :o, :t, :s, :e, :h, :st)
                    ON CONFLICT (job_id, language) DO UPDATE
                       SET title = EXCLUDED.title, slug = EXCLUDED.slug,
                           excerpt = EXCLUDED.excerpt, html = EXCLUDED.html,
                           status = EXCLUDED.status, updated_at = now()"""),
            {"j": job_id, "l": language, "o": original, "t": title, "s": slug, "e": excerpt,
             "h": html, "st": status},
        )


def _is_reserve(job_id: int) -> bool:
    """O job nasceu reserva (D-115) e ainda espera um slot."""
    with session_scope() as db:
        return bool(db.execute(text("SELECT reserve FROM publication_jobs WHERE id = :i"),
                               {"i": job_id}).scalar())


def _produce(job_id: int, owner: str, now: datetime) -> str:
    with session_scope() as db:
        row = db.execute(text("SELECT site_key, reserve FROM publication_jobs WHERE id = :i"),
                         {"i": job_id}).first()
        site = sites.get(db, row[0]) if row else None
    if site is None or row is None:
        raise _Blocked("site desconhecido")
    reserve = bool(row[1])

    recipe_mode = bool((site.get("config") or {}).get("recipe_mode"))   # D-080, ADR 0004
    # --- Pesquisa (E6.3)
    _step(job_id, owner, "RESEARCHING")
    picked, found = _research(job_id, owner, site, now, reserve=reserve)
    refs = [recipe.Reference(s.name, s.recipe, s.text) for s in found.sources
            if s.recipe is not None] if recipe_mode else []
    with session_scope() as db:
        _owned(db, job_id, owner)
        db.execute(text("DELETE FROM article_evidence WHERE job_id = :j"), {"j": job_id})
        for index, source in enumerate(found.sources, start=1):
            claims = (recipe.summary(source.recipe) if recipe_mode and source.recipe else
                      " | ".join(f["claim"] for f in found.facts
                                 if index in (f["source"], *f.get("also", []))))
            db.execute(
                text("""INSERT INTO article_evidence
                          (job_id, source_url, source_name, source_published_at, accessed_at,
                           claims_supported, usage_rights_note)
                        VALUES (:j, :u, :n, :p, :a, :c, :r)"""),
                {"j": job_id, "u": source.url, "n": source.name, "p": source.published,
                 "a": source.accessed_at, "c": claims[:4000],
                 "r": ("conferência de plausibilidade; receita, texto e imagens não reproduzidos"
                       if recipe_mode else "citação com ligação; texto e imagens não reproduzidos")
                      + ("; reforço encontrado por busca"
                         if websearch.domain_of(source.url) in found.via_search else "")})

    # --- Redação (E6.5) e revisão por outra família (E6.6), com uma segunda oportunidade
    _step(job_id, owner, "WRITING", idea_id=picked.id)
    feedback: list[str] | None = None
    draft = None
    verdict = None
    rewrite_reasons: list[dict[str, Any]] = []     # por que cada rascunho voltou ao redator
    attempts = settings.review_max_rewrites + 1
    stumbled = False               # B da D-101: um tropeço técnico dá direito a UMA repetição
    avoid_writer: str | None = None
    for attempt in range(1, attempts + 1):
        while True:
            try:
                if recipe_mode:
                    draft = article.write_recipe(picked.topic, picked.angle, site=site,
                                                 job_id=job_id, now=now, feedback=feedback,
                                                 avoid_family=avoid_writer)
                else:
                    draft = article.write(picked.topic, picked.angle, found, site=site,
                                          job_id=job_id, now=now, feedback=feedback,
                                          avoid_family=avoid_writer)
            except article.Declined as exc:
                # Tema sem como aprovar não trava o job: a pauta sai e procura-se outra (D-085).
                _discard_idea(job_id, owner, picked.id, "theme_declined",
                              topic=picked.topic[:160], reason=str(exc)[:300])
                raise
            except article.StepFailed as exc:
                # Resultado inutilizável (sem título, link estranho): não é veredito editorial.
                # Repete-se UMA vez, de preferência com outra família (B da D-101).
                if stumbled:
                    raise _Blocked(str(exc)) from None
                stumbled, avoid_writer = True, exc.family
                _step_retry(job_id, owner, step="write", reason=str(exc), family=exc.family)
                continue
            break
        _step(job_id, owner, "VALIDATING", writer_family=draft.writer_family)
        # Contam as fontes a que o texto liga, não as que a pesquisa leu: um artigo que só se
        # apoia numa fonte tem uma fonte, por mais que a pesquisa tenha encontrado.
        cited = found.sources if recipe_mode else article.cited_sources(draft.html, found.sources)
        problems = gates.check_article(
            title=draft.title, excerpt=draft.excerpt, html=draft.html, category=draft.category,
            site=site,
            source_count=len(cited),
            min_words=settings.recipe_min_words if recipe_mode else settings.ms_article_min_words,
            recent_titles=_recent_titles(site["site_key"], job_id, now))
        if recipe_mode:
            problems = [*problems, *recipe.check_recipe(draft.html, refs)]
        if problems:
            # Falhou um gate determinístico: nem vale a pena gastar uma revisão.
            kind, feedback, reasons, verdict = "gates", problems, problems, None
        else:
            verdict = (article.review_recipe(draft, refs, site=site, job_id=job_id,
                                             topic=picked.topic) if recipe_mode else
                       article.review(draft, found, site=site, job_id=job_id, now=now,
                                      topic=picked.topic))
            if verdict.approved:
                break
            kind = "revisão"
            reasons = [*verdict.unsupported_claims, *verdict.blocking] or [
                f"nota {verdict.score} abaixo do mínimo {settings.review_min_score}"]
            feedback = verdict.to_fix or reasons
        if attempt == attempts:
            # Fica guardado o rascunho recusado, para quem for ver porque bloqueou.
            _save(job_id, owner, "pt-BR", title=draft.title, slug=slugify(draft.title),
                  excerpt=draft.excerpt, html=draft.html, original=True, status="failed")
            final = {"attempt": attempt, "kind": kind, "reasons": [r[:240] for r in reasons[:6]]}
            trail = [*rewrite_reasons, final]
            # Uma pauta que a revisão esgotou não trava o job: sai e procura-se outra (D-093).
            _discard_idea(job_id, owner, picked.id, "review_exhausted",
                          topic=picked.topic[:160], kind=kind, attempts=attempt,
                          rewrite_reasons=trail)
            raise _ReviewExhausted(f"{kind}: " + "; ".join(reasons)[:900],
                                   {"rewrite_reasons": trail})
        rewrite_reasons.append({"attempt": attempt, "kind": kind,
                                "reasons": [r[:240] for r in reasons[:6]]})
        _step(job_id, owner, "WRITING")           # VALIDATING → WRITING: reescrever com o parecer
    assert draft is not None
    rewrites = attempt - 1

    # --- Ajuste C: receita aprovada com reparos menores ganha uma passada de polimento.
    # A versão aprovada fica de reserva e volta se o polimento tropeçar num gate ou na re-revisão.
    polish_report: dict[str, Any] | None = None
    if recipe_mode and settings.recipe_polish and verdict is not None and verdict.notes:
        _alive(job_id, owner)
        reserve_draft, reserve_verdict = draft, verdict
        try:
            polished = article.polish_recipe(draft, verdict.notes, site=site, job_id=job_id,
                                             topic=picked.topic)
            problems = gates.check_article(
                title=polished.title, excerpt=polished.excerpt, html=polished.html,
                category=polished.category, site=site, source_count=len(found.sources),
                min_words=settings.recipe_min_words,
                recent_titles=_recent_titles(site["site_key"], job_id, now))
            problems = [*problems, *recipe.check_recipe(polished.html, refs)]
            if problems:
                polish_report = {"applied": False,
                                 "reason": "gates: " + "; ".join(problems)[:240]}
            else:
                second = article.review_recipe(polished, refs, site=site, job_id=job_id,
                                               topic=picked.topic)
                if second.approved and second.score >= verdict.score:
                    draft, verdict = polished, second
                    polish_report = {"applied": True,
                                     "notes_before": len(reserve_verdict.notes),
                                     "notes_after": len(second.notes),
                                     "scores": [reserve_verdict.score, second.score]}
                else:
                    polish_report = {"applied": False,
                                     "reason": f"re-revisão: aprovado={second.approved}, "
                                               f"nota {second.score} (antes {verdict.score})"}
        except article.StepFailed as exc:
            polish_report = {"applied": False, "reason": str(exc)[:240]}
        if not polish_report.get("applied"):
            draft, verdict = reserve_draft, reserve_verdict
        with session_scope() as db:
            _owned(db, job_id, owner)
            jobs.record_event(db, job_id, "recipe_polish", actor=ACTOR, details=polish_report)

    base_slug = slugify(draft.title) or f"artigo-{job_id}"
    # "Fontes e créditos" lista as fontes que o texto usa, e só essas.
    listed = found.sources if recipe_mode else article.cited_sources(draft.html, found.sources)

    def closing(language: str) -> str:
        """O que o código acrescenta ao fim do texto: aviso da receita (D-080) e fontes."""
        notice = recipe.notice([s.name for s in listed], language) if recipe_mode else ""
        return notice + article.sources_section(listed, language)

    _save(job_id, owner, "pt-BR", title=draft.title, slug=base_slug, excerpt=draft.excerpt,
          html=draft.html + closing("pt-BR"), original=True)

    # --- Tradução (E6.8): links e números têm de bater com o original
    _step(job_id, owner, "TRANSLATING")
    _alive(job_id, owner)
    # P5 do ensaio E14 (3/11): en e es saem numa só chamada; o zh-CN vai sozinho (gate D-060).
    # Os gates correm por idioma na mesma: o que reprovar volta sozinho, com o parecer.
    pair = article.translate_pair(draft, site=site, job_id=job_id, base_slug=base_slug)
    title_en = ""
    for language in TARGET_LANGUAGES:
        flaws: list[str] | None = None
        avoid_translator: str | None = None
        translation: article.Translation | None = pair.get(language)
        for attempt in (1, 2):
            _alive(job_id, owner)
            try:
                if translation is None:
                    translation = article.translate(draft, language, site=site, job_id=job_id,
                                                    base_slug=base_slug, problems=flaws,
                                                    avoid_family=avoid_translator)
            except article.StepFailed as exc:
                # Resultado inutilizável: a 2.ª tentativa vai com outra família (B da D-101);
                # na 2.ª, bloqueia como antes.
                if attempt == 2:
                    raise _Blocked(str(exc)) from None
                flaws, avoid_translator, translation = [str(exc)[:200]], exc.family, None
                _step_retry(job_id, owner, step=f"translate:{language}", reason=str(exc),
                            family=exc.family)
                continue
            flaws = gates.check_translation(
                draft.html, translation.html, title=translation.title,
                excerpt=translation.excerpt, original_title=draft.title,
                original_excerpt=draft.excerpt, language=language)
            if not flaws and recipe_mode:
                # Títulos canônicos (revisão E14): o sb-recipe.php casa por igualdade exata;
                # os <h2> e os rótulos do cartão são reescritos por posição, não traduzidos.
                localized = recipe.localize_structure(translation.html, language,
                                                      original=draft.html)
                if localized is None:
                    flaws = ["estrutura da receita: a tradução não manteve as "
                             f"{len(recipe.SECTIONS)} seções <h2> do original"]
                else:
                    translation = replace(translation, html=localized)
            if not flaws:
                break
            if attempt == 2:
                # Como no rascunho recusado: fica guardada, marcada como falhada, para diagnóstico.
                _save(job_id, owner, language, title=translation.title, slug=translation.slug,
                      excerpt=translation.excerpt, html=translation.html, original=False,
                      status="failed")
                reason = f"tradução {language}: " + "; ".join(flaws)
                if any(f.startswith("números") for f in flaws):
                    seen = gates.new_number_context(draft.html, translation.html)
                    if seen:
                        reason += " | números a mais em: " + " ".join(seen)
                # A pauta não trava o job (D-100): o artigo aprovado morreu num gate de
                # tradução sem saída; a pauta sai e o job conta outra história.
                _discard_idea(job_id, owner, picked.id, "translation_exhausted",
                              topic=picked.topic[:160], language=language,
                              reason=reason[:300])
                raise _TranslationExhausted(
                    reason[:900],
                    {"rewrite_reasons": rewrite_reasons} if rewrite_reasons else None)
            translation = None          # reprova: a 2.ª tentativa vai sozinha, com o parecer
        assert translation is not None  # só se sai do laço com uma tradução que passou
        _save(job_id, owner, language, title=translation.title, slug=translation.slug,
              excerpt=translation.excerpt,
              html=translation.html + closing(language),
              original=False)
        if language == "en":
            title_en = translation.title

    # --- Imagem (E7): foto pública com licença ou ilustração por IA; opcional, nunca bloqueia
    _alive(job_id, owner)
    illustration.illustrate(job_id, draft.title, draft.excerpt, site=site, title_en=title_en)

    # --- Fim: à espera da aprovação humana (E6.9). Daqui para a frente decide o Euclides.
    with session_scope() as db:
        _owned(db, job_id, owner)
        db.execute(text("UPDATE ideas SET status = 'used' WHERE id = :i OR duplicate_of = :i"),
                   {"i": picked.id})
    # Reparos menores do revisor: não bloqueiam, seguem para quem aprova (D-037).
    notes: list[str] = verdict.notes if verdict else []
    report: dict[str, Any] = {
        "writer": draft.writer, "reviewer": verdict.reviewer if verdict else None,
        "score": verdict.score if verdict else None, "sources": len(listed),
        "sources_read": len(found.sources), "category": draft.category,
        "rewrites": rewrites, "notes": notes}
    if rewrite_reasons:
        report["rewrite_reasons"] = rewrite_reasons
    if polish_report is not None:
        report["polish"] = polish_report
    with session_scope() as db:
        _owned(db, job_id, owner)
        db.execute(text("UPDATE publication_jobs SET review_report = CAST(:r AS JSONB) "
                        "WHERE id = :i"),
                   {"r": json.dumps(report, ensure_ascii=False), "i": job_id})
    _step(job_id, owner, "AWAITING_APPROVAL",
          details={**report, "notes": len(notes)})
    return "AWAITING_APPROVAL"


def _source_retry_at(job_id: int, now: datetime) -> datetime | None:
    """Quando repetir a pesquisa de um job sem ideia viável; None se já repetiu ou não cabe.

    Só repete `source_retries` vezes e só se a nova tentativa começa antes do slot (com a
    D-066 a produção começa 14 h antes, então há folga para produzir e aprovar). A reserva
    (D-115) não espera notícia fresca: as pautas dela são intemporais e vêm do resgate.
    """
    if _is_reserve(job_id):
        return None
    retry_at = now + timedelta(hours=settings.source_retry_hours)
    with session_scope() as db:
        due, done = db.execute(
            text("""SELECT j.due_at_utc,
                           (SELECT count(*) FROM publication_events e
                             WHERE e.job_id = j.id AND e.to_status = 'RETRY_WAIT'
                               AND e.details->>'source_retry' = 'true')
                      FROM publication_jobs j WHERE j.id = :i"""), {"i": job_id}).one()
    if done >= settings.source_retries or retry_at >= due:
        return None
    return retry_at


_RESCUE_SCHEMA = {
    "type": "object",
    "properties": {"topics": {"type": "array", "items": {
        "type": "object",
        "properties": {"topic": {"type": "string"}, "angle": {"type": "string"},
                       "country": {"type": "string"}},
        "required": ["topic", "angle", "country"], "additionalProperties": False}}},
    "required": ["topics"], "additionalProperties": False,
}


def _slot_rescue(job_id: int, owner: str, now: datetime) -> bool:
    """Pauta de resgate (D-101): o slot ia bloquear — sem notícia com fontes suficientes,
    ou com os tetos (D-085/D-093/D-100) esgotados — e o blog não pode ficar sem post.

    UMA chamada de editor propõe temas intemporais (explicação, guia, contexto; num blog
    de receitas, pratos clássicos) e guarda-os como ideias prioritárias (`origin='rescue'`,
    sem ligação): o job volta à pesquisa NORMAL, onde o reforço por busca (D-063) procura
    as fontes e todos os gates correm como sempre (R41). A própria política editorial já
    prefere o tema intemporal à notícia fraca. Um resgate por CICLO de produção — desde a
    última entrada na fila: dentro de uma corrida nunca há dois, mas uma volta à fila
    (reconciliação, com `JOB_MAX_ATTEMPTS` por teto, ou o botão «Devolver à fila») dá
    direito a um resgate novo (10/11: o requeue do 240 chegou com o resgate já gasto na
    véspera e bloqueou sem nova chance). Devolve True se guardou pelo menos uma pauta.
    """
    if not settings.slot_rescue or not websearch.enabled():
        return False
    with session_scope() as db:
        done = db.execute(text("""SELECT count(*) FROM publication_events
                                   WHERE job_id = :i AND event_type = 'slot_rescue'
                                     AND id > COALESCE((SELECT max(id)
                                                          FROM publication_events
                                                         WHERE job_id = :i
                                                           AND event_type = 'status_changed'
                                                           AND to_status = 'QUEUED'), 0)"""),
                          {"i": job_id}).scalar() or 0
        row = db.execute(text("SELECT site_key, reserve FROM publication_jobs WHERE id = :i"),
                         {"i": job_id}).first()
        site = sites.get(db, row[0]) if row else None
        # O que já falhou ou está nas mãos de alguém não volta como "tema novo".
        burned = list(db.execute(text(
            """SELECT topic FROM ideas WHERE site_key = :s AND status IN ('taken', 'discarded')
                ORDER BY id DESC LIMIT 30"""), {"s": row[0] if row else ""}).scalars())
    if site is None or row is None or done:
        return False
    # A reserva (D-115) usa o mesmo editor, com pautas que só ela pega.
    reserve = bool(row[1])
    origin = "reserve" if reserve else "rescue"
    config = site.get("config") or {}
    if config.get("recipe_mode"):
        kind = (f"Propõe {settings.rescue_topics} pratos CLÁSSICOS e bem documentados "
                "(receitas que qualquer livro de cozinha ensina), de interesse geral")
    else:
        kind = (f"Propõe {settings.rescue_topics} temas INTEMPORAIS — explicação, guia ou "
                "contexto, não notícia — do tema do blog, de alcance MUNDIAL (D-110: que sirvam "
                "a leitores de qualquer país; NENHUM centrado no Brasil nem num país só), "
                "que se confirmem em fontes oficiais "
                "ou institucionais que uma busca na web encontra (órgãos públicos, organismos "
                "internacionais, publicações científicas). Formula o título pelo ASSUNTO, "
                "não por uma organização («higiene do sono: o que dizem as autoridades de "
                "saúde», e não «o que a OMS recomenda»): os termos de uso de alguns sítios "
                "oficiais impedem a leitura das suas páginas, e um tema amarrado a uma "
                "organização ilegível morre sem fontes")
    why = ("O blog precisa de um artigo de RESERVA: fica guardado e só é publicado no dia em "
           "que a notícia não render post, talvez semanas depois — nada de «hoje», «esta "
           "semana» nem datas que envelheçam." if reserve else
           "Hoje nenhuma notícia da fila deu artigo (fontes insuficientes ou reprovação nos "
           "gates), e o blog não pode ficar sem post.")
    task = (
        f"Blog: {site['name']} — {config.get('description', '')}\n"
        f"{why} {kind}, que:\n"
        "- não repitam nem rocem os títulos do bloco «títulos a evitar»;\n"
        "- caibam na política editorial.\n"
        + (f"Categorias do blog: {', '.join(config['categories'])}.\n"
           if config.get("categories") else "")
        + "Para cada tema devolve `topic` (título de trabalho, uma linha, em português do "
          "Brasil), `angle` (o ângulo, em 1 ou 2 frases) e `country` (código ISO de 2 letras "
          "do país de que o tema trata — num prato, o país de origem —, ou «XX» se é mundial)."
    )
    titles = [*_recent_titles(site["site_key"], job_id, now), *burned]
    result = chain.run(
        "researcher", task,
        system="És editor deste blog e planeias o artigo do dia. Segue a política editorial:"
               "\n\n" + str(config.get("policy", "")),
        schema=_RESCUE_SCHEMA, purpose="slot_rescue", site=site["site_key"],
        external=[External("títulos a evitar", "\n".join(titles) or "(nenhum)")],
    )
    stored: list[str] = []
    with session_scope() as db:
        _owned(db, job_id, owner)
        for item in (result.data or {}).get("topics", [])[: settings.rescue_topics]:
            if not isinstance(item, dict):
                continue
            topic = gates.plain_line(str(item.get("topic", "")))
            angle = gates.plain_line(str(item.get("angle", "")))
            if len(topic) < 8:
                continue
            outcome, _ = discovery.store_idea(
                db, site_key=site["site_key"], origin=origin, topic=topic, url=None,
                angle=angle, priority=discovery.RESCUE_PRIORITY, created_by=ACTOR,
                ttl_days=1, now=now, country=str(item.get("country") or ""))
            if outcome == "new":
                stored.append(topic[:160])
        jobs.record_event(db, job_id, "slot_rescue", actor=ACTOR,
                          details={"stored": len(stored), "topics": stored, "origin": origin})
    if not stored:
        log.warning("Job %d: o resgate não guardou pauta nova (D-101)", job_id)
    return bool(stored)


def _provider_backoff(job_id: int, moment: datetime) -> datetime:
    """Quando tentar de novo sem provedor de IA: 30 min a dobrar até o teto (R27, 2/11).

    As esperas anteriores contam-se pelos eventos (chave `provider_wait`, como o
    `_source_retry_at` faz com `source_retry`). O job continua a expirar 24 h
    depois do slot (dispatcher), então o teto não o prende para sempre.
    """
    with session_scope() as db:
        done = db.execute(
            text("""SELECT count(*) FROM publication_events
                     WHERE job_id = :i AND to_status = 'RETRY_WAIT'
                       AND details->>'provider_wait' = 'true'"""), {"i": job_id}).scalar() or 0
    minutes = min(settings.job_retry_minutes * 3 * 2 ** min(int(done), 10),
                  settings.provider_retry_cap_minutes)
    return moment + timedelta(minutes=minutes)


def produce(job_id: int, owner: str, now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    with session_scope() as db:
        # A lease começa a contar quando o worker pega no job.
        if not dispatcher.start_work(db, job_id, owner):
            log.warning("Job %d já não pertence à entrega %s: ignorado", job_id, owner)
            return "STALE"
    declined: list[str] = []            # temas que o redator recusou nesta entrega (D-085)
    exhausted: list[str] = []           # pautas em que a revisão esgotou as reescritas (D-093)
    untranslatable: list[str] = []      # pautas cuja tradução não saiu dos gates (D-100)
    try:
        with quota.for_job(job_id):
            # Pré-checagem (R27, 2/11): sem pesquisador, redator ou revisor de outra família
            # utilizável, a tentativa morreria na revisão — esperar sem gastar pauta, buscas
            # nem chamadas (a cota do codex e o teto do SearXNG foram queimados assim).
            with session_scope() as db:
                site_key = db.execute(text(
                    "SELECT site_key FROM publication_jobs WHERE id = :i"),
                    {"i": job_id}).scalar()
                hold = chain.production_block(db, site_key)
                if hold is None:
                    # Orçamento diário do próprio job (llm_job_cap): um requeue em maratona
                    # não recomeça a gastar se o job já queimou a fatia dele hoje.
                    over = quota.job_blocked(db, job_id)
                    if over:
                        hold = (f"Job {job_id} no orçamento diário de chamadas de IA "
                                f"(llm_job_cap: {vault.limit('llm_job_cap')}); "
                                f"espera a cota do dia renovar")
            if hold is not None:
                raise _NoProviders(hold)
            while True:
                try:
                    return _produce(job_id, owner, moment)
                # Nos tetos (D-085/D-093/D-100), antes de bloquear tenta-se UMA pauta de
                # resgate (D-101): se ela entrar na fila, o laço volta à pesquisa.
                except article.Declined as exc:
                    # A pauta já foi descartada; o job volta à pesquisa por outra, até ao limite.
                    declined.append(str(exc)[:240])
                    if len(declined) >= settings.writer_decline_themes \
                            and not _slot_rescue(job_id, owner, moment):
                        raise _Blocked(str(exc)) from None
                    log.warning("Job %d: %s (%dº tema); procura-se outra pauta",
                                job_id, str(exc)[:200], len(declined))
                except _ReviewExhausted as exc:
                    # A pauta já foi descartada; o job volta à pesquisa por outra, até ao limite.
                    exhausted.append(str(exc)[:240])
                    if len(exhausted) >= settings.review_exhausted_themes \
                            and not _slot_rescue(job_id, owner, moment):
                        raise _Blocked(str(exc), exc.details) from None
                    log.warning("Job %d: revisão esgotada (%dª pauta): %s; procura-se outra",
                                job_id, len(exhausted), str(exc)[:200])
                except _TranslationExhausted as exc:
                    # A pauta já foi descartada; o job volta à pesquisa por outra, até ao limite.
                    untranslatable.append(str(exc)[:240])
                    if len(untranslatable) >= settings.translation_exhausted_themes \
                            and not _slot_rescue(job_id, owner, moment):
                        raise _Blocked(str(exc), exc.details) from None
                    log.warning("Job %d: tradução sem saída (%dª pauta): %s; procura-se outra",
                                job_id, len(untranslatable), str(exc)[:200])
                except _NoIdea:
                    # D-101: a fila esgotou-se sem pauta viável. Se ainda cabe uma volta com
                    # notícias frescas (RETRY_WAIT), ela vem primeiro; senão, o resgate.
                    if _source_retry_at(job_id, moment) is not None \
                            or not _slot_rescue(job_id, owner, moment):
                        raise
                    log.warning("Job %d: sem pauta viável; segue com pauta de resgate (D-101)",
                                job_id)
    except _Stale:
        log.warning("Job %d: lease perdida a meio (entrega %s)", job_id, owner)
        return "STALE"
    except _Blocked as exc:
        reason = str(exc)
        if declined and not reason.startswith("o redator recusou"):
            reason += f" — o redator recusou {len(declined)} tema(s): {declined[-1]}"
        if exhausted and not reason.startswith(("revisão:", "gates:")):
            reason += f" — a revisão esgotou {len(exhausted)} pauta(s): {exhausted[-1]}"
        if untranslatable and not reason.startswith("tradução"):
            reason += (f" — a tradução emperrou em {len(untranslatable)} pauta(s): "
                       f"{untranslatable[-1]}")
        outcome = "BLOCKED"
        fields: dict[str, Any] = {"blocked_reason": reason[:1000]}
        details: dict[str, Any] = {"reason": reason[:500], **exc.details}
        if declined:
            details["declined_themes"] = declined
        if exhausted:
            details["exhausted_themes"] = exhausted
        if untranslatable:
            details["untranslatable_themes"] = untranslatable
        retry_at = _source_retry_at(job_id, moment) if isinstance(exc, _NoIdea) else None
        if retry_at is not None:
            outcome = "RETRY_WAIT"
            details["source_retry"] = True
            fields = {"retry_after_utc": retry_at,
                      "blocked_reason": (f"{reason} — nova tentativa às "
                                         f"{localtime.clock(retry_at)}")[:1000]}
        else:
            # A pauta não morre com o job (M5): se ainda está `taken` (bloqueio pós-redação,
            # pelos gates), volta à fila para outro dia recontar a história; as descartadas
            # e as usadas ficam como estão.
            _release_taken_idea(job_id, owner)
    except (chain.ChainExhausted, _NoProviders) as exc:
        # Sem provedor de IA disponível: não é culpa do artigo. Tenta mais tarde, esperando
        # mais a cada volta (R27: as voltas fixas de 30 min queimavam as cotas do dia).
        reason = str(exc)
        if isinstance(exc, chain.ChainExhausted):
            # A cadeia esgotou a meio (a pré-checagem passou e a cota acabou durante a
            # tentativa): a pauta já redigida volta à fila para a próxima volta retomá-la.
            _release_taken_idea(job_id, owner)
        outcome = "RETRY_WAIT"
        retry_at = _provider_backoff(job_id, moment)
        fields = {"blocked_reason": (f"{reason} — nova tentativa às "
                                     f"{localtime.clock(retry_at)}")[:1000],
                  "retry_after_utc": retry_at}
        details = {"reason": reason[:500], "provider_wait": True}
    try:
        with session_scope() as db:
            jobs.transition(db, job_id, outcome, actor=ACTOR, owner=owner,
                            details=details, **fields)
            if outcome == "BLOCKED":
                # Um job bloqueado não deixa textos "prontos" para trás.
                db.execute(text("UPDATE job_translations SET status = 'failed', updated_at = now() "
                                "WHERE job_id = :j AND status = 'ready'"), {"j": job_id})
    except jobs.LeaseLost:
        return "STALE"
    log.warning("Job %d → %s: %s", job_id, outcome, reason[:300])
    return outcome
