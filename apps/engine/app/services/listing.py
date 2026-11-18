"""Leitura de páginas de notícias sem RSS (E14.5i).

Alguns sites oficiais (a Anthropic, em `anthropic.com/news`) não publicam feed,
mas a página de notícias lista cada artigo como uma ligação com título e data.
Daqui tira-se o mesmo que de um item RSS (D-013): título, ligação e data — nunca
o resumo nem as imagens.

Regra de leitura: cada `<a href>` que tenha dentro um `<time>` e um título (um
`<h1>`–`<h6>` ou um elemento com «title» na classe) é um item. Se o `<time>` está
fora da ligação, vale o da mesma linha (`<li>` ou `<article>`), com a primeira
ligação com título dessa linha (formato da Anthropic desde 2026-10-20). Só contam as
ligações `https` do mesmo site da página: um cartão que aponte para fora não é
uma notícia da fonte.

Só biblioteca padrão: `scripts/validate_feeds.py` carrega este arquivo para
validar as fontes do tipo `html` com as mesmas regras do motor.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

_HEADINGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6"})
_ROWS = frozenset({"li", "article"})
# Elementos sem fecho: não entram na pilha.
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
                   "source", "track", "wbr"})
_DATE_FORMATS = ("%b %d, %Y", "%B %d, %Y", "%d %b %Y", "%d %B %Y", "%Y-%m-%d")


@dataclass
class _Row:
    """Uma linha da lista: a primeira ligação com título sem data e o `<time>` de fora dela."""
    tag: str
    link: tuple[str, str] | None = None
    date: str = ""
    time_attr: str | None = None
    in_time: bool = False


@dataclass(frozen=True)
class Item:
    title: str
    link: str
    published: datetime | None


def parse_date(raw: str) -> datetime | None:
    """`Oct 6, 2026`, `6 October 2026` ou ISO 8601 → datetime em UTC (ou None)."""
    raw = re.sub(r"\s+", " ", raw).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        parsed = None
        for fmt in _DATE_FORMATS:
            try:
                parsed = datetime.strptime(raw.replace("Sept", "Sep"), fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _same_site(host_a: str, host_b: str) -> bool:
    return host_a.lower().removeprefix("www.") == host_b.lower().removeprefix("www.")


class _Cards(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cards: list[tuple[str, str, str]] = []     # (href, título, data)
        self._href: str | None = None
        self._stack: list[str] = []                      # elementos abertos dentro da ligação
        self._title: list[str] = []
        self._title_depth: int | None = None
        self._title_done = False
        self._date = ""
        self._time_attr: str | None = None
        self._in_time = False
        self._rows: list[_Row] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {k: v or "" for k, v in attrs}
        if self._href is None and tag in _ROWS:
            self._rows.append(_Row(tag))
            return
        if self._href is None and tag == "time" and self._rows:
            row = self._rows[-1]
            row.in_time = True
            row.time_attr = row.time_attr or values.get("datetime") or None
            return
        if tag == "a":
            # Uma ligação dentro de outra não é HTML válido: fecha a anterior.
            self._close()
            self._href = values.get("href", "")
            self._stack = []
            return
        if self._href is None or tag in _VOID:
            return
        self._stack.append(tag)
        if tag == "time":
            self._in_time = True
            self._time_attr = self._time_attr or values.get("datetime") or None
        elif (self._title_depth is None and not self._title_done
              and (tag in _HEADINGS or "title" in values.get("class", "").lower())):
            self._title_depth = len(self._stack)

    def handle_endtag(self, tag: str) -> None:
        if self._href is None:
            if tag == "time" and self._rows:
                self._rows[-1].in_time = False
            elif tag in _ROWS and any(r.tag == tag for r in self._rows):
                while (row := self._rows.pop()).tag != tag:
                    pass
                if row.link and (row.time_attr or row.date.strip()):
                    self.cards.append((*row.link, row.time_attr or row.date))
            return
        if tag == "a":
            self._close()
            return
        if tag not in self._stack:
            return
        while self._stack:
            depth = len(self._stack)
            closed = self._stack.pop()
            if self._title_depth == depth:
                self._title_depth = None
                self._title_done = bool("".join(self._title).strip())
            if closed == "time":
                self._in_time = False
            if closed == tag:
                break

    def handle_data(self, data: str) -> None:
        if self._href is None:
            if self._rows and self._rows[-1].in_time:
                self._rows[-1].date += data
            return
        if self._in_time:
            self._date += data
        elif self._title_depth is not None:
            self._title.append(data)

    def _close(self) -> None:
        if self._href is not None:
            title = re.sub(r"\s+", " ", "".join(self._title)).strip()
            date = self._time_attr or self._date
            if title and date.strip():
                self.cards.append((self._href, title, date))
            elif title and self._rows and self._rows[-1].link is None:
                self._rows[-1].link = (self._href, title)
        self._href = None
        self._stack = []
        self._title = []
        self._title_depth = None
        self._title_done = False
        self._date = ""
        self._time_attr = None
        self._in_time = False


def parse(html: str, page_url: str) -> list[Item]:
    """Itens da página, pela ordem em que aparecem, sem ligações repetidas."""
    reader = _Cards()
    reader.feed(html)
    reader.close()
    host = urlsplit(page_url).hostname or ""
    items: list[Item] = []
    seen: set[str] = set()
    for href, title, date in reader.cards:
        link = urljoin(page_url, href.strip()).split("#", 1)[0]
        parts = urlsplit(link)
        if parts.scheme != "https" or not _same_site(parts.hostname or "", host):
            continue
        if link in seen or link.rstrip("/") == page_url.rstrip("/"):
            continue
        seen.add(link)
        items.append(Item(title=title, link=link, published=parse_date(date)))
    return items
