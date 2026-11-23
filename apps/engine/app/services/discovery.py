"""Descoberta de ideias (E6.1): feeds RSS e ideias manuais → `ideas`.

O RSS serve para descobrir e citar (D-013): de cada item guarda-se só o título,
a ligação e a data — nunca o texto nem as imagens do feed. Uma fonte sem RSS
pode ser uma página de notícias (`kind: html`, E14.5i), lida por `listing.py`
com a mesma regra.

Deduplicação, em dois momentos:
- **ao guardar** (por blog): a mesma ligação não entra duas vezes; uma notícia
  com título muito parecido com outra recente do mesmo blog entra como repetição
  dela (`duplicate_of`) — é uma segunda fonte para a mesma história, útil para
  corroborar, não um segundo artigo;
- **ao escolher** (global, entre os seis blogs): `pick` salta uma ideia cuja
  ligação ou título parecido já foi pegado por qualquer blog nos últimos dias.
  Os feeds generalistas partilhados (Senado, BBC Brasil…) entram em vários
  blogs; é aqui que se evita o mesmo artigo em dois sítios.

A semelhança é lexical (palavras do título). Não apanha a mesma notícia em
línguas diferentes: para a corroboração (não para a deduplicação), `same_story`
compara as entidades do título — nomes, siglas, números —, que a tradução mantém
(E14.5g, D-072).
"""
from __future__ import annotations

import pathlib
import re
import unicodedata
from calendar import timegm
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core import cache
from app.core.config import settings
from app.core.logging import get_logger
from app.services import fetcher, listing, terms

log = get_logger(__name__)

MANUAL_PRIORITY = 100
# Pauta de resgate (D-101): à frente das ideias dos feeds, atrás das manuais.
RESCUE_PRIORITY = 90

# --- Cobertura mundial (D-110) ---------------------------------------------------------------
# Regra do código, igual para TODOS os blogs (inclusive os que forem criados): cada ideia sabe de
# que país trata e a escolha recusa o que estoura a cota do blog na janela. O prompt sozinho
# (D-102) não bastou: os feeds nacionais têm mais volume e o Brasil voltava a dominar.
GLOBAL = "XX"           # alcance mundial, vários países ou nenhum país em particular
BRAZIL = "BR"
_COUNTRY = re.compile(r"[A-Z]{2}")
# Jobs cujo `idea_id` é o post do slot (já escrito, em aprovação, aprovado ou publicado).
_COUNTED = ("WRITING", "VALIDATING", "TRANSLATING", "AWAITING_APPROVAL", "APPROVED",
            "PUBLISHING", "VERIFY_REQUIRED", "PUBLISHED_CONFIRMED")


def country_code(value: Any) -> str:
    """Código ISO de 2 letras do país de que a ideia trata; o resto vira `XX` (mundial)."""
    code = str(value or "").strip().upper()
    return code if _COUNTRY.fullmatch(code) else GLOBAL


def country_counts(db: Session, site_key: str, now: datetime) -> Counter[str]:
    """Posts do blog por país na janela da D-110 (`world_window_days`), contando os já
    produzidos para os próximos slots."""
    since = now - timedelta(days=settings.world_window_days)
    rows = db.execute(
        text("""SELECT i.country FROM publication_jobs j JOIN ideas i ON i.id = j.idea_id
                 WHERE j.site_key = :s AND j.due_at_utc >= :since AND j.status = ANY(:st)
                   AND NOT j.reserve"""),
        {"s": site_key, "since": since, "st": list(_COUNTED)}).scalars().all()
    return Counter(country_code(c) for c in rows if c)


def over_quota(country: str, counts: Counter[str]) -> str:
    """Motivo pelo qual uma ideia deste país estouraria a cota (D-110), ou ""."""
    if country == BRAZIL and counts[BRAZIL] >= settings.world_brazil_max:
        return (f"cota do Brasil cheia ({counts[BRAZIL]} de {settings.world_brazil_max} em "
                f"{settings.world_window_days} dias)")
    if country != GLOBAL and counts[country] >= settings.world_country_max:
        return (f"cota de {country} cheia ({counts[country]} de {settings.world_country_max} "
                f"em {settings.world_window_days} dias)")
    return ""
SIMILARITY = 0.6
FEED_ACCEPT = "application/rss+xml, application/atom+xml, application/xml, text/xml, */*;q=0.5"
PAGE_ACCEPT = "text/html, application/xhtml+xml, */*;q=0.5"
# Tipo de fonte (`kind` no ficheiro de feeds → `sources.source_type`).
SOURCE_KINDS = ("rss", "html")

# Parâmetros de seguimento: não fazem parte da identidade de um artigo.
_TRACKING = re.compile(r"^(utm_|fbclid$|gclid$|mc_|ref$|ref_|igshid$|cmpid$|ocid$|at_)", re.I)
_STOPWORDS = frozenset({
    "a", "o", "as", "os", "um", "uma", "uns", "umas", "de", "do", "da", "dos", "das", "em", "no",
    "na", "nos", "nas", "por", "para", "com", "sem", "e", "ou", "que", "se", "ao", "aos", "à",
    "às", "é", "são", "foi", "ser", "mais", "como", "sobre", "após", "entre", "the", "of", "and",
    "to", "in", "for", "on", "with", "at", "by", "from", "is", "are", "was", "el", "la", "los",
    "las", "del", "y", "en", "un", "una",
})


