"""Os passos de um artigo (E6.3 pesquisa, E6.5 redação, E6.6 revisão, E6.8 tradução).

Cada passo é uma chamada à cadeia de provedores de IA com saída estruturada.
Regras que atravessam todos:
- o texto das fontes é conteúdo externo: entra sempre por `external` (blocos
  delimitados, E3.9), nunca misturado nas instruções;
- as instruções vêm do código e da política editorial do blog, nunca das fontes;
- a secção "Fontes e créditos" é montada pelo código a partir das fontes
  realmente lidas — o modelo não a escreve nem a pode alterar;
- quem escreve nunca revê: o revisor recebe a família de quem escreveu.
"""
from __future__ import annotations

import html as html_lib
import io
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from pypdf import PdfReader

from app.core.config import settings
from app.core.logging import get_logger
from app.services import discovery, fetcher, gates, read_failures, terms
from app.services import recipe as recipes
from app.services.ai import chain
from app.services.ai.prompting import External
from app.services.urls import domain_of
from app.utils.text import slugify

log = get_logger(__name__)

LANGUAGES = {"en": "inglês", "es": "espanhol", "zh-CN": "chinês simplificado"}
SOURCES_HEADING = {"pt-BR": "Fontes e créditos", "en": "Sources and credits",
                   "es": "Fuentes y créditos", "zh-CN": "来源与致谢"}
ACCESSED = {"pt-BR": "consultado em", "en": "accessed on", "es": "consultado el",
            "zh-CN": "访问日期"}


@dataclass(frozen=True)
class Source:
    url: str
    name: str            # domínio ou nome da publicação
    title: str
    text: str            # texto extraído, limitado; só para o modelo ler, nunca é guardado
    accessed_at: datetime
    published: datetime | None = None    # data lida na página (os resultados de busca não trazem)
    recipe: recipes.RecipeRef | None = None    # receita estruturada (modo receita, D-080)


@dataclass
class Research:
    sources: list[Source]
    facts: list[dict[str, Any]] = field(default_factory=list)
    divergences: list[str] = field(default_factory=list)
    # Páginas lidas que não tratam do tema (o modelo disse que não): não contam nem se listam.
    off_topic: list[str] = field(default_factory=list)
    # Domínios das fontes que vieram do reforço por busca (D-063), para o painel de aprovação.
    via_search: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Draft:
    title: str
    excerpt: str
    html: str            # corpo, sem a secção de fontes
    category: str
    writer: str          # provedor que escreveu
    writer_family: str


@dataclass(frozen=True)
class Review:
    approved: bool
    score: int
    blocking: list[str]              # impedem o artigo de seguir
    notes: list[str]                 # reparos menores: seguem para quem aprova
    unsupported_claims: list[str]    # bloqueiam sempre
    reviewer: str

    @property
    def to_fix(self) -> list[str]:
        """O que o redator recebe para corrigir: primeiro o que bloqueia, depois o resto."""
        return [*self.unsupported_claims, *self.blocking, *self.notes]


class StepFailed(Exception):
    """Um passo não produziu um resultado utilizável (motivo pronto para `blocked_reason`).

    `family`: família do modelo cujo resultado falhou, quando se sabe — a repetição
    tenta primeiro outra família (B da D-101)."""

    def __init__(self, reason: str, *, family: str | None = None) -> None:
        super().__init__(reason)
        self.family = family


# --- Fontes -----------------------------------------------------------------------------------


# Domínios que nunca são fonte de corroboração: redes sociais, encurtadores, partilha.
_NOT_A_SOURCE = (
    "twitter.com", "x.com", "t.co", "facebook.com", "instagram.com", "linkedin.com",
    "youtube.com", "youtu.be", "tiktok.com", "reddit.com", "whatsapp.com", "telegram.me",
    "t.me", "pinterest.com", "bit.ly", "google.com", "apple.com", "play.google.com",
    # Fóruns de discussão: comentários de utilizadores não corroboram nada (visto no teste real
    # de 2026-10-04, em que uma discussão do Hacker News entrou como 4.ª fonte).
    "news.ycombinator.com", "lobste.rs", "slashdot.org", "stackoverflow.com", "quora.com",
    # Espelhos e cópias de outra página: não são outra fonte.
    "archive.org", "archive.ph", "archive.today", "translate.goog", "ampproject.org",
)


# Segundos níveis genéricos sob um código de país (com.br, gov.br, co.uk…).
_SECOND_LEVEL = frozenset({
    "com", "org", "net", "gov", "edu", "mil", "co", "ac", "gob", "jus", "leg", "mp", "blog",
    "art", "ind", "inf", "eco", "emp", "tv", "or", "ne", "go",
})
# Alojamentos em que cada subdomínio é uma publicação diferente.
_HOSTING = ("github.io", "substack.com", "medium.com", "wordpress.com", "blogspot.com",
            "readthedocs.io")
# O mesmo editor com mais de um domínio.
_SAME_PUBLISHER = {"folha.uol.com.br": "folha.com.br", "bbc.co.uk": "bbc.com",
                   "exame.com.br": "exame.com"}
# Páginas de serviço de um sítio (entrar, assinar, reportar erro): nunca são fonte.
_SERVICE_HOST = frozenset({
    "login", "logout", "assinaturas", "assinatura", "assine", "assinante", "account", "accounts",
    "auth", "sso", "checkout", "pay", "ads", "anuncie", "newsletter", "newsletters", "cadastro",
    "paywall",
})
_SERVICE_PATH = re.compile(
    r"(?:^|/)(?:login|logout|signin|sign-in|signup|sign-up|register|cadastro|cadastre-se|assine|"
    r"assine-ja|assinatura|assinaturas|subscribe|subscription|feedback|fale-conosco|contato|"
    r"contact|privacy|privacy-policy|privacidade|politica-de-privacidade|termos|termos-de-uso|"
    r"terms|newsletter|newsletters|anuncie|comunicar-erro|comunique-erros)(?:/|$|\.)",
    re.IGNORECASE)


def _shingles(text: str, size: int = 5) -> set[tuple[str, ...]]:
    words = re.findall(r"\w+", text.lower())
    return {tuple(words[i:i + size]) for i in range(max(0, len(words) - size + 1))}


