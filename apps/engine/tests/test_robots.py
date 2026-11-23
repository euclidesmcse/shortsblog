"""Leitor de robots.txt da RFC 9309 (23/11): curingas, regra mais longa, grupos."""
from __future__ import annotations

import urllib.robotparser

import pytest

from app.services.robots import DISALLOW_ALL, Robots

BOT = "ShortsBlogBot/1.0 (+https://shortsblog.com/bot)"


def rules(*lines: str) -> Robots:
    return Robots.parse(list(lines))


def test_curinga_no_meio_do_caminho_como_na_aemps():
    """O `urllib.robotparser` ignorava o `*` e deixava passar o feed da AEMPS."""
    robots = rules("User-agent: *", "Disallow: /feed*/")
    assert not robots.can_fetch(BOT, "https://www.aemps.gob.es/feed-noticias/")
    assert not robots.can_fetch(BOT, "https://www.aemps.gob.es/feed/rss/")
    assert robots.can_fetch(BOT, "https://www.aemps.gob.es/noticias/")
    old = urllib.robotparser.RobotFileParser()
    old.parse(["User-agent: *", "Disallow: /feed*/"])
    assert old.can_fetch(BOT, "https://www.aemps.gob.es/feed-noticias/")   # o defeito antigo


def test_cifrao_prende_ao_fim_e_curinga_com_query():
    robots = rules("User-agent: *", "Disallow: /*.pdf$", "Disallow: /*?print=")
    assert not robots.can_fetch(BOT, "https://x.test/docs/a.pdf")
    assert robots.can_fetch(BOT, "https://x.test/docs/a.pdf.html")
    assert not robots.can_fetch(BOT, "https://x.test/noticia?print=1")
    assert robots.can_fetch(BOT, "https://x.test/noticia?page=2")


def test_vence_a_regra_mais_longa_e_no_empate_o_allow():
    robots = rules("User-agent: *", "Allow: /news/", "Disallow: /news/private",
                   "Disallow: /", "Allow: /public")
    assert robots.can_fetch(BOT, "https://x.test/news/a")
    assert not robots.can_fetch(BOT, "https://x.test/news/private/b")
    assert not robots.can_fetch(BOT, "https://x.test/outra")
    assert robots.can_fetch(BOT, "https://x.test/public/c")
    tie = rules("User-agent: *", "Disallow: /a", "Allow: /a")
    assert tie.can_fetch(BOT, "https://x.test/a/b")
    # A ordem não importa (o urllib aplicava a 1.ª que casava).
    order = rules("User-agent: *", "Disallow: /", "Allow: /feed/")
    assert order.can_fetch(BOT, "https://x.test/feed/")


def test_grupo_do_robo_substitui_o_asterisco_e_grupos_juntam_se():
    robots = rules("User-agent: *", "Disallow: /",
                   "", "User-agent: ShortsBlogBot", "Allow: /",
                   "User-agent: ClaudeBot", "User-agent: GPTBot", "Disallow: /",
                   "User-agent: claudebot", "Allow: /open/")
    assert robots.can_fetch(BOT, "https://x.test/qualquer")
    assert not robots.can_fetch("Outro/2.0", "https://x.test/qualquer")
    assert not robots.can_fetch("ClaudeBot", "https://x.test/a")
    assert robots.can_fetch("ClaudeBot", "https://x.test/open/b")      # os 2 grupos juntos
    assert not robots.can_fetch("GPTBot", "https://x.test/open/b")


@pytest.mark.parametrize("lines, allowed", [
    ((), True),                                            # sem regras
    (("User-agent: *", "Disallow:"), True),               # Disallow vazio
    (("Disallow: /",), True),                              # regra sem grupo
    (("User-agent: *", "Disallow: / # tudo"), False),     # comentário na linha
    (("﻿User-agent: *", "Disallow: /"), False),       # BOM no início
])
def test_casos_de_borda(lines, allowed):
    assert rules(*lines).can_fetch(BOT, "https://x.test/a") is allowed


def test_robots_txt_sempre_permitido_e_proibir_tudo():
    assert DISALLOW_ALL.can_fetch(BOT, "https://x.test/robots.txt")
    assert not DISALLOW_ALL.can_fetch(BOT, "https://x.test/")


def test_codificacao_percentual_igual_dos_dois_lados():
    robots = rules("User-agent: *", "Disallow: /notícias/")
    assert not robots.can_fetch(BOT, "https://x.test/not%C3%ADcias/a")
    assert not robots.can_fetch(BOT, "https://x.test/notícias/a")


def test_regra_sem_barra_inicial_como_na_onu():
    """news.un.org: `Disallow: */news/` para todos e `Allow: /feed/subscribe/` só para o
    Feedfetcher-Google. Lido como o Google lê, o `*` inicial casa qualquer começo: os feeds
    `/feed/subscribe/…/news/…` ficam proibidos ao nosso robô (o urllib deixava passar)."""
    robots = rules("User-agent: *", "Disallow: */news/",
                   "User-agent: Feedfetcher-Google", "Allow: /feed/subscribe/")
    feed = "https://news.un.org/feed/subscribe/en/news/all/rss.xml"
    assert not robots.can_fetch(BOT, feed)
    assert robots.can_fetch("Feedfetcher-Google", feed)
    assert robots.can_fetch(BOT, "https://news.un.org/en/story/2026/11/1234")
