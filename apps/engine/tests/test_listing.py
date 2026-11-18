"""E14.5i — leitura de páginas de notícias sem RSS (a Anthropic)."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services import listing

PAGE = "https://www.anthropic.com/news"

# Recorte do formato real de anthropic.com/news (2026-10-20), com os nomes de classe encurtados.
ANTHROPIC = """
<nav><a href="/news">News</a><a href="/research">Research</a></nav>
<a href="/news/cyber-verification-program" class="FeaturedGrid__content">
  <h2 class="headline-4 FeaturedGrid__featuredTitle">Expanding the Cyber Verification Program</h2>
  <div class="FeaturedGrid__meta"><span class="caption bold">Announcements</span>
  <time class="FeaturedGrid__date caption bold">Oct 6, 2026</time></div>
  <p class="body-3 serif">We&#x27;re launching a new, expanded version of our program.</p></a>
<a href="/claude-haiku-5-5" class="FeaturedGrid__sideLink">
  <div class="FeaturedGrid__meta"><span class="caption bold">Announcements</span>
  <time class="caption bold">Oct 7, 2026</time></div>
  <h4 class="headline-6 FeaturedGrid__title">Introducing Claude Haiku 5.5</h4>
  <p>Our fastest model.</p></a>
<ul><li><a href="/news/public-benefit-fund" class="PublicationList__listItem">
  <time class="PublicationList__date body-3">Oct 18, 2026</time>
  <span class="caption PublicationList__subject">Announcements</span>
  <span class="PublicationList__title body-3">Investing in the public benefit &amp; more</span>
  </a></li>
<li><a href="/news/cyber-verification-program#top"><time>Oct 6, 2026</time>
  <span class="title">Expanding the Cyber Verification Program</span></a></li>
<li><a href="https://partner.example.com/story"><time>Oct 9, 2026</time>
  <h3>Hosted somewhere else</h3></a></li>
<li><a href="http://www.anthropic.com/news/insecure"><time>Oct 9, 2026</time>
  <h3>Plain http link</h3></a></li>
<li><a href="/news/no-date"><h3>A card without a date</h3></a></li>
<li><a href="/news/no-title"><time>Oct 9, 2026</time><p>No heading here</p></a></li></ul>
"""


def test_le_os_cartoes_da_pagina_da_anthropic():
    items = listing.parse(ANTHROPIC, PAGE)
    assert [(i.link, i.title, i.published.date().isoformat()) for i in items] == [
        ("https://www.anthropic.com/news/cyber-verification-program",
         "Expanding the Cyber Verification Program", "2026-10-06"),
        ("https://www.anthropic.com/claude-haiku-5-5", "Introducing Claude Haiku 5.5",
         "2026-10-07"),
        ("https://www.anthropic.com/news/public-benefit-fund",
         "Investing in the public benefit & more", "2026-10-18"),
    ]
    # D-013: só título, ligação e data; o resumo do cartão não sai daqui.
    assert all("launching" not in i.title and "fastest" not in i.title for i in items)


# Formato de anthropic.com/news a partir de 2026-10-20: o `<time>` fica fora da ligação, na
# mesma linha (`<li>`) ou no mesmo destaque (`<article>`), antes ou depois dela.
ANTHROPIC_LINHAS = """
<article><header><p><a href="/news/announcements">Announcements</a></p><time>Oct 20, 2026</time>
  </header><a href="/news/claude-opus-5-5"><h2>Introducing Claude Opus 5.5</h2></a></article>
<ul><li data-listing-row="a"><div><div><a href="/news/public-benefit-fund"><span>
  <h3>Investing in the public benefit</h3></span></a></div>
  <div><a tabindex="-1" href="/news/public-benefit-fund"><p>We're committing shares.</p></a></div>
  <div><a href="/news/announcements">Announcements</a></div><div><time>Oct 18, 2026</time></div>
  </div></li>
<li data-listing-row="b"><div><a href="/news/sem-data"><h3>Row without a date</h3></a></div></li>
<li data-listing-row="c"><div><a href="/news/announcements">Announcements</a></div>
  <div><time>Oct 9, 2026</time></div></li></ul>
"""


def test_le_as_linhas_com_a_data_fora_da_ligacao():
    items = listing.parse(ANTHROPIC_LINHAS, PAGE)
    assert [(i.link, i.title, i.published.date().isoformat()) for i in items] == [
        ("https://www.anthropic.com/news/claude-opus-5-5", "Introducing Claude Opus 5.5",
         "2026-10-20"),
        ("https://www.anthropic.com/news/public-benefit-fund", "Investing in the public benefit",
         "2026-10-18"),
    ]


def test_atributo_datetime_tem_precedencia_e_www_e_o_mesmo_sitio():
    html = ('<a href="https://anthropic.com/news/x"><time datetime="2026-10-19T15:30:00Z">'
            'Yesterday</time><h3>Something announced yesterday afternoon</h3></a>')
    (item,) = listing.parse(html, PAGE)
    assert item.published == datetime(2026, 10, 19, 15, 30, tzinfo=UTC)
    assert item.link == "https://anthropic.com/news/x"


def test_pagina_sem_cartoes_nao_da_itens():
    assert listing.parse("<html><body><p>Em manutenção</p></body></html>", PAGE) == []
    assert listing.parse("<a href='/news/a'><time>Oct 1, 2026</time>", PAGE) == []  # sem título


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Oct 6, 2026", datetime(2026, 10, 6, tzinfo=UTC)),
     ("October 6, 2026", datetime(2026, 10, 6, tzinfo=UTC)),
     ("Sept 30, 2026", datetime(2026, 9, 30, tzinfo=UTC)),
     ("6 Oct 2026", datetime(2026, 10, 6, tzinfo=UTC)),
     ("2026-10-06", datetime(2026, 10, 6, tzinfo=UTC)),
     ("2026-10-06T10:00:00-03:00", datetime(2026, 10, 6, 13, tzinfo=UTC)),
     ("ontem", None), ("", None)],
)
def test_datas(raw, expected):
    assert listing.parse_date(raw) == expected