def same_text(a: str, b: str) -> bool:
    """Dois textos são a mesma peça (republicação, agência, tradução literal não conta)?

    Mede quanto do texto mais curto aparece no outro, em sequências de cinco palavras.
    Um jornal que reproduz a Agência Brasil ou o The Conversation não é uma segunda fonte.
    """
    first, second = _shingles(a), _shingles(b)
    if not first or not second:
        return False
    return len(first & second) / min(len(first), len(second)) >= 0.5


def _site(url: str) -> str:
    """O sítio (domínio registável) de um endereço: `www1.folha.uol.com.br` e
    `login.folha.com.br` são o mesmo. É por sítio que se conta a corroboração.

    Aproximação sem a lista pública de sufixos: na dúvida junta de mais (conta
    menos fontes), nunca de menos.
    """
    host = (urlsplit(url).hostname or "").lower().rstrip(".")
    for suffix, publisher in _SAME_PUBLISHER.items():
        if host == suffix or host.endswith("." + suffix):
            return publisher
    labels = host.split(".")
    keep = 2
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        keep = 3
    base = ".".join(labels[-keep:])
    if base in _HOSTING and len(labels) > keep:
        return ".".join(labels[-(keep + 1):])
    return base


def is_service_page(url: str) -> bool:
    """Página inicial, de entrada, de assinatura ou de contacto: não sustenta facto nenhum."""
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.path in ("", "/"):
        return True
    if any(label in _SERVICE_HOST for label in host.split(".")[:-2]):
        return True
    return bool(_SERVICE_PATH.search(parts.path))


def extract(page: fetcher.Fetched) -> tuple[str, str, list[str]]:
    """(título, texto, ligações citadas) de uma página, sem navegação, scripts nem estilos.

    As ligações são as que o corpo do artigo cita para outros sítios: muitas vezes é
    o documento original (o anúncio do fornecedor, o aviso oficial, o estudo).
    """
    soup = BeautifulSoup(page.body, "html.parser")
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "aside", "form",
                     "iframe", "svg"]):
        tag.decompose()
    title = re.sub(r"\s+", " ", soup.title.get_text(" ", strip=True)) if soup.title else ""
    main = soup.find("article") or soup.find("main") or soup.body or soup
    text = re.sub(r"\s+", " ", main.get_text(" ", strip=True))
    own = _site(page.url)
    cited: list[str] = []
    for anchor in main.find_all("a", href=True):
        try:
            url = discovery.canonical_url(urljoin(page.url, str(anchor["href"])))
        except ValueError:
            continue                                      # href malformado
        domain = domain_of(url) if url else ""
        if (not url or not domain or _site(url) == own
                or any(domain == d or domain.endswith("." + d) for d in _NOT_A_SOURCE)
                or is_service_page(url)):
            continue
        if url not in cited:
            cited.append(url)
    return title[:300], text[: settings.source_max_chars], cited[: settings.cited_links_per_page]


_PDF_MAX_PAGES = 40           # um relatório inteiro não cabe no corte de chars; parar cedo


def extract_pdf(page: fetcher.Fetched) -> tuple[str, str, datetime | None]:
    """(título, texto, data) de um PDF — relatórios oficiais e estudos vêm assim (5/11).

    Um PDF da web é entrada hostil: qualquer falha (corrompido, cifrado, sem texto)
    devolve texto vazio e a fonte fica de fora, como uma página vazia. Só as primeiras
    `_PDF_MAX_PAGES` páginas, até o corte normal de chars das fontes.
    """
    try:
        reader = PdfReader(io.BytesIO(page.body))
        if reader.is_encrypted:
            return "", "", None
        parts: list[str] = []
        total = 0
        for sheet in reader.pages[:_PDF_MAX_PAGES]:
            piece = sheet.extract_text() or ""
            parts.append(piece)
            total += len(piece)
            if total >= settings.source_max_chars:
                break
        meta = reader.metadata
        title = str(meta.title).strip() if meta and meta.title else ""
        published = meta.creation_date if meta else None
    except Exception as exc:  # noqa: BLE001 — nunca derrubar a produção por um PDF estranho
        log.info("PDF ilegível (%s): %s", type(exc).__name__, page.url[:120])
        return "", "", None
    if published is not None and published.tzinfo is None:
        published = published.replace(tzinfo=UTC)
    if not title:
        # Sem título nos metadados, o nome do ficheiro serve de identificação.
        name = urlsplit(page.url).path.rsplit("/", 1)[-1]
        title = re.sub(r"[-_]+", " ", re.sub(r"\.pdf$", "", name, flags=re.I)).strip()
    text = re.sub(r"[ \t]+", " ", "\n".join(parts)).strip()
    return title[:300], text[: settings.source_max_chars], published


_META_DATES = ("article:published_time", "og:article:published_time", "datepublished",
               "pubdate", "publishdate", "date", "dc.date", "dc.date.issued", "parsely-pub-date")
_JSON_LD_DATE = re.compile(r'"datePublished"\s*:\s*"([^"]{8,40})"')


def _parse_date(value: str) -> datetime | None:
    value = (value or "").strip()
    if not re.match(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value[:10])
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def page_date(body: bytes) -> datetime | None:
    """Data de publicação lida na página (meta, JSON-LD ou <time>); None se não houver."""
    text_body = body.decode("utf-8", errors="replace")
    soup = BeautifulSoup(text_body, "html.parser")
    for meta in soup.find_all("meta"):
        key = str(meta.get("property") or meta.get("name") or meta.get("itemprop") or "").lower()
        if key in _META_DATES and (parsed := _parse_date(str(meta.get("content", "")))):
            return parsed
    found = _JSON_LD_DATE.search(text_body)
    if found and (parsed := _parse_date(found.group(1))):
        return parsed
    tag = soup.find("time", attrs={"datetime": True})
    return _parse_date(str(tag["datetime"])) if tag else None


