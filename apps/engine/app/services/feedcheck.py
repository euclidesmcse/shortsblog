"""E18.3a entrega 3: validação de um feed antes de entrar pelo portal.

As mesmas regras de `scripts/validate_feeds.py` (E5.2), mas pelo `fetcher` seguro do motor
(HTTPS, IPs públicos, `robots.txt` do nosso robô, tamanho limitado) e pelos mesmos leitores da
descoberta (`discovery._feed_items` / `_page_items`) — o que passa aqui é o que o motor lê:

- os termos do site não proíbem o uso comercial do RSS (`rss: false` no sem-leitura, D-071);
- responde 200 e é RSS/Atom (ou, `kind: html`, uma página de notícias com cartões) com itens;
- o item mais recente (ignorando datas no futuro) tem ligação HTTPS e menos de 30 dias;
- a maioria dos itens tem data e ligação.

Os termos (sem-leitura) ou o `robots.txt` contra os robôs de IA (D-070) não reprovam o feed:
ele fica «só pauta» (`pauta` diz porquê). Olha-se o site do ARTIGO mais recente, não só o do
feed: é a página do artigo que o motor entrega à IA, e ela pode estar noutro servidor. Mesma
regra de `scripts/validate_feeds.py`.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.core.config import settings
from app.services import discovery, fetcher, terms
from app.services.urls import under

TIERS = ("oficial", "agencia", "veiculo", "fornecedor", "academico")
LANGS = ("pt", "en", "es")
KINDS = discovery.SOURCE_KINDS
MAX_AGE_DAYS = 30
MIN_FEEDS = 15                      # piso por blog (E5): abaixo disso a tela avisa
FUTURE_TOLERANCE = timedelta(days=1)


@dataclass
class FeedCheck:
    ok: bool
    reason: str = ""
    items: int = 0
    newest: str = ""
    readable: bool = True           # False: o feed fica «só pauta» (ver `pauta`)
    titles: list[str] = field(default_factory=list)
    pauta: str = ""                 # "termos" (sem-leitura) ou "robots" (bloqueia a IA); D-070


PAUTA_WHY = {"termos": "o site está no sem-leitura",
             "robots": "o robots.txt do site bloqueia os robôs de IA"}


def pauta_only(feed_url: str, link: str) -> str:
    """Porque o feed fica «só pauta» (o motor não lê a página do artigo), ou ""."""
    if any(under(terms.host_of(u), terms.no_fetch_domains()) for u in (feed_url, link)):
        return "termos"
    if not fetcher.allowed_by_robots(link, settings.research_ai_agents):
        return "robots"
    return ""


def check(url: str, *, kind: str = "rss", tier: str = "", lang: str = "",
          now: datetime | None = None) -> FeedCheck:
    moment = now or datetime.now(UTC)
    if not url.lower().startswith("https://"):
        return FeedCheck(False, "o endereço não é HTTPS")
    if tier and tier not in TIERS:
        return FeedCheck(False, f"tier inválido: {tier}")
    if lang and lang not in LANGS:
        return FeedCheck(False, f"idioma inválido: {lang}")
    if kind not in KINDS:
        return FeedCheck(False, f"tipo inválido: {kind}")
    host = terms.host_of(url)
    if under(host, terms.no_feed_domains()):
        return FeedCheck(False, f"os termos de {host} proíbem o uso comercial do RSS "
                                "(sem-leitura, rss: false)")
    readable = not under(host, terms.no_fetch_domains())
    try:
        page = fetcher.fetch(url, accept=discovery.PAGE_ACCEPT if kind == "html"
                             else discovery.FEED_ACCEPT)
    except fetcher.FetchError as exc:
        labels = {"robots_disallowed": "o robots.txt do site proíbe o nosso robô",
                  "timeout": "sem resposta a tempo", "too_large": "resposta grande demais"}
        return FeedCheck(False, labels.get(exc.kind, f"falha ao ler: {exc.kind}"),
                         readable=readable)
    if page.status != 200:
        return FeedCheck(False, f"HTTP {page.status}", readable=readable)
    found = (discovery._page_items(page) if kind == "html"   # noqa: SLF001
             else discovery._feed_items(page.body))          # noqa: SLF001
    if not found:
        return FeedCheck(False, "feed sem itens (ou não é RSS/Atom)", readable=readable)
    dated = [(title, link, date) for title, link, date in found
             if date is not None and date <= moment + FUTURE_TOLERANCE]
    titles = [title for title, _, _ in found[:5] if title]
    if not dated:
        return FeedCheck(False, "itens sem data utilizável (ausente ou no futuro)",
                         items=len(found), readable=readable, titles=titles)
    _, link, newest = max(dated, key=lambda item: item[2])
    result = FeedCheck(False, items=len(found), newest=newest.date().isoformat(),
                       readable=readable, titles=titles)
    if not link.lower().startswith("https://"):
        result.reason = "item mais recente sem ligação HTTPS"
        return result
    age = moment - newest
    if age > timedelta(days=MAX_AGE_DAYS):
        result.reason = (f"item mais recente tem {math.ceil(age.total_seconds() / 86400)} dias "
                         f"(limite {MAX_AGE_DAYS})")
        return result
    usable = sum(date is not None and link.lower().startswith("https://")
                 for _, link, date in found)
    if usable * 2 < len(found):
        result.reason = f"só {usable} de {len(found)} itens têm data e ligação HTTPS"
        return result
    result.ok = True
    result.pauta = pauta_only(url, link)
    result.readable = not result.pauta
    return result
