"""E18.3a entrega 3: feeds, newsletters de pauta e sem-leitura pelo portal; validador de feeds."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import unquote_plus

import pytest
import yaml

from app.core.db import session_scope
from app.services import editorial, feedcheck, fetcher, terms
from tests.conftest import fetch, login_as
from tests.test_editorial import legacy_sync, plan

NOW = datetime.now(UTC)
FEED = "https://news.example-org.test/rss.xml"
RFC822 = "%a, %d %b %Y %H:%M:%S +0000"


def rss(days_old: int = 1, items: int = 6, link: str = "https://news.example-org.test/a") -> bytes:
    entries = "".join(
        f"<item><title>Notícia {i}</title><link>{link}{i}</link>"
        f"<pubDate>{(NOW - timedelta(days=days_old, hours=i)).strftime(RFC822)}"
        f"</pubDate></item>" for i in range(items))
    return (f'<?xml version="1.0"?><rss version="2.0"><channel><title>T</title>{entries}'
            f"</channel></rss>").encode()


@pytest.fixture
def web(monkeypatch):
    pages: dict[str, Any] = {FEED: rss()}

    def fake(url, **kwargs):
        reply = pages.get(url, fetcher.FetchError("network_error", "404"))
        if isinstance(reply, Exception):
            raise reply
        return fetcher.Fetched(url=url, status=200, content_type="application/rss+xml",
                               body=reply)

    monkeypatch.setattr(feedcheck.fetcher, "fetch", fake)
    # robots.txt contra os robôs de IA (D-070): hosts em `ai_blocked` bloqueiam; nunca vai à rede.
    pages["ai_blocked"] = set()

    def robots(url, agents=()):
        assert agents, "o feedcheck pergunta pelos robôs de IA"
        return terms.host_of(url) not in pages["ai_blocked"]

    monkeypatch.setattr(feedcheck.fetcher, "allowed_by_robots", robots)
    return pages


def location(response) -> str:
    return unquote_plus(response.headers["location"])


# --- validador -------------------------------------------------------------------------------
def test_feed_valido_passa(web):
    legacy_sync()
    result = feedcheck.check(FEED, tier="oficial", lang="en")
    assert result.ok and result.items == 6 and result.readable and result.titles
    assert result.pauta == ""


def test_robots_contra_a_ia_fica_so_pauta(web):
    """Revisão de 17/11: o portal só olhava o sem-leitura; o motor também olha o robots.txt."""
    legacy_sync()
    web["ai_blocked"].add("news.example-org.test")
    result = feedcheck.check(FEED, tier="oficial", lang="en")
    assert result.ok and result.pauta == "robots" and result.readable is False


def test_olha_o_site_do_artigo(web):
    """Feed num servidor, artigos noutro: vale o robots.txt de onde está o artigo."""
    legacy_sync()
    web[FEED] = rss(link="https://artigos.example-org.test/a")
    web["ai_blocked"].add("artigos.example-org.test")
    assert feedcheck.check(FEED).pauta == "robots"


def test_tela_avisa_que_entra_so_como_pauta_pelo_robots(panel_client, web):
    legacy_sync()
    web["ai_blocked"].add("news.example-org.test")
    assert "robots.txt do site bloqueia os robôs de IA" in location(add(panel_client))
    checked = panel_client.post("/dashboard/config/feeds/saude/check", data={"url": FEED}).text
    assert "robots.txt bloqueia a IA" in checked


def test_feed_invalido_nao_pergunta_robots_da_ia(web, monkeypatch):
    legacy_sync()
    web[FEED] = rss(days_old=45)
    monkeypatch.setattr(feedcheck.fetcher, "allowed_by_robots",
                        lambda *a, **k: pytest.fail("não devia perguntar"))
    assert not feedcheck.check(FEED).ok


@pytest.mark.parametrize("case,reason", [
    ("velho", "dias"), ("http", "HTTPS"), ("tier", "tier"), ("falha", "falha ao ler"),
    ("robots", "robots.txt"), ("vazio", "sem itens"),
])
def test_feed_invalido_e_recusado(web, case, reason):
    legacy_sync()
    url, kw = FEED, {"tier": "oficial", "lang": "en"}
    if case == "velho":
        web[FEED] = rss(days_old=45)
    elif case == "http":
        url = "http://news.example-org.test/rss.xml"
    elif case == "tier":
        kw["tier"] = "blog"
    elif case == "falha":
        url = "https://nao-existe.test/rss"
    elif case == "robots":
        web[FEED] = fetcher.FetchError("robots_disallowed", FEED)
    elif case == "vazio":
        web[FEED] = b"<html><body>nada</body></html>"
    result = feedcheck.check(url, **kw)
    assert not result.ok and reason in result.reason


def test_dominio_com_rss_false_e_recusado_e_sem_leitura_fica_so_pauta(web):
    legacy_sync()
    with session_scope() as db:
        entries = editorial.no_fetch(db)
        editorial.save_no_fetch(db, [*entries, {"domain": "example-org.test",
                                                "reason": "proíbe IA"}], "teste")
    result = feedcheck.check(FEED)
    assert result.readable is False and result.ok and result.pauta == "termos"
    with session_scope() as db:
        entries = [e for e in editorial.no_fetch(db) if e["domain"] != "example-org.test"]
        editorial.save_no_fetch(db, [*entries, {"domain": "example-org.test",
                                                "reason": "RSS só pessoal", "rss": False}],
                                "teste", "nova leitura dos termos em 12/11")
    result = feedcheck.check(FEED)
    assert not result.ok and "rss: false" in result.reason


# --- tela de feeds -----------------------------------------------------------------------------
def add(client, **over):
    data = {"url": FEED, "name": "Example Org", "tier": "oficial", "lang": "en", "kind": "rss",
            "notes": "", "terms_reviewed": "true", **over}
    return client.post("/dashboard/config/feeds/saude/add", data=data, follow_redirects=False)


def test_acrescentar_feed_valida_grava_e_audita(panel_client, web):
    legacy_sync()
    assert "Feeds" in panel_client.get("/dashboard/config/feeds/saude").text
    assert "termos" in location(add(panel_client, terms_reviewed=""))
    response = add(panel_client)
    assert "msg=" in response.headers["location"]
    row = fetch("SELECT enabled, tier, domain FROM sources WHERE feed_url = :u", u=FEED)[0]
    assert row["enabled"] and row["tier"] == "oficial" and row["domain"] == "news.example-org.test"
    log = fetch("SELECT target, details FROM audit_log WHERE action = 'config.edit'")[0]
    assert log["target"] == f"source:saude {FEED}" and log["details"]["kind"] == "add"
    assert log["details"]["check"]["items"] == 6
    assert "já está ativo" in location(add(panel_client))
    # o feed novo não existe no YAML: a reimportação o desligaria — e avisa que foi do portal
    change = next(c for c in plan() if c.entity == "source")
    assert change.kind == "disable" and change.edited_in_portal


def test_feed_invalido_nao_entra(panel_client, web):
    legacy_sync()
    web[FEED] = rss(days_old=60)
    assert "recusado" in location(add(panel_client))
    assert fetch("SELECT 1 FROM sources WHERE feed_url = :u", u=FEED) == []


def test_so_validar_mostra_o_resultado_sem_gravar(panel_client, web):
    legacy_sync()
    page = panel_client.post("/dashboard/config/feeds/saude/check",
                             data={"url": FEED, "kind": "rss"})
    assert page.status_code == 200 and "Passa na validação" in page.text
    assert "Notícia 0" in page.text
    assert fetch("SELECT 1 FROM sources WHERE feed_url = :u", u=FEED) == []


def test_editar_e_desligar_feed_com_aviso_do_piso(panel_client):
    legacy_sync()
    row = fetch("SELECT feed_url, name, tier, lang FROM sources WHERE site_key = 'saude' "
                "AND enabled ORDER BY id LIMIT 1")[0]
    response = panel_client.post("/dashboard/config/feeds/saude/edit", follow_redirects=False,
                                 data={"url": row["feed_url"], "name": row["name"] + " (x)",
                                       "tier": row["tier"], "lang": row["lang"], "notes": ""})
    text = location(response)
    assert "name" in text and "enabled" in text
    assert fetch("SELECT enabled FROM sources WHERE feed_url = :u",
                 u=row["feed_url"])[0]["enabled"] is False
    active = fetch("SELECT count(*) AS n FROM sources WHERE site_key = 'saude' AND enabled")[0]["n"]
    assert ("piso" in text) == (active < feedcheck.MIN_FEEDS)
    # desfazer pelo histórico religa
    audit_id = fetch("SELECT id FROM audit_log WHERE action = 'config.edit'")[0]["id"]
    panel_client.post(f"/dashboard/config/history/{audit_id}/undo")
    assert fetch("SELECT enabled FROM sources WHERE feed_url = :u",
                 u=row["feed_url"])[0]["enabled"] is True


def test_exportar_feeds_volta_a_ser_lido_pelo_deploy(panel_client):
    legacy_sync()
    body = panel_client.get("/dashboard/config/export/feeds/saude.yaml").text
    data = yaml.safe_load(body)
    active = fetch("SELECT count(*) AS n FROM sources WHERE site_key = 'saude' AND enabled")[0]["n"]
    assert data["site_key"] == "saude" and len(data["feeds"]) == active
    assert all(f["url"].startswith("https://") and f["tier"] and f["lang"] for f in data["feeds"])


# --- newsletters de pauta ---------------------------------------------------------------------
def test_newsletters_acrescentar_tirar_validar_e_desfazer(panel_client):
    legacy_sync()
    with session_scope() as db:
        before = editorial.newsletters(db)
    form = {"name": [e["name"] for e in before] + ["Nova"],
            "sender_domain": [e["sender_domain"] for e in before] + ["nova.example"],
            "site_key": [e["site_key"] for e in before] + ["ia"],
            "lang": [e["lang"] for e in before] + ["en"], "drop": ["0"]}
    response = panel_client.post("/dashboard/config/newsletters", data=form,
                                 follow_redirects=False)
    assert "msg=" in response.headers["location"]
    with session_scope() as db:
        after = editorial.newsletters(db)
    assert after[-1] == {"name": "Nova", "sender_domain": "nova.example", "site_key": "ia",
                         "lang": "en"}
    assert before[0] not in after and len(after) == len(before)
    audit_id = fetch("SELECT id FROM audit_log WHERE target = 'newsletters:newsletters'")[0]["id"]
    panel_client.post(f"/dashboard/config/history/{audit_id}/undo")
    with session_scope() as db:
        assert editorial.newsletters(db) == before


@pytest.mark.parametrize("domain,blog,erro", [("sem-ponto", "ia", "sender_domain"),
                                              ("x.example", "nenhum", "desconhecido")])
def test_newsletter_invalida_e_recusada(panel_client, domain, blog, erro):
    legacy_sync()
    response = panel_client.post("/dashboard/config/newsletters", follow_redirects=False,
                                 data={"name": ["X"], "sender_domain": [domain],
                                       "site_key": [blog], "lang": ["en"]})
    assert "err=" in response.headers["location"] and erro in location(response)


# --- sem-leitura --------------------------------------------------------------------------------
def test_sem_leitura_no_banco_vale_na_hora_no_mesmo_processo(panel_client):
    legacy_sync()
    url = "https://www.novo-site.test/pagina"
    assert terms.readable(url)
    response = panel_client.post("/dashboard/config/sem-leitura", follow_redirects=False,
                                 data={"action": "add", "domain": "novo-site.test",
                                       "reason": "proíbe IA", "terms_url": "https://x.test/t"})
    assert "msg=" in response.headers["location"]
    assert not terms.readable(url)
    no_reason = panel_client.post("/dashboard/config/sem-leitura", follow_redirects=False,
                                  data={"action": "remove", "domain": "novo-site.test"})
    assert "justificativa" in location(no_reason) and not terms.readable(url)
    panel_client.post("/dashboard/config/sem-leitura",
                      data={"action": "remove", "domain": "novo-site.test",
                            "justification": "licença obtida em 12/11"})
    assert terms.readable(url)
    log = fetch("SELECT details FROM audit_log WHERE target = 'no_fetch:lista' ORDER BY id")
    assert log[-1]["details"]["justification"] == "licença obtida em 12/11"
    assert "novo-site.test" in log[0]["details"]["note"]


def test_rss_false_avisa_dos_feeds_ativos_do_dominio(panel_client):
    legacy_sync()
    domain = fetch("SELECT domain FROM sources WHERE enabled AND site_key = 'saude' "
                   "ORDER BY id LIMIT 1")[0]["domain"]
    with session_scope() as db:
        listed = {e["domain"] for e in editorial.no_fetch(db)}
    action = "edit" if domain.removeprefix("www.") in listed else "add"
    response = panel_client.post("/dashboard/config/sem-leitura", follow_redirects=False,
                                 data={"action": action, "domain": domain, "reason": "teste",
                                       "no_rss": "true"})
    assert "devem ser desligados" in location(response)


def test_exportar_sem_leitura_e_valido(panel_client):
    legacy_sync()
    data = yaml.safe_load(panel_client.get("/dashboard/config/export/sem-leitura.yaml").text)
    assert len(terms.validate_entries(data["sites"])) == len(terms.load_file())


@pytest.mark.parametrize("path", ["/dashboard/config/feeds/saude", "/dashboard/config/newsletters",
                                  "/dashboard/config/sem-leitura"])
def test_operador_nao_entra(client, path):
    login_as(client, "op@example.com", "operador")
    assert client.get(path, follow_redirects=False).status_code == 403


def test_indice_liga_as_telas(panel_client):
    legacy_sync()
    page = panel_client.get("/dashboard/config").text
    for href in ("/dashboard/config/feeds/saude", "/dashboard/config/newsletters",
                 "/dashboard/config/sem-leitura"):
        assert href in page