def read_sources(urls: list[str], now: datetime, *, fresh_urls: frozenset[str] = frozenset(),
                 max_age_days: int = 0, skipped: list[str] | None = None,
                 with_recipe: bool = False, base: list[Source] | None = None,
                 transient: list[str] | None = None) -> list[Source]:
    """Vai buscar cada fonte pelo fetcher seguro. As que falham ficam de fora.

    Primeiro as ligações da ideia (o artigo-fonte e as repetições noutros feeds);
    se ainda couberem mais, as ligações que essas páginas citam — é assim que se
    chega ao documento original quando ele não tem feed (R38). Uma fonte por
    sítio (domínio registável): corroborar é ouvir outra fonte, não a mesma duas vezes.
    `fresh_urls`: ligações vindas da busca (D-063). Os resultados não trazem data, por isso
    lê-se na página; uma com data mais velha que `max_age_days` não corrobora uma notícia de hoje.
    PDFs contam como fonte (5/11): relatórios oficiais e estudos vêm assim; o texto sai do
    `extract_pdf` e a data dos metadados do ficheiro (sem data, não corrobora pela idade).
    Sítios cujos termos ou `robots.txt` não deixam entregar a página a uma IA (D-070) ficam
    só como pauta: não se lêem nem se citam; `skipped`, se vier, recebe essas ligações.
    `with_recipe` (modo receita, D-080): só ficam as páginas com `schema.org/Recipe` utilizável,
    que servem para conferir a receita escrita.
    `base`: fontes já lidas numa chamada anterior (o reforço, D-063, só lê as ligações novas);
    entram no resultado e os seus domínios não se repetem.
    `transient`, se vier, recebe as ligações que falharam por motivo passageiro (tempo,
    rede, 429 ou erro 5xx do servidor): a ideia delas pode valer noutra hora (R42).
    """
    sources: list[Source] = list(base or [])
    seen_domains: set[str] = {_site(s.url) for s in sources}
    cited: list[str] = []

    def read(url: str) -> None:
        domain = _site(url)
        if not domain or domain in seen_domains:
            return
        if not terms.readable(url):
            log.info("Fonte só de pauta (termos de uso, D-070): %s", url[:120])
            if skipped is not None:
                skipped.append(url)
            return
        try:
            page = fetcher.fetch(url, ai_agents=settings.research_ai_agents,
                                 max_bytes_pdf=settings.fetch_max_bytes_pdf)
        except fetcher.FetchError as exc:
            log.info("Fonte ignorada (%s): %s", exc.kind, url[:120])
            read_failures.failed(domain, exc.kind, now)
            if skipped is not None and exc.kind == "ai_disallowed":
                skipped.append(url)
            if transient is not None and exc.kind in ("timeout", "network_error"):
                transient.append(url)
            return
        if page.status != 200:
            read_failures.failed(domain, f"http_{page.status}", now)
        if transient is not None and (page.status == 429 or page.status >= 500):
            transient.append(url)
            return
        ctype = page.content_type.lower()
        is_pdf = "application/pdf" in ctype
        if page.status != 200:
            return
        if "html" not in ctype and not is_pdf:
            read_failures.failed(domain, "content_type", now)
            return
        links: list[str] = []
        if is_pdf:
            # PDFs (5/11): a busca da Saúde devolve relatórios oficiais e estudos em PDF
            # que eram descartados pelo tipo. Receita, não: só página com schema.org/Recipe.
            if with_recipe:
                log.info("Fonte sem receita estruturada (modo receita): %s", url[:120])
                return
            title, text, published = extract_pdf(page)
        else:
            title, text, links = extract(page)
            published = page_date(page.body)
        if len(text) < 200:
            read_failures.failed(domain, "no_text", now)
            return                           # página vazia, paywall ou só JavaScript
        read_failures.read_ok(domain, now)
        ref = recipes.parse_recipe(page.body) if with_recipe else None
        if with_recipe and ref is None:
            log.info("Fonte sem receita estruturada (modo receita): %s", url[:120])
            return
        if published and published > now + timedelta(days=1):
            published = None                 # data no futuro: lixo, não conta
        if (url in fresh_urls and max_age_days and published
                and now - published > timedelta(days=max_age_days)):
            log.info("Fonte de busca ignorada (publicada em %s): %s", published.date(), url[:120])
            return
        final_domain = _site(page.url)
        # O destino de um redirecionamento passa pelos mesmos filtros (uma ligação pode
        # acabar na página de entrada ou na página inicial do sítio, ou noutro sítio).
        if final_domain in seen_domains or (page.url != url and (
                is_service_page(page.url) or not terms.readable(page.url))):
            return
        seen_domains.update({domain, final_domain})
        if any(same_text(text, other.text) for other in sources):
            log.info("Fonte ignorada (republicação de outra já lida): %s", url[:120])
            return
        sources.append(Source(url=page.url, name=domain_of(page.url), title=title, text=text,
                              accessed_at=now, published=published, recipe=ref))
        cited.extend(link for link in links if link not in cited)

    for url in urls:
        if len(sources) >= settings.max_sources:
            return sources
        read(url)
    tried = 0
    for url in list(cited):                  # só as citadas pelas fontes da ideia, não em cadeia
        if len(sources) >= settings.max_sources or tried >= settings.cited_links_tried:
            break
        if _site(url) in seen_domains:
            continue
        tried += 1
        read(url)
    return sources


def _external(sources: list[Source]) -> list[External]:
    return [External(f"fonte {i} — {s.name} — {s.url}", f"{s.title}\n\n{s.text}")
            for i, s in enumerate(sources, start=1)]


def sources_section(sources: list[Source], language: str) -> str:
    """Secção "Fontes e créditos" (spec §10.1), montada pelo código."""
    items = "".join(
        f'<li><a href="{html_lib.escape(s.url, quote=True)}" rel="noopener noreferrer nofollow">'
        f"{html_lib.escape(s.title or s.name)}</a> — {html_lib.escape(s.name)} "
        f"({ACCESSED[language]} {s.accessed_at.date().isoformat()})</li>"
        for s in sources
    )
    return f"<h2>{SOURCES_HEADING[language]}</h2><ul>{items}</ul>"


# --- Pesquisa (E6.3) --------------------------------------------------------------------------

_RESEARCH_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {"type": "array", "items": {
            "type": "object",
            "properties": {"claim": {"type": "string"}, "source": {"type": "integer"},
                           "also": {"type": "array", "items": {"type": "integer"}}},
            "required": ["claim", "source", "also"], "additionalProperties": False}},
        "divergences": {"type": "array", "items": {"type": "string"}},
        "on_topic": {"type": "boolean"},
        # Uma entrada por fonte lida: a página trata do próprio tema (E14, D-059)?
        "sources": {"type": "array", "items": {
            "type": "object",
            "properties": {"source": {"type": "integer"}, "covers_topic": {"type": "boolean"}},
            "required": ["source", "covers_topic"], "additionalProperties": False}},
    },
    "required": ["facts", "divergences", "on_topic", "sources"], "additionalProperties": False,
}


class OffTopic(StepFailed):
    """A notícia não é do tema do blog (vem de um feed generalista, por exemplo)."""


