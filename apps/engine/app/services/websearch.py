"""Reforço de pesquisa por busca na web (D-063, E14.5b).

Os feeds continuam a ser o caminho principal. Só quando uma ideia não chega a `min_sources`
é que se procuram mais páginas sobre o MESMO assunto no SearXNG (instância própria, só na rede
interna). Daqui saem apenas ligações: o texto é lido depois pelo `fetcher.py` (HTTPS, SSRF,
robots.txt) e o modelo continua sem ferramentas. Regras que não se negociam:

- desligado por omissão (`SEARXNG_URL` vazio);
- a consulta é só o título da ideia, em inglês (D-064), e também em português e na língua da
  fonte (E14.5g, D-072); nunca texto de rascunho;
- cache curto e teto diário de consultas (D-063);
- qualquer falha (rede, timeout, JSON, teto) devolve lista vazia: nunca bloqueia o motor.
"""
from __future__ import annotations

import contextlib
import hashlib
from datetime import date, datetime, time, timedelta
from typing import Any

import httpx
import redis
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core import cache
from app.core.config import settings
from app.core.localtime import local_zone
from app.core.logging import get_logger
from app.services import discovery, terms, vault
from app.services.ai import chain
from app.services.ai.prompting import External
from app.services.urls import domain_of, under

log = get_logger(__name__)

# Para os testes: troca o transporte HTTP sem ligar à rede.
_transport: httpx.BaseTransport | None = None

# Redes sociais e agregadores não são fontes (E14.5d acrescenta a lista completa e as regras de
# Saúde e Política).
_BLOCKED_DOMAINS = (
    "facebook.com", "instagram.com", "x.com", "twitter.com", "tiktok.com", "youtube.com",
    "pinterest.com", "reddit.com", "linkedin.com", "wikipedia.org", "news.google.com",
    "quora.com", "medium.com", "scribd.com", "slideshare.net", "t.me", "whatsapp.com",
    "weibo.com", "bilibili.com", "vk.com",
)

# Blogs com `search_policy.official_only` (Saúde e Política): só estes domínios servem de reforço.
# Último rótulo de governo/educação/organismo internacional, penúltimo rótulo de um país
# (gov.br, gob.mx, gouv.fr, ac.uk, jus.br, leg.br, go.jp, govt.nz, gv.at) e uma lista de
# organismos multilaterais, governos e parlamentos de outros países com domínio próprio (E14.5h,
# D-073: órgãos oficiais de qualquer país valem, dizendo sempre a jurisdição), revistas
# científicas e agências. O blog acrescenta os seus (`allow_domains` no YAML do site).
# Sites sem leitura (D-070) saem depois, em `terms.readable`.
_OFFICIAL_LAST = {"gov", "edu", "mil", "int"}
_OFFICIAL_SECOND = {"gov", "gob", "gouv", "gub", "edu", "ac", "jus", "leg", "mp", "mil", "gc",
                    "go", "govt", "gv"}
_OFFICIAL_DOMAINS = (
    # Nações Unidas e outros organismos multilaterais
    "un.org", "paho.org", "unicef.org", "unaids.org", "unfpa.org", "unhcr.org", "wfp.org",
    "fao.org", "iaea.org", "ilo.org", "unesco.org", "undp.org", "unep.org", "oecd.org", "imf.org",
    "worldbank.org", "wto.org", "bis.org", "iadb.org", "caf.com", "oas.org", "osce.org",
    "icj-cij.org", "europa.eu",
    # Governos, parlamentos e saúde pública de outros países fora do padrão gov/gob/gouv
    "parliament.uk", "nhs.uk", "bundestag.de", "bundesregierung.de", "bund.de",
    "bundesgesundheitsministerium.de", "rki.de", "assemblee-nationale.fr", "senat.fr",
    "elysee.fr", "gouvernement.fr", "santepubliquefrance.fr", "has-sante.fr", "congreso.es",
    "senado.es", "canada.ca", "admin.ch", "korea.kr", "minsal.cl", "parlamento.pt", "dgs.pt",
    "governo.it",
    # Ciência e agências
    "nih.gov", "fiocruz.br", "scielo.br", "thelancet.com", "bmj.com", "nejm.org", "science.org",
    "cell.com", "plos.org", "cochrane.org", "reuters.com", "apnews.com",
    "agenciabrasil.ebc.com.br",
)

_QUERY_SCHEMA = {
    "type": "object",
    "properties": {"query_en": {"type": "string"}, "query_pt": {"type": "string"}},
    "required": ["query_en", "query_pt"],
    "additionalProperties": False,
}
# Língua da ideia (`sources.lang`) → parâmetro `language` do SearXNG.
_SEARX_LANGUAGE = {"pt": "pt-BR", "en": "en", "es": "es"}