class SourceConfigError(ValueError):
    pass


# --- Normalização --------------------------------------------------------------------------


def canonical_url(url: str, *, keep_www: bool = False) -> str | None:
    """Forma canónica de uma ligação, ou None se não for `https` utilizável.

    Sem o `www.`: é a IDENTIDADE da ligação (deduplicação). Para ir buscar a página usa-se
    `keep_www=True` (R42: há sítios que só respondem com o `www.`)."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme.lower() != "https" or not parts.hostname:
        return None
    # Alguns feeds (Folha) dão um endereço de redirecionamento com o destino depois de "*":
    # o artigo é o destino, e o redirecionador é proibido a robôs.
    if "/*https:/" in parts.path:
        target = parts.path.split("/*", 1)[1]
        target = re.sub(r"^https:/(?!/)", "https://", target)
        return canonical_url(target + (f"?{parts.query}" if parts.query else ""),
                             keep_www=keep_www)
    host = parts.hostname.lower()
    if not keep_www:
        host = host.removeprefix("www.")
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                             if not _TRACKING.match(k)))
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/") or "/"
    return urlunsplit(("https", host, path, query, ""))


def title_tokens(title: str) -> frozenset[str]:
    plain = unicodedata.normalize("NFKD", title.lower())
    plain = "".join(ch for ch in plain if not unicodedata.combining(ch))
    words = re.findall(r"[a-z0-9]+", plain)
    return frozenset(w for w in words if len(w) > 2 and w not in _STOPWORDS)


# --- Entidades do título (E14.5g): a mesma notícia em línguas diferentes -------------------

# Siglas que mudam com a língua: tudo vai para a forma em português.
_ENTITY_ALIASES = {"un": "onu", "who": "oms", "us": "eua", "usa": "eua", "eu": "ue",
                   "imf": "fmi", "nato": "otan", "ecb": "bce"}
# Aparecem em quase todos os títulos de tecnologia: não dizem que é a mesma notícia.
_ENTITY_IGNORED = frozenset({"ai", "ia", "ceo", "cfo", "cto", "tv", "pc", "api"})
_ENTITY_JACCARD = 0.3


def _fold(word: str) -> str:
    plain = unicodedata.normalize("NFKD", word)
    return "".join(ch for ch in plain if not unicodedata.combining(ch)).lower()


def title_entities(title: str) -> frozenset[str]:
    """Nomes, siglas e números do título, na forma minúscula e sem acentos.

    Palavras seguidas com maiúscula formam um nome só («Palo Alto Networks»); uma sigla, um
    nome com maiúscula no meio («GitHub») ou um número ficam sozinhos.
    """
    found: set[str] = set()
    run: list[str] = []

    def flush() -> None:
        if run:
            name = _fold(" ".join(run))
            name = _ENTITY_ALIASES.get(name, name)
            if name not in _ENTITY_IGNORED:
                found.add(name)
            run.clear()

    for token in re.findall(r"\w[\w.\-+]*\w|\w|[^\w\s]", title):
        word = token.strip(".-+")
        alone = bool(re.search(r"\d", word)) or (len(word) >= 2 and word.isupper())
        if word[:1].isalnum() and (alone or re.search(r"[a-z][A-Z]", word)
                                   or (word[0].isupper() and len(word) >= 3)):
            if alone:
                flush()
                run.append(word)
                flush()
            else:
                run.append(word)
        else:
            flush()
    flush()
    return frozenset(found)


def same_story(a: frozenset[str], b: frozenset[str], common: frozenset[str]) -> bool:
    """Dois títulos (em línguas diferentes) contam a mesma notícia?

    Pelo menos 2 entidades em comum, uma delas rara na janela (não está em `common`: a
    Microsoft ou o Google sozinhos não fazem a mesma notícia) e não só um número.
    """
    shared = a & b
    if len(shared) < 2 or len(shared) / len(a | b) < _ENTITY_JACCARD:
        return False
    return any(not e.isdigit() and e not in common for e in shared)


def title_key(title: str) -> str:
    return " ".join(sorted(title_tokens(title)))


def similar(key_a: str, key_b: str) -> bool:
    a, b = set(key_a.split()), set(key_b.split())
    if len(a) < 3 or len(b) < 3:
        return False               # títulos curtos demais para comparar com segurança
    return len(a & b) / len(a | b) >= SIMILARITY


# --- Fontes: content-config/feeds → tabela `sources` ---------------------------------------


def load_feeds(known_sites: set[str],
               directory: str | pathlib.Path | None = None) -> dict[str, list[dict[str, Any]]]:
    """Lê e valida `content-config/feeds/*.yaml`: {site_key: [feed normalizado, ...]}.

    Só os blogs com ficheiro aparecem (um blog sem ficheiro não tem os feeds mexidos)."""
    root = pathlib.Path(directory or settings.feeds_config_dir)
    if not root.is_dir():
        raise SourceConfigError(f"diretório de feeds não encontrado: {root}")
    out: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(root.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        site = data.get("site_key")
        if site != path.stem:
            raise SourceConfigError(f"{path.name}: site_key diferente do nome do ficheiro")
        if site not in known_sites:
            raise SourceConfigError(f"{path.name}: site desconhecido")
        feeds: list[dict[str, Any]] = []
        for feed in data.get("feeds", []):
            url = str(feed.get("url", ""))
            if not url.startswith("https://"):
                raise SourceConfigError(f"{path.name}: feed sem https: {url[:80]}")
            kind = str(feed.get("kind", "rss"))
            if kind not in SOURCE_KINDS:
                raise SourceConfigError(f"{path.name}: tipo de fonte desconhecido: {kind}")
            if any(f["feed_url"] == url for f in feeds):
                raise SourceConfigError(f"{path.name}: feed repetido: {url[:80]}")
            feeds.append({"name": feed["name"], "feed_url": url,
                          "domain": feed.get("domain") or urlsplit(url).hostname,
                          "source_type": kind, "tier": feed["tier"], "lang": feed["lang"],
                          "usage_notes": feed.get("notes", "")})
        out[str(site)] = feeds
    return out


def sync_sources(db: Session, directory: str | pathlib.Path | None = None) -> int:
    """Copia os feeds validados para `sources`. Um feed que saiu do ficheiro é desligado.

    E18.3a: em produção o `migrate` já não chama isto (só semeia, `services/editorial.py`);
    fica para os testes e para recriar uma base do zero."""
    known_sites = set(db.execute(text("SELECT site_key FROM sites")).scalars())
    total = 0
    for site, feeds in load_feeds(known_sites, directory).items():
        urls = []
        for feed in feeds:
            url = feed["feed_url"]
            urls.append(url)
            db.execute(
                text("""INSERT INTO sources (site_key, name, feed_url, domain, source_type,
                                             tier, lang, usage_notes, enabled)
                        VALUES (:s, :n, :u, :d, :k, :t, :l, :notes, true)
                        ON CONFLICT (site_key, feed_url) DO UPDATE
                           SET name = EXCLUDED.name, domain = EXCLUDED.domain,
                               source_type = EXCLUDED.source_type,
                               tier = EXCLUDED.tier, lang = EXCLUDED.lang,
                               usage_notes = EXCLUDED.usage_notes, enabled = true"""),
                {"s": site, "n": feed["name"], "u": url, "d": feed["domain"],
                 "k": feed["source_type"], "t": feed["tier"], "l": feed["lang"],
                 "notes": feed["usage_notes"]},
            )
            total += 1
        db.execute(text("UPDATE sources SET enabled = false "
                        "WHERE site_key = :s AND NOT (feed_url = ANY(:urls))"),
                   {"s": site, "urls": urls})
    return total


# --- Descoberta ---------------------------------------------------------------------------


@dataclass
class DiscoveryResult:
    sources: int = 0
    failed: int = 0
    new: int = 0
    duplicates: int = 0          # mesma história, outra fonte (ficam como corroboração)
    known: int = 0               # ligação já vista neste blog
    skipped: int = 0             # sem data/ligação utilizável, ou antigos
    errors: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"sources": self.sources, "failed": self.failed, "new": self.new,
                "duplicates": self.duplicates, "known": self.known, "skipped": self.skipped,
                "errors": self.errors}


def _entry_date(entry: Any) -> datetime | None:
    """Data do item em UTC. O feedparser dá a hora em UTC (`*_parsed`): `timegm`, não
    `mktime` — com `TZ=America/Sao_Paulo` no container, toda data ficava 3 h no futuro (22/11).
    Data fora do padrão do RSS que o feedparser não entende («November 19, 2026 15:57:58
    +0000», Tax Foundation: 0 pautas desde sempre) é lida por `_loose_date`."""
    # `dict.get` direto: o `get` do feedparser mapeia `updated_*` para `published_*` com aviso.
    fields = entry if isinstance(entry, dict) else {}
    for attr in ("published_parsed", "updated_parsed"):
        parsed = dict.get(fields, attr)
        if parsed:
            try:
                return datetime.fromtimestamp(timegm(parsed), tz=UTC)
            except (OverflowError, ValueError):
                return None
    for attr in ("published", "updated"):
        raw = dict.get(fields, attr)
        if not raw:
            continue
        found = _loose_date(str(raw))
        if found is not None:
            return found
    return None


# Formatos fora do RFC 822 vistos em feeds reais (o ISO 8601 vai pelo `fromisoformat`).
_LOOSE_FORMATS = ("%B %d, %Y %H:%M:%S %z", "%B %d, %Y %H:%M %z", "%B %d, %Y",
                  "%d %B %Y %H:%M:%S %z", "%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S")


def _loose_date(raw: str) -> datetime | None:
    text_ = raw.strip()
    try:
        found = datetime.fromisoformat(text_)
    except ValueError:
        found = None
        for fmt in _LOOSE_FORMATS:
            try:
                found = datetime.strptime(text_, fmt)
                break
            except ValueError:
                continue
    if found is None:
        return None
    return (found if found.tzinfo else found.replace(tzinfo=UTC)).astimezone(UTC)


def store_idea(db: Session, *, site_key: str, origin: str, topic: str, url: str | None,
               published_at: datetime | None = None, source_id: int | None = None,
               angle: str = "", priority: int = 0, created_by: str | None = None,
               ttl_days: int | None = None, timeless: bool = False,
               now: datetime | None = None, country: str | None = None) -> tuple[str, int | None]:
    """Guarda uma ideia aplicando a deduplicação do blog. Devolve (resultado, id).

    resultado: `new`, `duplicate` (repetição de outra recente do blog) ou `known`
    (a ligação já existia neste blog).
    """
    moment = now or datetime.now(UTC)
    canonical = canonical_url(url) if url else None
    # O endereço a buscar guarda o `www.` original (R42); a identidade continua sem ele.
    fetchable = canonical_url(url, keep_www=True) if url else None
    key = title_key(topic)
    if canonical and db.execute(
        text("SELECT 1 FROM ideas WHERE site_key = :s AND canonical_url = :u"),
        {"s": site_key, "u": canonical},
    ).first():
        return "known", None

    primary = None
    recent = db.execute(
        text("""SELECT id, title_key FROM ideas
                 WHERE site_key = :s AND duplicate_of IS NULL AND created_at >= :since
                 ORDER BY id"""),
        {"s": site_key, "since": moment - timedelta(days=settings.idea_window_days)},
    ).all()
    for idea_id, other_key in recent:
        if similar(key, other_key):
            primary = idea_id
            break

    idea_id = db.execute(
        text("""INSERT INTO ideas (site_key, origin, topic, angle, source_urls, canonical_url,
                                   priority, expires_at, source_id, published_at, title_key,
                                   duplicate_of, created_by, timeless, country)
                VALUES (:s, :o, :t, :a, :urls, :u, :p, :exp, :src, :pub, :k, :dup, :by, :tl, :c)
                RETURNING id"""),
        {"s": site_key, "o": origin, "t": topic[:500], "a": angle[:2000],
         "urls": [fetchable or canonical] if canonical else [], "u": canonical, "p": priority,
         "exp": moment + timedelta(days=ttl_days or settings.idea_ttl_days), "src": source_id,
         "pub": published_at, "k": key, "dup": primary, "by": created_by, "tl": timeless,
         "c": country_code(country) if country is not None else None},
    ).scalar_one()
    return ("duplicate" if primary else "new"), int(idea_id)


def _feed_items(body: bytes) -> list[tuple[str, str, datetime | None]]:
    # feedparser recebe bytes: não vai à rede por conta própria.
    parsed = feedparser.parse(body)
    return [(str(entry.get("title", "")), str(entry.get("link", "")), _entry_date(entry))
            for entry in parsed.entries]


def _page_items(page: fetcher.Fetched) -> list[tuple[str, str, datetime | None]]:
    return [(item.title, item.link, item.published)
            for item in listing.parse(page.text, page.url)]


def _newest_first(items: list[tuple[str, str, datetime | None]]
                  ) -> list[tuple[str, str, datetime | None]]:
    """Os mais novos primeiro (sem data no fim), antes do corte de `feed_max_items`. Há feeds
    em ordem cronológica: o do Health Canada traz 525 itens desde 2020 e os 30 primeiros eram
    todos velhos — 0 pautas em 14 dias (22/11)."""
    floor = datetime.min.replace(tzinfo=UTC)
    return sorted(items, key=lambda item: item[2] or floor, reverse=True)


def discover_site(db: Session, site_key: str, now: datetime | None = None) -> DiscoveryResult:
    """Lê os feeds (e as páginas de notícias) de um blog e guarda as recentes como ideias."""
    moment = now or datetime.now(UTC)
    oldest = moment - timedelta(days=settings.idea_max_age_days)
    result = DiscoveryResult()
    sources = db.execute(
        text("SELECT id, name, feed_url, source_type, lang FROM sources WHERE site_key = :s "
             "AND enabled ORDER BY id"), {"s": site_key},
    ).all()
    for source_id, name, feed_url, kind, lang in sources:
        result.sources += 1
        is_page = kind == "html"
        try:
            page = fetcher.fetch(feed_url, accept=PAGE_ACCEPT if is_page else FEED_ACCEPT,
                                 language=lang)
            if page.status != 200:
                raise fetcher.FetchError("http_status", str(page.status))
        except fetcher.FetchError as exc:
            result.failed += 1
            result.errors[name] = exc.kind
            db.execute(text("""UPDATE sources SET last_checked_at = :n, last_status = :st,
                                      fail_count = fail_count + 1 WHERE id = :i"""),
                       {"n": moment, "st": exc.kind, "i": source_id})
            continue
        items = _newest_first(_page_items(page) if is_page else _feed_items(page.body))
        stored = 0
        for raw_title, raw_link, date in items[: settings.feed_max_items]:
            title = re.sub(r"\s+", " ", raw_title).strip()
            link = canonical_url(raw_link)
            in_window = date is not None and oldest <= date <= moment + timedelta(days=1)
            if not title or not link or not in_window:
                result.skipped += 1
                continue
            outcome, _ = store_idea(db, site_key=site_key, origin="rss", topic=title, url=link,
                                    published_at=date, source_id=source_id, now=moment)
            stored += 1
            if outcome == "new":
                result.new += 1
            elif outcome == "duplicate":
                result.duplicates += 1
            else:
                result.known += 1
        db.execute(text("""UPDATE sources SET last_checked_at = :n, last_status = 'ok',
                                  fail_count = 0, last_items = :c WHERE id = :i"""),
                   {"n": moment, "c": stored, "i": source_id})
    return result


def add_manual(db: Session, site_key: str, topic: str, *, angle: str = "",
               url: str | None = None, created_by: str, priority: int = MANUAL_PRIORITY,
               ttl_days: int | None = None, timeless: bool = False) -> tuple[str, int | None]:
    """Ideia introduzida por uma pessoa: prioritária, mas passa os mesmos gates.

    `timeless` (E18.2): tema intemporal — a busca de reforço aceita páginas de qualquer data."""
    topic = re.sub(r"\s+", " ", topic).strip()
    if len(topic) < 8:
        raise ValueError("o tema é curto demais")
    if url and canonical_url(url) is None:
        raise ValueError("a ligação tem de ser https")
    if not db.execute(text("SELECT 1 FROM sites WHERE site_key = :s"), {"s": site_key}).first():
        raise ValueError(f"site desconhecido: {site_key}")
    return store_idea(db, site_key=site_key, origin="manual", topic=topic, url=url, angle=angle,
                      priority=priority, created_by=created_by, ttl_days=ttl_days,
                      timeless=timeless)


# --- Escolha (dedup global) ------------------------------------------------------------------


@dataclass(frozen=True)
class Picked:
    id: int
    topic: str
    angle: str
    origin: str
    # A ligação da ideia e as das repetições (outras fontes da mesma história).
    source_urls: tuple[str, ...]
    lang: str = ""          # língua do feed de origem (pt, en, es); vazia nas manuais
    timeless: bool = False  # E18.2: pauta manual intemporal (busca sem filtro de data)


def expire_disabled(db: Session) -> int:
    """Tira da fila as ideias de feeds desligados (23/11).

    Um feed desligado (poda, robots.txt, portal) deixava as ideias dele na fila: 1158 em 23/11,
    857 só na Política. Elas expiram — nada se apaga — salvo a história que um feed ainda
    ligado também conta (uma repetição dela é de feed ligado): essa fica, com as fontes.
    """
    return len(db.execute(text(
        """UPDATE ideas i SET status = 'expired'
             FROM sources s
            WHERE s.id = i.source_id AND NOT s.enabled AND i.status = 'new'
              AND NOT EXISTS (SELECT 1 FROM ideas d JOIN sources ds ON ds.id = d.source_id
                               WHERE d.duplicate_of = i.id AND ds.enabled)
        RETURNING i.id""")).all())


def pick(db: Session, site_key: str, now: datetime | None = None, *,
         reserve: bool = False) -> Picked | None:
    """Escolhe a próxima ideia do blog e marca-a como pegada.

    Ordem: manuais primeiro, depois as mais recentes. Salta (e descarta) a ideia
    cuja ligação ou título parecido já foi pegado por qualquer blog na janela.
    À mesma prioridade, pauta de fonte legível vai antes da de domínio sem-leitura
    (D-070) sem corroboração: essa parte de 0 fontes e precisa de 2 só da busca —
    na Saúde, 46% do estoque nascia assim e queimava as tentativas do dia (5/11).

    A reserva (D-115) só pega as pautas dela (`origin = 'reserve'`), e os slots nunca as pegam.
    """
    moment = now or datetime.now(UTC)
    since = moment - timedelta(days=settings.idea_window_days)
    db.execute(text("UPDATE ideas SET status = 'expired' "
                    "WHERE status = 'new' AND expires_at < :n"), {"n": moment})
    expire_disabled(db)
    taken = db.execute(
        text("""SELECT canonical_url, title_key FROM ideas
                 WHERE status IN ('taken', 'used') AND created_at >= :since"""),
        {"since": since},
    ).all()
    taken_urls = {u for u, _ in taken if u}
    taken_keys = [k for _, k in taken if k]
    counts = country_counts(db, site_key, moment)

    rows = db.execute(
        # Manuais primeiro; depois as histórias contadas por mais sítios DIFERENTES (várias
        # páginas do mesmo sítio não corroboram nada); depois as de feeds do tema, antes das
        # de feeds generalistas; depois as mais recentes.
        text("""SELECT i.id, i.topic, i.angle, i.origin, i.canonical_url, i.title_key, s.lang,
                       COALESCE(i.source_urls[1], i.canonical_url) AS fetch_url,
                       i.priority, i.timeless, i.country,
                       (SELECT count(DISTINCT substring(d.canonical_url FROM 'https://([^/]+)'))
                          FROM ideas d
                         WHERE d.duplicate_of = i.id
                           AND substring(d.canonical_url FROM 'https://([^/]+)')
                               IS DISTINCT FROM substring(i.canonical_url FROM 'https://([^/]+)')
                       ) AS corroboration
                  FROM ideas i LEFT JOIN sources s ON s.id = i.source_id
                 WHERE i.site_key = :s AND i.status = 'new' AND i.duplicate_of IS NULL
                   AND (i.origin = 'reserve') = :reserve
                 ORDER BY i.priority DESC,
                          -- as já triadas com boa nota antes das que ainda não foram triadas
                          (i.relevance IS NOT NULL) DESC,
                          corroboration DESC,
                          i.relevance DESC NULLS LAST,
                          (COALESCE(s.usage_notes, '') LIKE 'feed generalista%') ASC,
                          i.published_at DESC NULLS LAST, i.id
                 FOR UPDATE OF i SKIP LOCKED"""),
        {"s": site_key, "reserve": reserve},
    ).all()

    def handicapped(url: str | None, corroboration: int) -> bool:
        # Sem fonte legível nem corroboração, a pauta depende só da busca (D-070).
        if not url or corroboration:
            return False
        try:
            return not terms.readable(url)
        except terms.TermsConfigError:
            return False      # sem a lista não há desempate; a leitura falha mais adiante

    # Ordenação ESTÁVEL: só reagrupa por legibilidade dentro da mesma prioridade; o resto
    # da ordem (triagem, corroboração, nota, recência) continua a do SQL.
    candidates = sorted(rows, key=lambda r: (-r.priority, handicapped(r.canonical_url,
                                                                      r.corroboration)))
    held = 0
    for (idea_id, topic, angle, origin, url, key, lang, fetch_url, _priority, timeless, country,
         _corroboration) in candidates:
        if (url and url in taken_urls) or any(similar(key, other) for other in taken_keys):
            db.execute(text("UPDATE ideas SET status = 'discarded' WHERE id = :i OR "
                            "duplicate_of = :i"), {"i": idea_id})
            continue
        # D-110: a pauta manual é escolha do editor e passa; as outras só com o país conhecido
        # (a de feed sem país espera a triagem) e dentro da cota do blog. A recusada fica na
        # fila: volta a valer quando a janela andar.
        if origin != "manual":
            if country is None and origin not in ("rescue", "reserve"):
                held += 1
                continue
            if country is not None and over_quota(country_code(country), counts):
                held += 1
                continue
        extra = db.execute(
            text("""UPDATE ideas SET status = 'taken' WHERE duplicate_of = :i
                    RETURNING COALESCE(source_urls[1], canonical_url)"""), {"i": idea_id},
        ).scalars().all()
        db.execute(text("UPDATE ideas SET status = 'taken' WHERE id = :i"), {"i": idea_id})
        urls = tuple(dict.fromkeys(u for u in (fetch_url, *extra) if u))
        if held:
            log.info("Escolha de %s: %d ideia(s) retidas pela cota por país (D-110)", site_key,
                     held)
        return Picked(id=int(idea_id), topic=topic, angle=angle, origin=origin, source_urls=urls,
                      lang=lang or "", timeless=bool(timeless))
    if held:
        log.warning("Escolha de %s: nada fora da cota por país; %d ideia(s) retidas (D-110)",
                    site_key, held)
    return None


# --- Triagem de relevância (D-040) -----------------------------------------------------------

_TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {"scores": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"}, "score": {"type": "integer"},
                       "country": {"type": "string"}},
        "required": ["id", "score", "country"], "additionalProperties": False}},
        "groups": {"type": "array", "items": {"type": "array", "items": {"type": "integer"}}}},
    "required": ["scores", "groups"], "additionalProperties": False,
}

_WORLD_TRIAGE = (
    "Em `country`, o código ISO de 2 letras do país de que o título trata (BR, US, FR, CN…); "
    "«XX» quando o assunto é mundial, de vários países ou de nenhum em particular (um "
    "produto, uma descoberta científica, um organismo internacional).\n"
    "O blog cobre o MUNDO (D-110), não o Brasil: notícia de interesse só local ou nacional — "
    "de QUALQUER país, inclusive o Brasil —, que nada diz a leitores de outros países (um "
    "órgão, um tribunal, um governo estadual ou municipal, um balanço de empresa local, um "
    "hospital) leva 1 a 4. Fora disso, o país não dá nem tira pontos.\n")


def triage(db: Session, site: dict[str, Any], now: datetime | None = None) -> dict[str, int]:
    """Dá uma nota de pertinência (0–10) às ideias novas do blog, numa só chamada.

    Só os títulos seguem para o modelo, como conteúdo externo delimitado. As ideias
    manuais e as de resgate (D-101) não são triadas (foram escolhidas de propósito).
    Abaixo de `triage_min_relevance` a ideia é descartada, com as suas repetições.

    A mesma chamada agrupa os títulos que contam a mesma história (D-043): jornais
    diferentes dão títulos que a comparação por palavras não emparelha, e sem esse
    par a ideia fica com uma só fonte. As agrupadas passam a repetições da de melhor
    nota — é só uma pista de onde ir ler: a pesquisa continua a exigir que cada
    fonte sustente factos do tema.
    """
    from app.services.ai import chain
    from app.services.ai.prompting import External

    moment = now or datetime.now(UTC)
    expire_disabled(db)              # não gasta nota em ideia de feed desligado
    # O lote reveza as fontes; a de rendimento baixo entra a cada `triage_source_penalty`
    # voltas (config.py, 22/11).
    rows = db.execute(
        text("""WITH yield AS (
                   SELECT source_id, avg(relevance) AS average, count(*) AS scored
                     FROM ideas
                    WHERE site_key = :s AND relevance IS NOT NULL AND source_id IS NOT NULL
                      AND created_at >= :yield_since
                    GROUP BY source_id),
                 queue AS (
                   SELECT id, topic, source_id, published_at,
                          ROW_NUMBER() OVER (PARTITION BY source_id
                                             ORDER BY published_at DESC NULLS LAST, id) AS turn
                     FROM ideas
                    WHERE site_key = :s AND status = 'new' AND duplicate_of IS NULL
                      AND relevance IS NULL AND origin NOT IN ('manual', 'rescue', 'reserve'))
                 SELECT q.id, q.topic FROM queue q LEFT JOIN yield y ON y.source_id = q.source_id
                  ORDER BY q.turn * CASE WHEN y.scored >= :min_scored AND y.average < :low
                                         THEN :penalty ELSE 1 END,
                           q.published_at DESC NULLS LAST, q.id
                  LIMIT :n"""),
        {"s": site["site_key"], "n": settings.triage_batch,
         "yield_since": moment - timedelta(days=settings.triage_yield_days),
         "min_scored": settings.triage_source_min_scored,
         "low": settings.triage_source_low_avg, "penalty": settings.triage_source_penalty},
    ).all()
    if not rows:
        return {"scored": 0, "discarded": 0, "grouped": 0}
    # Ideias já triadas e ainda na fila: não voltam a receber nota, mas podem ser a outra
    # metade de uma história que só agora chegou por outro feed.
    older = db.execute(
        text("""SELECT id, topic, relevance FROM ideas
                 WHERE site_key = :s AND status = 'new' AND duplicate_of IS NULL
                   AND relevance IS NOT NULL AND origin NOT IN ('manual', 'rescue', 'reserve')
                 ORDER BY published_at DESC NULLS LAST, id LIMIT :n"""),
        {"s": site["site_key"], "n": settings.triage_batch // 2},
    ).all()
    # P3 do ensaio E14 (3/11): a reentrada no `_research` (pauta esgotada pela revisão, volta do
    # RETRY_WAIT) não re-triagem o que acabou de ser triado — as ideias chegadas entretanto
    # esperam a próxima janela. O cache só vale enquanto houver ideias já triadas na fila
    # (`older`): sem elas, as novas precisam de nota já, senão o `pick` pegava pauta sem nota.
    if settings.triage_cache_minutes and older and cache.cache_get(f"triage:{site['site_key']}"):
        return {"scored": 0, "discarded": 0, "grouped": 0, "cached": 1}
    listing = "\n".join(f"{idea_id}: {topic}" for idea_id, topic in rows)
    if older:
        listing += "\n\nJá avaliados (não precisam de nota; só para agrupar):\n" + "\n".join(
            f"{idea_id}: {topic}" for idea_id, topic, _ in older)
    config = site.get("config") or {}
    if config.get("recipe_mode"):
        # Modo receita (D-080): o blog só publica receitas, então vale o prato que se ensina.
        task = (
            f"Blog: {site['name']} — {config.get('description', '')}\n"
            "Abaixo está uma lista de títulos, um por linha, no formato `id: título`. O blog "
            "publica uma receita completa por dia. Para cada título, dá uma nota de 0 a 10 de "
            "pertinência:\n"
            "- 8 a 10: é um prato ou receita concreta que se ensina passo a passo (ex.: «bolo de "
            "cenoura», «risoto de cogumelos»), de interesse geral;\n"
            "- 5 a 7: é um prato, mas muito regional, trabalhoso ou especializado;\n"
            "- 1 a 4: fala de comida sem ser um prato (um restaurante, um chef, uma tendência, "
            "um evento, um produto);\n"
            "- 0: é de outro assunto.\n"
            "Em `country`, o código ISO de 2 letras do país de origem do prato (BR, IT, FR, "
            "MX…); «XX» quando não tem origem definida. O blog cobre a cozinha do MUNDO "
            "(D-110): o país não dá nem tira pontos.\n"
            "Avalia só pelo título. Devolve uma nota para cada id da lista.\n"
            "Em `groups`, junta os ids dos títulos que são do MESMO prato (a mesma receita de "
            "sítios ou línguas diferentes). Só quando for claramente o mesmo prato. Cada id entra "
            "no máximo num grupo; se não houver nenhum, devolve uma lista vazia.")
    else:
        # P1 do ensaio de eficiência (3/11): nos blogs `official_only` a pesquisa exige fontes
        # oficiais; pauta sem lastro institucional reprova DEPOIS, caro (busca + pesquisa por
        # ideia — job 164: 10 pautas, 0 artigo). A triagem, que custa uma chamada só, rebaixa
        # essas pautas antes.
        official = (
            "Este blog só publica o que se confirma em fontes oficiais (órgãos de governo, "
            "organismos internacionais, revistas científicas, agências públicas de notícias). "
            "Rebaixa para 1 a 4 os títulos sem lastro institucional — estudo isolado, dica ou "
            "matéria de estilo de vida contada por um só veículo comercial —, mesmo que sejam "
            "do tema; 8 a 10 só quando há um ato, anúncio, dado ou publicação de órgão oficial, "
            "organismo ou revista científica por trás do título.\n"
            if ((config.get("search_policy") or {}).get("official_only")) else "")
        task = (
            f"Blog: {site['name']} — {config.get('description', '')}\n"
            "Abaixo está uma lista de títulos de notícias, um por linha, no formato `id: título`. "
            "Para cada um, dá uma nota de 0 a 10 de pertinência para ESTE blog:\n"
            "- 8 a 10: é claramente do tema do blog e interessa aos seus leitores;\n"
            "- 5 a 7: é do tema, mas de interesse limitado ou muito especializado;\n"
            "- 1 a 4: toca no tema só de passagem;\n"
            "- 0: é de outro assunto (por exemplo, eleições ou futebol num blog de finanças).\n"
            + official + _WORLD_TRIAGE +
            "Avalia só pelo título. Devolve uma nota para cada id da lista.\n"
            "Em `groups`, junta os ids dos títulos que contam a MESMA história (o mesmo "
            "acontecimento, noticiado por veículos diferentes ou em mais de um título). Só quando "
            "for claramente o mesmo acontecimento; o mesmo tema geral não chega. Cada id entra no "
            "máximo num grupo; se não houver nenhum, devolve uma lista vazia.")
    # R42: a leitura acaba aqui. A chamada ao modelo leva dezenas de segundos; a transação
    # não fica aberta («idle in transaction») à espera dela. A escrita das notas abre outra.
    db.commit()
    result = chain.run(
        "researcher", task,
        system="És editor deste blog e fazes a triagem das notícias do dia.",
        schema=_TRIAGE_SCHEMA, purpose="triage", site=site["site_key"],
        external=[External("títulos candidatos", listing)],
    )
    if settings.triage_cache_minutes:
        cache.cache_set(f"triage:{site['site_key']}", 1, ttl=settings.triage_cache_minutes * 60)
    known = {int(idea_id) for idea_id, _ in rows}
    scored = discarded = 0
    for item in (result.data or {}).get("scores", []):
        try:
            idea_id, score = int(item["id"]), max(0, min(10, int(item["score"])))
        except (KeyError, TypeError, ValueError):
            continue
        if idea_id not in known:
            continue                      # o modelo não inventa ideias
        known.discard(idea_id)
        scored += 1
        db.execute(text("UPDATE ideas SET relevance = :r, country = :c WHERE id = :i"),
                   {"r": score, "c": country_code(item.get("country")), "i": idea_id})
        if score < settings.triage_min_relevance:
            discarded += 1
            db.execute(text("UPDATE ideas SET status = 'discarded' "
                            "WHERE id = :i OR duplicate_of = :i"), {"i": idea_id})
    # Agrupamento: só ids enviados, ainda na fila, cada um num único grupo.
    current = db.execute(
        text("SELECT id, relevance, title_key FROM ideas WHERE id = ANY(:ids) "
             "AND status = 'new' AND duplicate_of IS NULL AND relevance IS NOT NULL"),
        {"ids": [int(r[0]) for r in (*rows, *older)]}).all()
    relevance = {int(i): int(r) for i, r, _ in current}
    words = {int(i): set((key or "").split()) for i, _, key in current}
    grouped = 0
    for group in (result.data or {}).get("groups", []):
        if not isinstance(group, list):
            continue
        members: list[int] = []
        for value in group:
            try:
                idea_id = int(value)
            except (TypeError, ValueError):
                continue
            if idea_id in relevance and idea_id not in members:
                members.append(idea_id)
        if len(members) < 2:
            continue
        leader = min(members, key=lambda i: (-relevance[i], i))
        # O modelo propõe, o código confere: um membro tem de partilhar palavras do título com
        # a líder (o mesmo tema geral, noutra língua ou com outras palavras, não entra), e
        # um grupo não engole a fila toda.
        members = [leader, *[
            i for i in members
            if i != leader and len(words[i] & words[leader]) >= settings.triage_group_overlap
        ]][: settings.triage_group_max]
        if len(members) < 2:
            continue
        for idea_id in members:
            relevance.pop(idea_id)                     # não entra noutro grupo
            if idea_id != leader:
                grouped += 1
                db.execute(text("UPDATE ideas SET duplicate_of = :l "
                                "WHERE id = :i OR duplicate_of = :i"), {"l": leader, "i": idea_id})
    return {"scored": scored, "discarded": discarded, "grouped": grouped}