def _index(value: Any, count: int) -> int | None:
    """Número de fonte dado pelo modelo, se for um inteiro válido."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if 1 <= number <= count else None


def research(topic: str, sources: list[Source], *, site: dict[str, Any], job_id: int) -> Research:
    """Levanta os factos. Devolve só as fontes que sustentam pelo menos um facto:
    uma página lida que não sustenta nada não é fonte, não conta para o mínimo nem
    aparece em "Fontes e créditos"."""
    result = chain.run(
        "researcher",
        "O tema da notícia está no bloco «tema». Lê as fontes abaixo e extrai os factos "
        "relevantes para o tema. Cada "
        "facto é uma frase tua, curta e verificável, com o número da fonte que o sustenta em "
        "`source` e, em `also`, os números das outras fontes que afirmam o mesmo (lista vazia "
        "se nenhuma). Só "
        "factos que estejam nas fontes. Se as fontes se contradizem, descreve a divergência em "
        "`divergences`, dizendo o que cada uma afirma.\n"
        "Em `sources` avalia TODAS as fontes lidas, uma entrada por número: `covers_topic` só é "
        "verdadeiro se a página trata do próprio tema (a mesma notícia, produto, documento ou "
        "assunto do bloco «tema»). É falso se a página apenas menciona uma tecnologia ou nome que "
        "aparece no texto, se é a página geral de um produto, empresa, repositório ou projeto, ou "
        "se serve só de contexto. Factos tirados de uma fonte com `covers_topic` falso são "
        "descartados.\n"
        f"Este blog é sobre: {site['name']} — {(site.get('config') or {}).get('description', '')} "
        "Em `on_topic` diz se o tema pertence claramente a este blog; se for de outro assunto "
        "(por exemplo, política num blog de finanças, ou economia num blog de culinária), é falso.",
        system="És um pesquisador rigoroso de uma redação. Não escreves o artigo: só levantas os "
               "factos e quem os afirma.",
        schema=_RESEARCH_SCHEMA, purpose="research", site=site["site_key"],
        external=[External("tema", topic), *_external(sources)],
    )
    data = result.data or {}
    if data.get("on_topic") is not True:
        raise OffTopic("tema fora do assunto do blog")
    # Relevância (D-059): só contam as fontes que o modelo diz tratarem do tema. Sem avaliação de
    # uma fonte, ela não conta (a falta de resposta nunca vale como "sim").
    rated = data.get("sources")
    covering = {n for n in (_index(e.get("source"), len(sources)) for e in
                            (rated if isinstance(rated, list) else []) if isinstance(e, dict)
                            and e.get("covers_topic") is True) if n}
    raw: list[tuple[str, int, list[int]]] = []
    for fact in data.get("facts", []):
        if not isinstance(fact, dict) or not str(fact.get("claim", "")).strip():
            continue
        main = _index(fact.get("source"), len(sources))
        if main is None:
            continue
        extra = fact.get("also")
        others: list[Any] = extra if isinstance(extra, list) else []
        also = [n for n in (_index(v, len(sources)) for v in others) if n and n != main]
        if main not in covering:       # passa para uma fonte que trate do tema, se houver
            promoted = [n for n in also if n in covering]
            if not promoted:
                continue
            main, also = promoted[0], [n for n in also if n != promoted[0]]
        raw.append((str(fact["claim"]).strip(), main, [n for n in also if n in covering]))
    off_topic = [s.url for n, s in enumerate(sources, start=1) if n not in covering]
    if off_topic:
        log.info("Pesquisa: %d fonte(s) lida(s) não tratam do tema e saem: %s", len(off_topic),
                 ", ".join(off_topic)[:300])
    if not raw:
        raise StepFailed("pesquisa sem factos atribuídos a fontes que tratem do tema")
    # Ficam só as fontes com factos, renumeradas.
    used = sorted({n for _, main, also in raw for n in (main, *also)})
    renumber = {old: new for new, old in enumerate(used, start=1)}
    facts = [{"claim": claim, "source": renumber[main],
              "also": sorted({renumber[n] for n in also})} for claim, main, also in raw]
    return Research(sources=[sources[n - 1] for n in used], facts=facts,
                    divergences=[str(d) for d in data.get("divergences", []) if str(d).strip()],
                    off_topic=off_topic)


# --- Redação (E6.5) ---------------------------------------------------------------------------

_DRAFT_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "excerpt": {"type": "string"},
                   "html": {"type": "string"}, "category": {"type": "string"},
                   "declined": {"type": "string"}},
    "required": ["title", "excerpt", "html", "category", "declined"],
    "additionalProperties": False,
}


class Declined(StepFailed):
    """O redator recusou o tema (a política editorial não o permite, ou falta material)."""


# O título vira o <title> e o resumo vira a meta description na página de resultados da
# busca; acima destes tamanhos o Google corta com reticências (8/11).
_SEO_LIMITS = (
    "\nO `title` tem no máximo 65 caracteres: é o que aparece na busca. O `excerpt` é uma "
    "frase única de 120 a 155 caracteres que resume o artigo sem repetir o título, sem "
    "reticências e sem «saiba mais»."
)


def _brief_header(site: dict[str, Any], now: datetime) -> str:
    """Cabeçalho do pedido (blog, data, categorias): comum ao artigo e à receita."""
    config = site.get("config") or {}
    local = now.astimezone(ZoneInfo(settings.tz))
    weekend = " É fim de semana: aplica a regra do fim de semana." if local.weekday() >= 5 else ""
    return (
        f"Blog: {site['name']}. Data de hoje: {local.date().isoformat()}.{weekend}\n"
        "Categorias possíveis (escolhe exatamente uma): "
        + ", ".join(config.get("categories", []))
    )


def _brief(site: dict[str, Any], now: datetime) -> str:
    """A parte do pedido que é do motor: nada aqui vem de fora."""
    return (
        _brief_header(site, now)
        + "\nO tema, o ângulo pedido (se houver), os factos levantados pela pesquisa, as "
        "divergências e a lista de fontes estão no bloco «material da pesquisa».\n"
    )


def _material(topic: str, angle: str, found: Research) -> External:
    """Tema, factos e referências: tudo isto veio de fora (títulos de feeds e de páginas,
    frases tiradas das fontes), por isso segue delimitado e não nas instruções."""
    def cite(fact: dict[str, Any]) -> str:
        return ", ".join(f"fonte {n}" for n in (fact["source"], *fact.get("also", [])))

    facts = "\n".join(f"- {f['claim']} [{cite(f)}]" for f in found.facts)
    refs = "\n".join(f"[fonte {i}] {s.name} — {s.title} — {s.url}"
                     for i, s in enumerate(found.sources, start=1))
    divergences = "\n".join(f"- {d}" for d in found.divergences) or "- nenhuma"
    return External(
        "material da pesquisa",
        f"Tema: {topic}\n" + (f"Ângulo pedido: {angle}\n" if angle else "")
        + f"\nFactos levantados pela pesquisa:\n{facts}\n\n"
        f"Divergências entre fontes:\n{divergences}\n\nFontes:\n{refs}\n")


def write(topic: str, angle: str, found: Research, *, site: dict[str, Any], job_id: int,
          now: datetime, feedback: list[str] | None = None,
          avoid_family: str | None = None) -> Draft:
    config = site.get("config") or {}
    task = (
        _brief(site, now)
        + "\nEscreve o artigo em português do Brasil — vocabulário e grafia do Brasil, não de "
        "Portugal (pedido, confiabilidade, tela, arquivo; não encomenda, fiabilidade, ecrã, "
        f"ficheiro) —, com pelo menos {settings.ms_article_min_words} palavras, só com os "
        "factos do bloco. Quando citares "
        'um facto, liga à fonte com <a href="…">: usa apenas os endereços da lista de fontes. '
        "Não escrevas a secção de fontes: o sistema acrescenta-a. Não digas quando este "
        "artigo é publicado nem uses a data de hoje como facto: as únicas datas que entram são "
        "as que as fontes dão. Não tires conclusões que as fontes não tirem. Quantidades, "
        "doses, prazos e percentagens escrevem-se em algarismos.\n"
        "O artigo é sobre o tema do bloco, não sobre outro. Se a política editorial não "
        "permite escrever sobre este tema hoje, ou se o material não chega para um artigo "
        "dentro da política, não escrevas outro texto no lugar dele (nem uma nota, nem um "
        "guia sobre as regras do blog): deixa `title`, `excerpt`, `html` e `category` vazios e "
        "explica o motivo numa frase em `declined`. Se escreves o artigo, `declined` fica vazio."
        + _SEO_LIMITS
    )
    external = [_material(topic, angle, found), *_external(found.sources)]
    if feedback:
        # O parecer cita o artigo e as fontes: é texto que veio de fora, por interposto modelo.
        task += ("\n\nA versão anterior foi recusada. Corrige os pontos listados no bloco "
                 "«parecer sobre a versão anterior».")
        external.append(External("parecer sobre a versão anterior",
                                 "\n".join(f"- {issue}" for issue in feedback)))
    result = chain.run(
        "writer", task,
        system="És redator deste blog. Segue à risca a política editorial:\n\n"
               + str(config.get("policy", "")),
        schema=_DRAFT_SCHEMA, purpose="article", site=site["site_key"],
        external=external, avoid_family=avoid_family,
    )
    data = result.data or {}
    declined = gates.plain_line(str(data.get("declined") or ""))
    if declined:
        raise Declined(f"o redator recusou o tema: {declined[:400]}")
    body = gates.sanitize_html(str(data.get("html", "")))
    title = gates.plain_line(str(data.get("title", "")))
    if not title or not body:
        raise StepFailed("redação sem título ou sem texto", family=result.family)
    allowed = {s.url for s in found.sources}
    foreign = [u for u in gates.links(body) if u not in allowed]
    if foreign:
        # Uma ligação que não veio das fontes lidas pode ser inventada ou injetada.
        raise StepFailed(f"o texto liga a {len(foreign)} endereço(s) que não são fontes lidas",
                         family=result.family)
    return Draft(title=title[:200], excerpt=gates.plain_line(str(data.get("excerpt", "")))[:400],
                 html=body, category=str(data.get("category", "")).strip(),
                 writer=result.provider, writer_family=result.family)


# --- Revisão (E6.6) ---------------------------------------------------------------------------

_REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer"}, "approved": {"type": "boolean"},
        "issues": {"type": "array", "items": {
            "type": "object",
            "properties": {"text": {"type": "string"},
                           "severity": {"type": "string", "enum": ["blocking", "minor"]}},
            "required": ["text", "severity"], "additionalProperties": False}},
        "unsupported_claims": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["score", "approved", "issues", "unsupported_claims"],
    "additionalProperties": False,
}


def cited_sources(html: str, sources: list[Source]) -> list[Source]:
    """As fontes a que o texto realmente liga. São as que contam e as que se listam."""
    linked = set(gates.links(html))
    return [s for s in sources if s.url in linked]


def review(draft: Draft, found: Research, *, site: dict[str, Any], job_id: int,
           now: datetime, topic: str = "") -> Review:
    config = site.get("config") or {}
    today = now.astimezone(ZoneInfo(settings.tz)).date().isoformat()
    listed = "\n".join(f"- {s.title or s.name} — {s.url}"
                       for s in cited_sources(draft.html, found.sources))
    result = chain.run(
        "reviewer",
        f"Data de hoje, dada pelo sistema: {today} (não é uma afirmação do "
        "artigo a verificar).\n"
        "O bloco «tema e fontes listadas» diz o tema escolhido para este artigo e as fontes "
        "que vão aparecer no fim dele, em \"Fontes e créditos\". Se o artigo trata de outro "
        "assunto que não esse tema, ou se a lista de fontes mostra ao leitor algo que a "
        "política editorial não deixa publicar, é um reparo `blocking`.\n"
        "Revê o artigo abaixo contra as fontes. Para cada afirmação factual do artigo, confirma "
        "que uma das fontes a sustenta; lista em `unsupported_claims` só as afirmações factuais "
        "que nenhuma fonte sustenta ou que as fontes contradizem. Os avisos e ressalvas que a "
        "política editorial manda pôr (procurar um profissional, o texto é informativo e não "
        "substitui aconselhamento, e semelhantes) não são afirmações factuais: não precisam de "
        "fonte.\n"
        "Em `issues` lista os outros reparos, cada um com a gravidade:\n"
        "- `blocking`: o artigo não pode ser publicado assim — viola a política editorial de "
        "forma que engana ou prejudica o leitor (conselho individual, promessa, alegação não "
        "confirmada, conteúdo partidário, cópia de uma fonte), ou tem um erro que muda o "
        "sentido de um facto;\n"
        "- `minor`: melhoraria o texto mas não o impede de sair — precisão de uma palavra, uma "
        "data sem o ano, um pormenor que a política pede e as fontes não dão, estilo, estrutura.\n"
        "Na dúvida entre as duas, é `minor`: uma pessoa lê o artigo e as tuas notas antes de "
        "publicar. O atributo `rel` nos links é posto pelo sistema: não o apontes. "
        "`score` de 0 a 100. `approved` é verdadeiro se não houver afirmações sem "
        "suporte nem reparos `blocking`.",
        system="És revisor deste blog e não escreveste este artigo. Sê exigente. Política "
               "editorial:\n\n" + str(config.get("policy", "")),
        schema=_REVIEW_SCHEMA, purpose="review", site=site["site_key"],
        writer_family=draft.writer_family,
        external=[External("tema e fontes listadas",
                           f"Tema: {topic}\n\nFontes listadas no fim do artigo:\n{listed}"),
                  External("artigo a rever", f"{draft.title}\n\n{draft.html}"),
                  *_external(found.sources)],
    )
    return _verdict(result.data or {}, result.provider)


def _verdict(data: dict[str, Any], provider: str) -> Review:
    """O parecer do revisor, com a decisão tomada pelo código e não pela palavra do modelo."""
    unsupported = [str(c) for c in data.get("unsupported_claims", []) if str(c).strip()]
    blocking: list[str] = []
    notes: list[str] = []
    for issue in data.get("issues", []):
        if isinstance(issue, dict) and str(issue.get("text", "")).strip():
            # Só `minor` explícito é menor: qualquer outro valor conta como grave.
            target = notes if issue.get("severity") == "minor" else blocking
            target.append(str(issue["text"]).strip())
        elif isinstance(issue, str) and issue.strip():
            blocking.append(issue.strip())
    try:
        score = max(0, min(100, int(data.get("score", 0))))
    except (TypeError, ValueError):
        score = 0
    # A decisão não fica na palavra do modelo: afirmações sem suporte, reparos graves ou nota
    # baixa reprovam, diga ele o que disser; reparos menores não reprovam.
    approved = not unsupported and not blocking and score >= settings.review_min_score
    return Review(approved=approved, score=score, blocking=blocking, notes=notes,
                  unsupported_claims=unsupported, reviewer=provider)


# --- Receita original (D-080) -----------------------------------------------------------------


def _unlinked_html(data: dict[str, Any]) -> str:
    """Sanitiza o `html` do modelo e desfaz os links: a receita não cita; as fontes vão no fim."""
    soup = BeautifulSoup(gates.sanitize_html(str(data.get("html", ""))), "html.parser")
    for anchor in soup.find_all("a"):
        anchor.unwrap()
    return str(soup).strip()


def write_recipe(topic: str, angle: str, *, site: dict[str, Any], job_id: int, now: datetime,
                 feedback: list[str] | None = None, avoid_family: str | None = None) -> Draft:
    """Escreve a receita a partir do conhecimento culinário geral. Não recebe o texto de nenhuma
    fonte: elas só servem, depois, para conferir os números (`recipe.check_recipe`)."""
    config = site.get("config") or {}
    sections = "; ".join(f"<h2>{s}</h2>" for s in recipes.SECTIONS)
    task = (
        _brief_header(site, now)
        + "\nEscreve em português do Brasil — vocabulário e grafia do Brasil, não de Portugal "
        "(geladeira, fogão, açúcar de confeiteiro; não frigorífico, fogão a gás, açúcar glaçado) "
        "— UMA receita completa do prato do bloco «tema», original, com pelo menos "
        f"{settings.recipe_min_words} palavras. Escreve-a com o teu conhecimento culinário: não "
        "copies nem parafraseies a receita de nenhum sítio. Se o tema não é um prato que se possa "
        "ensinar passo a passo (uma notícia, um restaurante, um evento), ou se mistura vários "
        "pratos sem um que se destaque, deixa `title`, `excerpt`, `html` e `category` vazios e "
        "explica numa frase em `declined`.\n"
        "Estrutura obrigatória do `html`: um ou dois parágrafos de abertura sobre o prato e "
        "depois, "
        f"nesta ordem, as seções com estes títulos <h2> exatos: {sections}.\n"
        "- «Informações da receita»: lista <ul> com Rendimento, Tempo de preparo, Tempo de "
        "cozimento (se houver), Tempo de descanso ou resfriamento (se houver) e Tempo total "
        "(ex.: «Tempo total: 1 hora e 20 minutos»). O tempo total é a SOMA de preparo, "
        "cozimento, descanso, resfriamento e espera de todos os passos que acontecem em "
        "sequência (passos em paralelo não somam); molho de véspera fica de fora e se diz «mais "
        "N horas de molho».\n"
        "- «Material necessário»: lista <ul> dos utensílios e do equipamento. Confira com os "
        "passos: todo recipiente, utensílio ou equipamento que um passo usa (uma segunda tigela, "
        "o termômetro, o forno, a peneira) está na lista, e nada que a lista traz fica sem uso.\n"
        "- «Ingredientes»: lista <ul>, cada item com quantidade e unidade em algarismos (use «a "
        "gosto» só para sal, pimenta e semelhantes); pode usar <h3> para separar partes (massa, "
        "cobertura).\n"
        "- «Modo de preparo»: lista numerada <ol>, um passo por item, com tempos, temperaturas "
        "em °C e sinais para reconhecer o ponto. Todo ingrediente listado é usado em algum passo.\n"
        "- «Dicas e substituições»: lista <ul>.\n"
        "- «Onde encontrar os ingredientes»: parágrafo sobre onde comprar os itens menos comuns e "
        "o que usar no lugar.\n"
        "- «Conservação»: parágrafo com como guardar e por quanto tempo duram. Se o prato vai à "
        "geladeira, diga «leve à geladeira logo depois de pronto, em recipiente raso, e nunca "
        "deixe mais de 2 horas fora dela»; nunca mande esperar esfriar por até 2 horas.\n"
        "- «Alérgenos e cuidados»: alérgenos comuns presentes (glúten, leite, ovos, amendoim, "
        "frutos secos, marisco) e cuidados de segurança alimentar.\n"
        "Antes de entregar, confira: a abertura e as dicas não afirmam nada que os passos "
        "desmintam (por exemplo «uma só panela» quando há duas); toda alternativa de método vem "
        "com instruções completas ou não entra; o ponto de carne, aves e ovos sempre traz "
        "temperatura interna ou tempo, nunca só a cor; nada de explicações causais duvidosas.\n"
        "Sem links, sem imagens, sem a seção de fontes nem aviso de que a receita não foi testada: "
        "o sistema os acrescenta. Nunca escrevas «testamos», «testei» nem «fica perfeito». Sem "
        "alegações de saúde. Quantidades, tempos e temperaturas escrevem-se em algarismos."
        + _SEO_LIMITS
    )
    subject = f"Prato: {topic}" + (f"\nÂngulo pedido: {angle}" if angle else "")
    external = [External("tema", subject)]
    if feedback:
        task += ("\n\nA versão anterior foi recusada. Corrige os pontos listados no bloco "
                 "«parecer sobre a versão anterior».")
        external.append(External("parecer sobre a versão anterior",
                                 "\n".join(f"- {issue}" for issue in feedback)))
    result = chain.run(
        "writer", task,
        system="És redator deste blog. Segue à risca a política editorial:\n\n"
               + str(config.get("policy", "")),
        schema=_DRAFT_SCHEMA, purpose="article", site=site["site_key"], external=external,
        avoid_family=avoid_family,
    )
    data = result.data or {}
    declined = gates.plain_line(str(data.get("declined") or ""))
    if declined:
        raise Declined(f"o redator recusou o tema: {declined[:400]}")
    body = _unlinked_html(data)
    title = gates.plain_line(str(data.get("title", "")))
    if not title or not body:
        raise StepFailed("redação sem título ou sem texto", family=result.family)
    return Draft(title=title[:200], excerpt=gates.plain_line(str(data.get("excerpt", "")))[:400],
                 html=body, category=str(data.get("category", "")).strip(),
                 writer=result.provider, writer_family=result.family)


def review_recipe(draft: Draft, refs: list[recipes.Reference], *, site: dict[str, Any],
                  job_id: int, topic: str = "") -> Review:
    """Revisão de uma receita por outra família de modelos. Vê só as faixas das fontes."""
    config = site.get("config") or {}
    result = chain.run(
        "reviewer",
        "Revê a receita abaixo. O bloco «faixas das receitas consultadas» traz só números e "
        "palavras de ingredientes de receitas publicadas por outros sítios, para conferir a "
        "plausibilidade; a receita foi escrita sem copiar nenhuma. Em `issues` lista os reparos "
        "com a gravidade:\n"
        "- `blocking`: risco para quem cozinha (temperatura ou tempo inseguros para carne, aves, "
        "ovos ou conservas; ponto de carne, aves ou ovos dado só pela cor ou aparência quando a "
        "receita não traz, em lugar nenhum, a temperatura interna ou o tempo para aquele item "
        "— se a verificação existe em outro ponto, a menção à cor é só um reparo `minor`; "
        "alérgeno importante omitido), ingrediente ou quantidade "
        "claramente errados ou absurdos para o rendimento (por exemplo meia xícara de raspas de "
        "laranja num bolo), afirmação culinária falsa ou mito (causa e efeito errados), tempo "
        "total que não cobre os tempos dos passos, contradição entre a abertura ou as dicas e os "
        "passos, alternativa de método sem instruções completas, passo faltando ou fora de ordem "
        "que estraga o prato, ingrediente listado que nenhum passo usa, não ser o prato do tema, "
        "alegação de saúde, ou texto que afirma ter testado a receita;\n"
        "- `minor`: estilo, clareza, uma dica a mais, um pormenor de proporção que não estraga.\n"
        "Na dúvida entre as duas, é `minor`. `unsupported_claims` fica vazio, salvo se houver "
        "uma afirmação factual falsa fora da receita (história do prato, origem). `score` de 0 a "
        "100. `approved` é verdadeiro se não houver reparos `blocking`.",
        system="És revisor e cozinheiro experiente deste blog e não escreveste esta receita. Sê "
               "exigente com segurança alimentar e com a coerência da receita. Política editorial:"
               "\n\n" + str(config.get("policy", "")),
        schema=_REVIEW_SCHEMA, purpose="review", site=site["site_key"],
        writer_family=draft.writer_family,
        external=[External("tema", f"Prato: {topic}"),
                  External("faixas das receitas consultadas", recipes.references_summary(refs)),
                  External("receita a rever", f"{draft.title}\n\n{draft.html}")],
    )
    return _verdict(result.data or {}, result.provider)


def polish_recipe(draft: Draft, notes: list[str], *, site: dict[str, Any], job_id: int,
                  topic: str = "") -> Draft:
    """Passada de polimento (ajuste C): aplica à receita já aprovada só os reparos menores do
    revisor. Quem chama guarda a versão aprovada como reserva e confere gates e re-revisão."""
    config = site.get("config") or {}
    task = (
        "Aplica à receita do bloco «receita aprovada» APENAS os reparos do bloco «reparos do "
        "revisor» (reparos menores de uma revisão que já a aprovou). Mantém tudo o que os reparos "
        "não pedem para mudar: título, ordem e títulos <h2> das seções, ingredientes, quantidades, "
        "tempos e temperaturas; não acrescentes nem apagues seções. Devolve a receita INTEIRA já "
        "corrigida (`title`, `excerpt`, `html`, `category`), com `declined` vazio. Sem links, sem "
        "imagens, sem seção de fontes nem aviso de teste: o sistema os acrescenta. Quantidades, "
        "tempos e temperaturas em algarismos, em português do Brasil."
    )
    result = chain.run(
        "writer", task,
        system="És redator deste blog. Segue à risca a política editorial:\n\n"
               + str(config.get("policy", "")),
        schema=_DRAFT_SCHEMA, purpose="article", site=site["site_key"],
        external=[External("tema", f"Prato: {topic}"),
                  External("reparos do revisor", "\n".join(f"- {note}" for note in notes)),
                  External("receita aprovada",
                           f"{draft.title}\n\n{draft.excerpt}\n\n{draft.html}")],
    )
    data = result.data or {}
    body = _unlinked_html(data)
    if not body:
        raise StepFailed("polimento sem texto")
    title = gates.plain_line(str(data.get("title", ""))) or draft.title
    return Draft(title=title[:200],
                 excerpt=gates.plain_line(str(data.get("excerpt", "")))[:400] or draft.excerpt,
                 html=body, category=draft.category,
                 writer=result.provider, writer_family=result.family)


# --- Tradução (E6.8) --------------------------------------------------------------------------

_TRANSLATION_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "excerpt": {"type": "string"},
                   "html": {"type": "string"}},
    "required": ["title", "excerpt", "html"], "additionalProperties": False,
}
# P5 do ensaio E14 (3/11): en e es numa só chamada. O zh-CN fica de fora: é a tradução que
# mais tropeça no gate dos números (escala chinesa, D-060) e as reescritas são por idioma.
PAIR_LANGUAGES = ("en", "es")
_PAIR_SCHEMA = {
    "type": "object",
    "properties": {lang: _TRANSLATION_SCHEMA for lang in PAIR_LANGUAGES},
    "required": list(PAIR_LANGUAGES), "additionalProperties": False,
}


@dataclass(frozen=True)
class Translation:
    language: str
    title: str
    excerpt: str
    html: str
    slug: str


def _translation(data: Any, language: str, base_slug: str) -> Translation:
    """Monta (e valida) uma tradução a partir do que o modelo devolveu. Levanta `StepFailed`."""
    data = data if isinstance(data, dict) else {}
    title = gates.plain_line(str(data.get("title", "")))
    body = gates.sanitize_html(str(data.get("html", "")))
    if not title or not body:
        raise StepFailed(f"tradução para {language} sem título ou sem texto")
    # Slug: traduzido em en/es; em chinês usa-se o do original com sufixo (proposta D-OPEN-3).
    slug = (f"{base_slug}-zh" if language == "zh-CN"
            else slugify(title) or f"{base_slug}-{language}")
    return Translation(language=language, title=title[:200],
                       excerpt=gates.plain_line(str(data.get("excerpt", "")))[:400],
                       html=body, slug=slug)


def translate(draft: Draft, language: str, *, site: dict[str, Any], job_id: int,
              base_slug: str, problems: list[str] | None = None,
              avoid_family: str | None = None) -> Translation:
    config = site.get("config") or {}
    glossary = config.get("glossary") or {}
    terms = glossary.get(language) if isinstance(glossary, dict) else None
    task = (
        f"Traduz o artigo abaixo de português do Brasil para {LANGUAGES[language]}. Mantém todas "
        "as etiquetas HTML, todos os links (`href` exatamente iguais) e todos os números tal como "
        "estão, com os mesmos algarismos. Nas datas, o dia e o ano ficam em algarismos e o mês "
        "escreve-se por extenso na língua de destino, na ordem natural dela (inglês: «October 1, "
        "2026»; espanhol: «1 de octubre de 2026»). NUNCA uses o formato numérico dia/mês/ano "
        "(«1/10/2026»): é ambíguo (para um americano é 10 de janeiro). Em chinês: 2026年10月3日, "
        "nunca 十月. "
        "Em chinês, os valores com milhões e bilhões seguem a escala chinesa (700 bilhões → "
        "7000亿; 3 milhões → 300万), sempre com algarismos. "
        "Não acrescentes nem retires informação. Nomes de órgãos "
        "e de leis ficam no original, com a tradução entre parênteses na primeira vez."
    )
    if terms:
        task += "\nGlossário do blog (usa estes termos): " + "; ".join(
            f"{k} → {v}" for k, v in terms.items())
    if problems:
        task += "\n\nA tradução anterior foi recusada: " + "; ".join(problems) + ". Corrige."
    result = chain.run(
        "translator", task,
        system="És tradutor profissional. Devolves só a tradução, no formato pedido.",
        schema=_TRANSLATION_SCHEMA, purpose=f"translate:{language}", site=site["site_key"],
        external=[External("artigo a traduzir",
                           f"TÍTULO: {draft.title}\nRESUMO: {draft.excerpt}\n\n{draft.html}")],
        avoid_family=avoid_family,
    )
    try:
        return _translation(result.data, language, base_slug)
    except StepFailed as exc:
        exc.family = result.family      # a repetição tenta outra família (B da D-101)
        raise


def translate_pair(draft: Draft, *, site: dict[str, Any], job_id: int,
                   base_slug: str) -> dict[str, Translation]:
    """Traduz para inglês e espanhol numa só chamada (P5 do ensaio E14, 3/11).

    Os gates continuam a correr por idioma, em quem chama; um idioma que vier mal formado
    simplesmente não entra no resultado e é traduzido sozinho, pelo caminho antigo — por isso
    esta função não levanta `StepFailed` (indisponibilidade de IA levanta como sempre)."""
    config = site.get("config") or {}
    task = (
        f"Traduz o artigo abaixo de português do Brasil DUAS vezes, numa só resposta: para "
        f"{LANGUAGES['en']} (na chave `en`) e para {LANGUAGES['es']} (na chave `es`). Cada "
        "tradução é completa e independente, com `title`, `excerpt` e `html` próprios. Em cada "
        "uma, mantém todas as etiquetas HTML, todos os links (`href` exatamente iguais) e todos "
        "os números tal como estão, com os mesmos algarismos. Nas datas, o dia e o ano ficam em "
        "algarismos e o mês escreve-se por extenso na língua de destino, na ordem natural dela "
        "(inglês: «October 1, 2026»; espanhol: «1 de octubre de 2026»). NUNCA uses o formato "
        "numérico dia/mês/ano («1/10/2026»): é ambíguo (para um americano é 10 de janeiro). "
        "Não acrescentes nem retires informação. Nomes de órgãos e de leis ficam no original, "
        "com a tradução entre parênteses na primeira vez."
    )
    glossary = config.get("glossary") or {}
    for language in PAIR_LANGUAGES:
        terms = glossary.get(language) if isinstance(glossary, dict) else None
        if terms:
            task += f"\nGlossário do blog em `{language}` (usa estes termos): " + "; ".join(
                f"{k} → {v}" for k, v in terms.items())
    result = chain.run(
        "translator", task,
        system="És tradutor profissional. Devolves só as traduções, no formato pedido.",
        schema=_PAIR_SCHEMA, purpose="translate:en+es", site=site["site_key"],
        external=[External("artigo a traduzir",
                           f"TÍTULO: {draft.title}\nRESUMO: {draft.excerpt}\n\n{draft.html}")],
    )
    out: dict[str, Translation] = {}
    for language in PAIR_LANGUAGES:
        try:
            out[language] = _translation((result.data or {}).get(language), language, base_slug)
        except StepFailed as exc:
            log.warning("Job %d: %s na chamada conjunta; vai sozinha", job_id, exc)
    return out