def enabled() -> bool:
    return bool(settings.searxng_url.strip())


def _blocked(domain: str) -> bool:
    return under(domain, _BLOCKED_DOMAINS)


def is_official(domain: str, allow: list[str] | None = None) -> bool:
    """Domínio de governo, parlamento, tribunal, universidade, organismo ou fonte aprovada."""
    labels = domain.split(".")
    return (labels[-1] in _OFFICIAL_LAST
            or (len(labels) >= 2 and len(labels[-1]) == 2 and labels[-2] in _OFFICIAL_SECOND)
            or under(domain, _OFFICIAL_DOMAINS)
            or under(domain, [d.lower().removeprefix("www.") for d in (allow or [])]))


def queries(topic: str, *, site: dict[str, Any]) -> dict[str, str]:
    """O título da ideia como consulta curta em inglês (D-064) e em português, numa só chamada.

    Se a IA falhar, o próprio título nas duas.
    """
    key = f"websearch:q2:{hashlib.sha1(discovery.title_key(topic).encode()).hexdigest()}"
    cached = cache.cache_get(key)
    if isinstance(cached, dict) and cached.get("en") and cached.get("pt"):
        return {"en": str(cached["en"]), "pt": str(cached["pt"])}
    found = {"en": topic, "pt": topic}
    try:
        result = chain.run(
            "researcher",
            "Reescreve o título de notícia abaixo como duas consultas curtas de motor de busca "
            "(5 a 12 palavras cada): `query_en` em inglês e `query_pt` em português do Brasil, "
            "mantendo nomes próprios, produtos e números. Sem aspas, sem operadores e sem "
            "acrescentar factos.",
            system="És um editor que prepara consultas de pesquisa.",
            schema=_QUERY_SCHEMA, purpose="search_query", site=site["site_key"],
            external=[External("título", topic)],
        )
        for lang in ("en", "pt"):
            candidate = str((result.data or {}).get(f"query_{lang}", "")).strip()
            if 3 <= len(candidate) <= 200:
                found[lang] = candidate
    except chain.ChainExhausted:
        # Sem IA nenhuma não é busca degradada: sobe, e a produção espera (R27, 2/11 —
        # buscar com o título cru aqui queimava o teto do SearXNG sem artigo possível).
        raise
    except Exception as exc:  # noqa: BLE001 — a busca é um reforço: nunca bloqueia
        log.info("Consultas de busca indisponíveis (%s); usa-se o título", type(exc).__name__)
        # Falha transitória não fica 24 h no cache: a próxima tentativa pergunta de novo
        # à IA, em vez de buscar o dia inteiro com o título pt-BR cru (revisão E14).
        return found
    cache.cache_set(key, found, ttl=24 * 3600)
    return found


def _rounds(topic: str, lang: str, asked: dict[str, str],
            recipe: bool = False) -> list[tuple[str, str, str, str]]:
    """Consultas por ordem: (língua do SearXNG, consulta, categoria, filtro de tempo).

    A língua da fonte primeiro (uma notícia brasileira tem as outras fontes em português), depois
    o inglês (D-064) e o português dos blogs; as notícias em inglês, sem filtro de tempo, no fim.
    """
    own = _SEARX_LANGUAGE.get(lang, "")
    plan: list[tuple[str, str, str, str]] = []
    if recipe:
        # Modo receita (D-080): receitas não têm data de notícia, então sem filtro de tempo.
        plan.append(("pt-BR", f"receita {asked['pt']}", "general", ""))
        plan.append(("en", f"recipe {asked['en']}", "general", ""))
        if own == "es":
            plan.append(("es", f"receta {topic}", "general", ""))
        return plan
    if own == "es":
        plan.append(("es", topic, "general", "week"))
    if own == "pt-BR":
        plan.append(("pt-BR", asked["pt"], "general", "week"))
    plan.append(("en", asked["en"], "general", "week"))
    if own != "pt-BR":
        plan.append(("pt-BR", asked["pt"], "general", "week"))
    plan.append(("en", asked["en"], "news", ""))
    return plan


def _ask(client: httpx.Client, query: str, *, category: str, time_range: str,
         language: str = "en") -> list[str]:
    """Uma consulta ao SearXNG. Devolve ligações HTTPS, na ordem dos resultados."""
    params = {"q": query, "format": "json", "language": language, "categories": category}
    if time_range:
        params["time_range"] = time_range
    response = client.get(settings.searxng_url.rstrip("/") + "/search", params=params)
    response.raise_for_status()
    urls = []
    for item in response.json().get("results", []):
        url = str(item.get("url", ""))
        if url.lower().startswith("https://"):
            urls.append(url)
    return urls


_CAPPED_AT = "sb:searxng:capped_at:"


def _mark_capped() -> None:
    """Guarda a hora em que a fila normal bateu no teto do dia (a 1.ª vez), para o resumo."""
    with contextlib.suppress(redis.RedisError):
        cache.redis_client.set(f"{_CAPPED_AT}{date.today().isoformat()}",
                               datetime.now(local_zone()).strftime("%H:%M"),
                               nx=True, ex=172_800)


def usage(db: Session, local_date: date) -> dict[str, Any]:
    """As buscas do dia para o resumo: consultas gastas, teto, reserva do resgate, hora em
    que a fila normal bateu no teto e os jobs cujas pautas ficaram sem busca por isso."""
    stamp = local_date.isoformat()
    try:
        used = int(cache.redis_client.get(f"counter:searxng_queries:{stamp}") or 0)
        capped_at = cache.redis_client.get(f"{_CAPPED_AT}{stamp}")
    except redis.RedisError:
        used, capped_at = 0, None
    cap = vault.limit("searxng_daily_cap")
    start = datetime.combine(local_date, time(0), tzinfo=local_zone())
    rows = db.execute(text("""
        SELECT e.job_id, j.site_key, count(*) AS ideas
          FROM publication_events e JOIN publication_jobs j ON j.id = e.job_id
         WHERE e.event_type = 'search_reinforcement'
           AND e.timestamp_utc >= :start AND e.timestamp_utc < :end
           AND (e.details->>'capped')::boolean AND NOT (e.details->>'job_capped')::boolean
           AND (e.details->>'queries')::int = 0
         GROUP BY e.job_id, j.site_key ORDER BY e.job_id"""),
        {"start": start, "end": start + timedelta(days=1)}).mappings().all()
    return {"used": used, "cap": cap, "reserve": rescue_reserve(cap),
            "capped_at": str(capped_at) if capped_at else "", "jobs": [dict(r) for r in rows]}


def rescue_reserve(daily: int) -> int:
    """Consultas do teto do dia que só o resgate gasta: `searxng_rescue_reserve`, no máximo 1/4."""
    return max(0, min(settings.searxng_rescue_reserve, daily // 4))


def _spend(job_id: int | None = None, pocket: str = "") -> str:
    """Gasta uma consulta dos tetos diários. Devolve `""` se gastou, ou o teto que travou.

    Além do teto global, cada job tem um orçamento diário próprio (`searxng_job_cap`): um
    job de pauta difícil não pode esgotar as consultas do dia inteiro e deixar os outros
    `capped` (4/11: o requeue do 174 gastou as 60 sozinho e a estreia da Saúde bloqueou).
    `pocket` separa o orçamento do job em bolsos: a pauta de resgate (D-101) nasce sem
    fontes e chega exatamente quando o job já gastou o bolso normal na fila inteira —
    sem bolso próprio ela morria `job_capped` com 0 consultas (10/11: requeue do 240).
    O teto do dia vale para todos os bolsos; o resgate é 1 por job, logo o custo extra
    fica limitado a mais um `searxng_job_cap`. O bolso `rescue` também tem as últimas
    `rescue_reserve` consultas do dia só para ele (23/11).
    """
    used, daily = cache.get_daily("searxng_queries"), vault.limit("searxng_daily_cap")
    if used >= daily:
        _mark_capped()
        return "daily"
    # As últimas consultas do dia ficam para o resgate (23/11, #404): ele chega quando a fila
    # normal já falhou e nasce sem fontes, então sem busca o job bloqueia de certeza.
    if pocket != "rescue" and used >= daily - rescue_reserve(daily):
        _mark_capped()
        return "daily"
    job_cap = vault.limit("searxng_job_cap")
    budgeted = job_id is not None and job_cap > 0
    key = f"searxng_queries:job:{job_id}" + (f":{pocket}" if pocket else "")
    if budgeted and cache.get_daily(key) >= job_cap:
        return "job"
    cache.incr_daily("searxng_queries")
    if budgeted:
        cache.incr_daily(key)
    return ""


def search(topic: str, *, site: dict[str, Any], exclude: list[str] | None = None,
           need: int = 2, stats: dict[str, Any] | None = None, lang: str = "",
           recipe: bool = False, job_id: int | None = None, pocket: str = "") -> list[str]:
    """Ligações de reforço para o assunto. Lista vazia se desligado, no teto ou em qualquer
    falha — menos a falta total de IA (`ChainExhausted`), que sobe para a produção esperar (R27).

    Consultas por `_rounds`: a categoria geral, com filtro de tempo, na língua da fonte (`lang`),
    em inglês e em português; só se ainda faltarem ligações, as notícias em inglês, sem filtro
    de tempo (o Bing News responde vazio com ele). Para assim que houver domínios bastantes.
    Uma ligação por domínio, sem redes sociais e sem os domínios de `exclude` (as fontes que a
    ideia já tem). `stats`, se vier, recebe `queries` (consultas gastas), `languages` (as línguas
    consultadas), `cached`, `capped`, `job_capped` e `failed` para o registro persistente da
    medição (E14.5e). `job_id`, se vier, debita também o orçamento diário do job (`_spend`);
    `pocket` escolhe o bolso desse orçamento (a pauta de resgate usa o seu, ver `_spend`).
    """
    info: dict[str, Any] = stats if stats is not None else {}
    info.update(queries=0, languages=[], cached=False, capped=False, job_capped=False,
                failed=False)
    if not enabled():
        return []
    skip = {domain_of(u) for u in (exclude or [])}
    limit = settings.recipe_search_results if recipe else settings.searxng_max_results
    policy = (site.get("config") or {}).get("search_policy") or {}
    official_only = bool(policy.get("official_only"))
    allow = list(policy.get("allow_domains") or [])

    def usable(url: str) -> bool:
        """Passaria no filtro final? Só domínios assim contam para parar de consultar."""
        domain = domain_of(url)
        return bool(domain) and domain not in skip and not _blocked(domain) \
            and (not official_only or is_official(domain, allow)) and terms.readable(url)

    digest = hashlib.sha1(discovery.title_key(topic).encode()).hexdigest()
    key = f"websearch:r:{site['site_key']}:{lang or '-'}:{'rec:' if recipe else ''}{digest}"
    links = cache.cache_get(key)
    info["cached"] = links is not None
    if links is None:
        try:
            asked = queries(topic, site=site)
            found: list[str] = []
            # Um cliente para todas as rondas: sem um handshake novo por consulta.
            with httpx.Client(timeout=settings.searxng_timeout_s, transport=_transport) as client:
                for language, query, category, time_range in _rounds(topic, lang, asked, recipe):
                    hit = _spend(job_id, pocket)
                    if hit:
                        if hit == "job":
                            log.warning("Orçamento de consultas do job %s atingido (%d)",
                                        job_id, vault.limit("searxng_job_cap"))
                            info["job_capped"] = True
                        else:
                            log.warning("Teto diário de consultas ao SearXNG atingido (%d)",
                                        vault.limit("searxng_daily_cap"))
                        info["capped"] = True
                        break
                    info["queries"] += 1
                    info["languages"].append(language)
                    found += _ask(client, query, category=category, time_range=time_range,
                                  language=language)
                    # Para de consultar só com domínios UTILIZÁVEIS bastantes: os vetados, as
                    # redes sociais, os que já são fonte e — em blogs `official_only` — os não
                    # oficiais não adiantam, pois o filtro final os tira (ensaio 2, risoto).
                    if len({domain_of(u) for u in found if usable(u)}) >= \
                            min(limit, need + (3 if recipe else 2)):
                        break
            links = found
        except chain.ChainExhausted:
            raise               # sem IA nenhuma a produção deve esperar (R27), ver `queries`
        except Exception as exc:  # noqa: BLE001 — nunca bloqueia o motor
            log.warning("Busca de reforço falhou (%s): %s", type(exc).__name__, str(exc)[:120])
            info["failed"] = True
            return []
        cache.cache_set(key, links, ttl=settings.searxng_cache_hours * 3600)
    picked: list[str] = []
    seen = set(skip)
    try:
        # O ramo do cache não passa pelo try de cima, e o `usable` lê o sem-leitura.yaml
        # (TermsConfigError se o volume faltar): o contrato «qualquer falha devolve lista
        # vazia, nunca bloqueia o motor» vale também aqui.
        for url in links:
            domain = domain_of(url)
            if domain in seen or not usable(url):
                continue
            seen.add(domain)
            picked.append(url)
            if len(picked) >= limit:
                break
    except Exception as exc:  # noqa: BLE001 — nunca bloqueia o motor
        log.warning("Filtro do reforço falhou (%s): %s", type(exc).__name__, str(exc)[:120])
        info["failed"] = True
        return []
    return picked
