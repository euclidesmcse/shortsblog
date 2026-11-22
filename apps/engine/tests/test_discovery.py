"""E6.1 — descoberta: feeds → ideias, ideias manuais e deduplicação (por blog e global)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import pytest
from sqlalchemy import text

from app.core import cache
from app.core.config import settings
from app.core.db import session_scope
from app.jobs import discovery as discovery_task
from app.services import discovery, fetcher, sites
from tests.conftest import fetch

NOW = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)

def pick_triaged(db, site_key, now):
    """`discovery.pick` depois de a triagem dar o país às ideias de feed (D-110: sem país a
    ideia de feed espera a triagem). Aqui todas viram «XX», mundiais."""
    db.execute(text("UPDATE ideas SET country = 'XX' WHERE country IS NULL"))
    return discovery.pick(db, site_key, now)



def rss(*items: tuple[str, str, datetime | None]) -> bytes:
    body = ""
    for title, link, date in items:
        body += (f"<item><title>{title}</title><link>{link}</link>"
                 f"<description>TEXTO DO FEED QUE NUNCA SE GUARDA</description>"
                 + (f"<pubDate>{format_datetime(date)}</pubDate>" if date else "") + "</item>")
    return f'<rss version="2.0"><channel><title>f</title>{body}</channel></rss>'.encode()


@pytest.fixture
def feeds(monkeypatch) -> dict[str, bytes | Exception]:
    """Feeds simulados: endereço → corpo RSS (ou a exceção a levantar)."""
    bodies: dict[str, bytes | Exception] = {}

    def fake_fetch(url: str, **kwargs) -> fetcher.Fetched:
        reply = bodies.get(url, fetcher.FetchError("http_status", "404"))
        if isinstance(reply, Exception):
            raise reply
        return fetcher.Fetched(url=url, status=200, content_type="application/rss+xml",
                               body=reply)

    monkeypatch.setattr(fetcher, "fetch", fake_fetch)
    return bodies


@pytest.fixture
def two_sites():
    with session_scope() as db:
        sites.sync(db)
        db.execute(text("UPDATE sites SET enabled = true WHERE site_key IN ('saude', 'politica')"))
        for site, name, url in (("saude", "Fonte A", "https://a.test/feed"),
                                ("saude", "Fonte B", "https://b.test/feed"),
                                ("politica", "Fonte A", "https://a.test/feed")):
            db.execute(
                text("""INSERT INTO sources (site_key, name, feed_url, domain, tier, lang)
                        VALUES (:s, :n, :u, 'x.test', 'veiculo', 'pt')"""),
                {"s": site, "n": name, "u": url})


def discover(site: str) -> discovery.DiscoveryResult:
    with session_scope() as db:
        return discovery.discover_site(db, site, NOW)


def ideas(where: str = "true") -> list[dict]:
    return [dict(r) for r in fetch(
        f"""SELECT site_key, topic, canonical_url, status, origin, priority, duplicate_of,
                   angle FROM ideas WHERE {where} ORDER BY id""")]


YESTERDAY = NOW - timedelta(days=1)
VACINA = "Ministério amplia campanha de vacinação contra a gripe em todo o país"


# --- Normalização --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://WWW.Fonte.test/noticia/?utm_source=rss&utm_medium=x&id=7#topo",
         "https://fonte.test/noticia?id=7"),
        ("https://fonte.test/a//b/", "https://fonte.test/a/b"),
        ("https://fonte.test", "https://fonte.test/"),
        ("https://fonte.test/x?b=2&a=1&fbclid=abc", "https://fonte.test/x?a=1&b=2"),
        # Folha: o feed dá um redirecionador com o destino depois de "*".
        ("https://redir.folha.com.br/redir/online/poder/rss091/*https://www1.folha.uol.com.br/"
         "poder/2026/10/noticia.shtml", "https://www1.folha.uol.com.br/poder/2026/10/noticia.shtml"),
        ("https://redir.folha.com.br/redir/online/poder/rss091/*https:/www1.folha.uol.com.br/x",
         "https://www1.folha.uol.com.br/x"),
        ("https://redir.x.test/r/*http://inseguro.test/x", "https://redir.x.test/r/*http:/inseguro.test/x"),
        ("http://fonte.test/x", None),            # o motor só vai buscar https
        ("/relativo", None), ("javascript:alert(1)", None), ("", None),
    ],
)
def test_url_canonica(raw, expected):
    assert discovery.canonical_url(raw) == expected


def test_titulos_parecidos():
    key = discovery.title_key
    assert discovery.similar(key(VACINA),
                             key("Campanha de vacinação contra a gripe é ampliada pelo ministério"
                                 " em todo o país"))
    assert not discovery.similar(key(VACINA),
                                 key("Banco Central mantém a taxa Selic em 15% ao ano"))
    # Acentos, maiúsculas e pontuação não contam.
    assert key("VACINAÇÃO: Gripe!") == key("vacinacao gripe")
    # Títulos curtos demais não se comparam (evita juntar notícias diferentes).
    assert not discovery.similar(key("Gripe avança"), key("Gripe avança"))


# --- Descoberta --------------------------------------------------------------------------------


def test_guarda_so_titulo_ligacao_e_data(feeds, two_sites):
    feeds["https://a.test/feed"] = rss((VACINA, "https://a.test/n1?utm_source=rss", YESTERDAY))
    result = discover("saude")
    assert (result.sources, result.failed, result.new) == (2, 1, 1)     # o feed B não respondeu
    (idea,) = ideas("site_key = 'saude'")
    assert (idea["topic"], idea["canonical_url"], idea["origin"], idea["status"]) == (
        VACINA, "https://a.test/n1", "rss", "new")
    # D-013: do feed não se guarda texto nenhum além do título.
    row = fetch("SELECT * FROM ideas")[0]
    assert all("TEXTO DO FEED" not in str(value) for value in row.values())
    source = fetch("SELECT last_status, fail_count, last_items FROM sources "
                   "WHERE site_key = 'saude' ORDER BY id")
    assert [dict(s) for s in source] == [
        {"last_status": "ok", "fail_count": 0, "last_items": 1},
        {"last_status": "http_status", "fail_count": 1, "last_items": None}]
    assert result.errors == {"Fonte B": "http_status"}


def test_reler_o_feed_nao_duplica(feeds, two_sites):
    feeds["https://a.test/feed"] = rss((VACINA, "https://a.test/n1", YESTERDAY))
    assert discover("saude").new == 1
    again = discover("saude")
    assert (again.new, again.known) == (0, 1)
    assert len(ideas("site_key = 'saude'")) == 1


def test_itens_sem_data_antigos_no_futuro_ou_sem_https_ficam_de_fora(feeds, two_sites):
    feeds["https://a.test/feed"] = rss(
        ("Notícia sem data nenhuma no item", "https://a.test/1", None),
        ("Notícia antiga de há dez dias atrás", "https://a.test/2", NOW - timedelta(days=10)),
        ("Notícia com data no futuro distante", "https://a.test/3", NOW + timedelta(days=30)),
        ("Notícia com ligação sem https", "http://a.test/4", YESTERDAY),
        ("Notícia boa e recente que deve entrar", "https://a.test/5", YESTERDAY),
    )
    result = discover("saude")
    assert (result.new, result.skipped) == (1, 4)
    assert [i["canonical_url"] for i in ideas()] == ["https://a.test/5"]


def test_feed_em_falha_nao_impede_os_outros(feeds, two_sites):
    feeds["https://a.test/feed"] = fetcher.FetchError("blocked_address", "a.test → 10.0.0.1")
    feeds["https://b.test/feed"] = rss((VACINA, "https://b.test/n1", YESTERDAY))
    result = discover("saude")
    assert (result.failed, result.new, result.errors) == (1, 1, {"Fonte A": "blocked_address"})


def page(*cards: tuple[str, str, str]) -> bytes:
    """Página de notícias sem RSS (como a da Anthropic): cartões com título e data."""
    body = "".join(
        f'<a href="{href}"><span class="caption">Announcements</span><time>{date}</time>'
        f'<h3 class="card-title">{title}</h3><p>RESUMO QUE NUNCA SE GUARDA</p></a>'
        for title, href, date in cards)
    return f'<html><body><nav><a href="/news">News</a></nav>{body}</body></html>'.encode()


def test_pagina_de_noticias_sem_rss_vira_ideias(feeds, two_sites):
    with session_scope() as db:
        db.execute(text("""INSERT INTO sources (site_key, name, feed_url, domain, source_type,
                                                tier, lang)
                           VALUES ('saude', 'Página C', 'https://c.test/news', 'c.test', 'html',
                                   'fornecedor', 'en')"""))
    feeds["https://c.test/news"] = page(
        ("Company opens a new office and signs agreement", "/news/office", "Oct 4, 2026"),
        ("Introducing a new model for everyone today", "https://www.c.test/model", "Oct 5, 2026"),
        ("Partner story hosted on another website", "https://other.test/story", "Oct 4, 2026"),
        ("An old announcement from last month here", "/news/old", "Sep 1, 2026"),
    )
    result = discover("saude")
    assert (result.new, result.skipped) == (2, 1)        # a de fora do sítio nem chega a item
    got = ideas("site_key = 'saude'")
    assert sorted((i["topic"], i["canonical_url"]) for i in got) == [
        ("Company opens a new office and signs agreement", "https://c.test/news/office"),
        ("Introducing a new model for everyone today", "https://c.test/model")]
    assert all("RESUMO" not in str(v) for r in fetch("SELECT * FROM ideas") for v in r.values())


# --- Deduplicação --------------------------------------------------------------------------------


def test_noticia_repetida_em_dois_feeds_do_mesmo_blog_vira_corroboracao(feeds, two_sites):
    feeds["https://a.test/feed"] = rss((VACINA, "https://a.test/n1", YESTERDAY))
    feeds["https://b.test/feed"] = rss(
        ("Campanha de vacinação contra a gripe é ampliada pelo ministério em todo o país",
         "https://b.test/outra", YESTERDAY))
    result = discover("saude")
    assert (result.new, result.duplicates) == (1, 1)
    first, second = ideas("site_key = 'saude'")
    assert first["duplicate_of"] is None and second["duplicate_of"] is not None
    with session_scope() as db:
        picked = pick_triaged(db, "saude", NOW)
        assert picked is not None and picked.topic == VACINA
        # Um artigo, duas fontes para corroborar.
        assert picked.source_urls == ("https://a.test/n1", "https://b.test/outra")
        assert pick_triaged(db, "saude", NOW) is None
    assert {i["status"] for i in ideas("site_key = 'saude'")} == {"taken"}


def test_noticia_repetida_em_dois_blogs_so_um_a_escreve(feeds, two_sites):
    # O mesmo feed generalista serve dois blogs (caso do Senado e da BBC Brasil).
    feeds["https://a.test/feed"] = rss((VACINA, "https://a.test/n1", YESTERDAY))
    assert discover("saude").new == 1
    assert discover("politica").new == 1           # entra nos dois: a escolha é que decide
    with session_scope() as db:
        assert pick_triaged(db, "saude", NOW).topic == VACINA
        assert pick_triaged(db, "politica", NOW) is None
    assert {(i["site_key"], i["status"]) for i in ideas()} == {
        ("saude", "taken"), ("politica", "discarded")}


def test_dedup_global_tambem_por_titulo_parecido_de_outra_fonte(feeds, two_sites):
    with session_scope() as db:
        discovery.store_idea(db, site_key="saude", origin="rss", topic=VACINA,
                             url="https://a.test/n1", now=NOW)
        discovery.store_idea(
            db, site_key="politica", origin="rss", url="https://outro.test/z", now=NOW,
            topic="Ministério amplia a campanha de vacinação contra a gripe no país todo")
        assert pick_triaged(db, "saude", NOW) is not None
        assert pick_triaged(db, "politica", NOW) is None


def test_ideias_fora_da_janela_ja_nao_bloqueiam(feeds, two_sites):
    with session_scope() as db:
        discovery.store_idea(db, site_key="saude", origin="rss", topic=VACINA,
                             url="https://a.test/n1", now=NOW - timedelta(days=30))
        db.execute(text("UPDATE ideas SET status = 'used', created_at = :d"),
                   {"d": NOW - timedelta(days=30)})
        discovery.store_idea(db, site_key="politica", origin="rss", topic=VACINA,
                             url="https://b.test/novo", now=NOW)
        assert pick_triaged(db, "politica", NOW) is not None


def test_ideia_expirada_nao_e_escolhida(feeds, two_sites):
    with session_scope() as db:
        discovery.store_idea(db, site_key="saude", origin="rss", topic=VACINA,
                             url="https://a.test/n1", now=NOW - timedelta(days=6))
        assert pick_triaged(db, "saude", NOW) is None
    assert ideas()[0]["status"] == "expired"


# --- Ideias manuais ------------------------------------------------------------------------------


def test_ideia_manual_e_prioritaria_mas_passa_os_mesmos_gates(feeds, two_sites):
    feeds["https://a.test/feed"] = rss(
        ("Anvisa aprova novo medicamento para tratamento de diabetes", "https://a.test/n9",
         NOW - timedelta(hours=1)))
    discover("saude")
    with session_scope() as db:
        outcome, idea_id = discovery.add_manual(
            db, "saude", "  Como ler um rótulo de   protetor solar  ", angle="guia prático",
            url="https://fonte.test/protetor?utm_source=x", created_by="euclides@example.com")
        assert outcome == "new" and idea_id
        # A manual sai primeiro, apesar de a do feed ser mais recente.
        picked = pick_triaged(db, "saude", NOW)
        assert picked.origin == "manual" and picked.topic == "Como ler um rótulo de protetor solar"
        assert picked.angle == "guia prático"
        assert picked.source_urls == ("https://fonte.test/protetor",)
        # Mesmos gates: a mesma ligação outra vez é recusada como conhecida.
        assert discovery.add_manual(db, "saude", "Outro título para a mesma ligação",
                                    url="https://fonte.test/protetor",
                                    created_by="x")[0] == "known"
    row = fetch("SELECT priority, created_by FROM ideas WHERE origin = 'manual'")[0]
    assert dict(row) == {"priority": 100, "created_by": "euclides@example.com"}


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [({"topic": "curto"}, "curto demais"),
     ({"topic": "Um tema com tamanho suficiente", "url": "http://fonte.test/x"}, "https"),
     ({"topic": "Um tema com tamanho suficiente", "site": "inexistente"}, "site desconhecido")],
)
def test_ideia_manual_invalida(two_sites, kwargs, fragment):
    site = kwargs.pop("site", "saude")
    with pytest.raises(ValueError, match=fragment), session_scope() as db:
        discovery.add_manual(db, site, created_by="x", **kwargs)


# --- Fontes: ficheiros reais → tabela ------------------------------------------------------------


def test_os_feeds_reais_sincronizam_para_a_tabela_sources():
    with session_scope() as db:
        sites.sync(db)
        total = discovery.sync_sources(db)
        again = discovery.sync_sources(db)          # idempotente
    assert total == again and total >= 6 * 15
    counts = {r["site_key"]: r["n"] for r in fetch(
        "SELECT site_key, count(*) AS n FROM sources WHERE enabled GROUP BY 1")}
    assert set(counts) == {"cloud", "ia", "financas", "saude", "politica", "culinaria"}
    assert all(n >= 15 for n in counts.values())
    assert fetch("SELECT count(*) AS n FROM sources WHERE feed_url NOT LIKE 'https://%'")[0][
        "n"] == 0
    # As notas de uso da E5 chegam à base (o pipeline vai precisar delas). O Planalto, âncora
    # antiga, saiu do final na virada da Política (8/11: item mais recente com 42 dias).
    assert fetch("SELECT usage_notes FROM sources WHERE name = 'GOV.UK — News'")[0][
        "usage_notes"].startswith("feed generalista")


def test_feed_retirado_do_ficheiro_e_desligado(tmp_path):
    with session_scope() as db:
        sites.sync(db)
    body = ("site_key: cloud\nfeeds:\n"
            "- {name: A, url: 'https://a.test/feed', tier: oficial, lang: pt}\n"
            "- {name: B, url: 'https://b.test/feed', tier: veiculo, lang: en, kind: html}\n")
    (tmp_path / "cloud.yaml").write_text(body)
    with session_scope() as db:
        assert discovery.sync_sources(db, tmp_path) == 2
    assert {r["name"]: r["source_type"] for r in fetch(
        "SELECT name, source_type FROM sources")} == {"A": "rss", "B": "html"}
    (tmp_path / "cloud.yaml").write_text(body.split("- {name: B")[0])
    with session_scope() as db:
        assert discovery.sync_sources(db, tmp_path) == 1
    assert {r["name"]: r["enabled"] for r in fetch("SELECT name, enabled FROM sources")} == {
        "A": True, "B": False}


@pytest.mark.parametrize(
    ("body", "fragment"),
    [("site_key: outro\nfeeds: []\n", "diferente do nome"),
     ("site_key: cloud\nfeeds:\n- {name: A, url: 'https://a.test/f', tier: oficial, lang: pt, "
      "kind: json}\n", "tipo de fonte desconhecido"),
     ("site_key: cloud\nfeeds:\n- {name: A, url: 'http://a.test/f', tier: oficial, lang: pt}\n",
      "sem https")],
)
def test_ficheiro_de_feeds_invalido(tmp_path, body, fragment):
    with session_scope() as db:
        sites.sync(db)
    (tmp_path / "cloud.yaml").write_text(body)
    with pytest.raises(discovery.SourceConfigError, match=fragment), session_scope() as db:
        discovery.sync_sources(db, tmp_path)


# --- Tarefa e rotas ------------------------------------------------------------------------------


def test_tarefa_le_so_os_sites_ligados_e_nao_corre_a_dobrar(feeds, two_sites):
    feeds["https://a.test/feed"] = rss((VACINA, "https://a.test/n1", datetime.now(UTC)))
    results = discovery_task.run()
    assert set(results) == {"saude", "politica"}        # os outros 4 estão desligados
    assert results["saude"]["new"] == 1
    # Enquanto uma descoberta corre, outra não arranca.
    lock = cache.ShortLock("discovery", ttl=30)
    assert lock.acquire()
    assert discovery_task.run() == {"skipped": "already_running"}
    lock.release()


def test_rota_discover_enfileira_no_worker_longo(client, auth):
    assert client.post("/api/sched/discover").status_code == 401
    body = client.post("/api/sched/discover", headers=auth).json()
    assert cache.queue_content.jobs[0].id == body["enqueued"]
    assert cache.queue_content.jobs[0].func_name == "app.jobs.discovery.run"


def test_rota_de_ideias_manuais(client, auth, two_sites):
    payload = {"site_key": "saude", "topic": "Como ler um rótulo de protetor solar",
               "created_by": "euclides@example.com"}
    assert client.post("/api/sched/ideas", json=payload).status_code == 401
    res = client.post("/api/sched/ideas", json=payload, headers=auth)
    assert res.status_code == 200 and res.json()["result"] == "new"
    bad = client.post("/api/sched/ideas", json={**payload, "topic": "x"}, headers=auth)
    assert bad.status_code == 422


def test_escolha_prefere_a_ideia_com_mais_fontes(two_sites):
    with session_scope() as db:
        discovery.store_idea(db, site_key="saude", origin="rss", url="https://a.test/sozinha",
                             topic="Anvisa aprova novo medicamento para tratamento de diabetes",
                             published_at=NOW, now=NOW)
        for url in ("https://a.test/n1", "https://b.test/n1"):
            discovery.store_idea(db, site_key="saude", origin="rss", topic=VACINA, url=url,
                                 published_at=NOW - timedelta(hours=5), now=NOW)
        # A mais recente tem uma só fonte; a mais antiga tem duas e sai primeiro.
        assert pick_triaged(db, "saude", NOW).topic == VACINA


def test_escolha_conta_sitios_diferentes_e_deixa_os_generalistas_para_o_fim(two_sites):
    with session_scope() as db:
        db.execute(text("UPDATE sources SET usage_notes = 'feed generalista: filtrar por tema' "
                        "WHERE site_key = 'saude' AND name = 'Fonte B'"))
        ids = {r["name"]: r["id"] for r in fetch("SELECT id, name FROM sources "
                                                   "WHERE site_key = 'saude'")}
    with session_scope() as db:
        # Seis páginas do MESMO sítio com títulos quase iguais (apuração por estado): 1 só fonte.
        for n in range(6):
            discovery.store_idea(
                db, site_key="saude", origin="rss", published_at=NOW, now=NOW,
                url=f"https://a.test/apuracao-{n}", source_id=ids["Fonte B"],
                topic=f"Apuração para governador no estado {n} veja quem está na frente agora")
        # Uma história contada por dois sítios diferentes.
        for url in ("https://a.test/vacina", "https://b.test/vacina"):
            discovery.store_idea(db, site_key="saude", origin="rss", topic=VACINA, url=url,
                                 published_at=NOW - timedelta(hours=8), now=NOW,
                                 source_id=ids["Fonte A"])
        # Uma notícia sozinha de um feed do tema, e outra mais recente de um feed generalista.
        discovery.store_idea(db, site_key="saude", origin="rss", source_id=ids["Fonte A"],
                             url="https://a.test/tema", published_at=NOW - timedelta(hours=9),
                             topic="Anvisa aprova novo medicamento para tratamento de diabetes",
                             now=NOW)
        discovery.store_idea(db, site_key="saude", origin="rss", source_id=ids["Fonte B"],
                             url="https://b.test/geral", published_at=NOW,
                             topic="Senado vota hoje projeto sobre concessões rodoviárias federais",
                             now=NOW)
        order = []
        while (picked := pick_triaged(db, "saude", NOW)) is not None:
            order.append(picked.topic[:20])
    assert order[0] == VACINA[:20]                        # dois sítios diferentes
    assert order[1].startswith("Anvisa aprova")           # feed do tema antes do generalista
    assert order[2].startswith(("Senado vota", "Apuração para"))



# --- E14.5g: a mesma notícia em línguas diferentes (entidades do título) ---------------------


def test_entidades_do_titulo():
    assert discovery.title_entities("Palo Alto Networks warns of PAN-OS RCE flaw") == {
        "palo alto networks", "pan-os", "rce"}
    assert discovery.title_entities("Muse Wonder vs. Claude Fable 5.1: Meta’s new model") == {
        "muse wonder", "claude fable", "5.1", "meta"}
    # Siglas que mudam com a língua ficam numa forma só; IA/AI não contam.
    assert discovery.title_entities("WHO and UN warn of AI risks in the US") == {
        "oms", "onu", "eua"}
    assert discovery.title_entities("OMS e ONU alertam para riscos da IA nos EUA") == {
        "oms", "onu", "eua"}
    assert discovery.title_entities("Ministério amplia vacinação") == {"ministerio"}


@pytest.mark.parametrize(("a", "b", "same"), [
    # Casos reais do `cloud` (out/2026).
    ("DHS bans DeepSeek products across US federal civilian agencies",
     "EUA mandam banir o uso da IA DeepSeek dos sistemas de TI governamentais", True),
    ("TorchTPU is now open source: Run native PyTorch on TPUs",
     "Google libera TorchTPU como código aberto para executar PyTorch em aceleradores", True),
    # Só um nome em comum (composto) não chega.
    ("Palo Alto Networks warns of PAN-OS RCE flaw exploited in attacks",
     "Sem segurança, não haverá avanço na adoção da IA, afirma CFO da Palo Alto Networks", False),
    # Um nome comum na janela e um número não fazem a mesma notícia.
    ("Microsoft releases Windows 11 update", "Microsoft anuncia 11 novos centros no Brasil", False),
])
def test_mesma_noticia_entre_linguas(a, b, same):
    common = frozenset({"microsoft", "google"})
    assert discovery.same_story(discovery.title_entities(a), discovery.title_entities(b),
                                common) is same


# --- Desempate de legibilidade no pick (D-070, 5/11) ---------------------------------------------


@pytest.fixture
def lista_fechado(tmp_path, monkeypatch):
    """`fechado.test` entra na lista sem-leitura só durante o teste."""
    from app.core.config import settings
    from app.services import terms
    path = tmp_path / "sem-leitura.yaml"
    path.write_text("sites:\n- {domain: fechado.test, reason: proíbe uso por IA}\n",
                    encoding="utf-8")
    monkeypatch.setattr(settings, "no_fetch_config", str(path))
    terms._load.cache_clear()
    yield
    terms._load.cache_clear()


def test_pick_deixa_pauta_sem_leitura_para_depois(two_sites, lista_fechado):
    # A pauta de domínio sem-leitura parte de 0 fontes e precisa de 2 só da busca;
    # à mesma prioridade, a de fonte legível vai primeiro — mesmo sendo mais velha.
    with session_scope() as db:
        discovery.store_idea(db, site_key="saude", origin="rss",
                             topic="Hospital inaugura centro de pesquisa em cardiologia",
                             url="https://fechado.test/novo", published_at=NOW, now=NOW)
        discovery.store_idea(db, site_key="saude", origin="rss",
                             topic="Estudo associa sono irregular a risco metabólico",
                             url="https://aberto.test/estudo",
                             published_at=NOW - timedelta(hours=2), now=NOW)
        first = pick_triaged(db, "saude", NOW)
        second = pick_triaged(db, "saude", NOW)
    assert first is not None and first.source_urls == ("https://aberto.test/estudo",)
    assert second is not None and second.source_urls == ("https://fechado.test/novo",)


def test_pick_sem_leitura_corroborada_nao_e_penalizada(two_sites, lista_fechado):
    # Com corroboração de OUTRO domínio a pauta não depende só da busca: fica onde a
    # ordem normal a põe (corroboração ganha da recência e da legibilidade).
    with session_scope() as db:
        discovery.store_idea(db, site_key="saude", origin="rss", topic=VACINA,
                             url="https://fechado.test/vacina", published_at=NOW, now=NOW)
        discovery.store_idea(
            db, site_key="saude", origin="rss",
            topic="Campanha de vacinação contra a gripe é ampliada pelo ministério no país",
            url="https://outro.test/vacina", published_at=NOW, now=NOW)
        discovery.store_idea(db, site_key="saude", origin="rss",
                             topic="Nova diretriz muda o rastreio do câncer de cólon",
                             url="https://aberto.test/diretriz", published_at=NOW, now=NOW)
        first = pick_triaged(db, "saude", NOW)
    assert first is not None and set(first.source_urls) == {
        "https://fechado.test/vacina", "https://outro.test/vacina"}


def test_pick_prioridade_manda_mais_que_a_legibilidade(two_sites, lista_fechado):
    # Uma pauta manual (prioridade alta) de domínio sem-leitura continua primeira:
    # alguém a escolheu; o desempate só vale dentro da mesma prioridade.
    with session_scope() as db:
        discovery.store_idea(db, site_key="saude", origin="manual",
                             topic="Pauta escolhida pelo editor sobre hepatite",
                             url="https://fechado.test/manual",
                             priority=discovery.MANUAL_PRIORITY, published_at=NOW, now=NOW)
        discovery.store_idea(db, site_key="saude", origin="rss",
                             topic="Assunto corriqueiro de fonte aberta qualquer",
                             url="https://aberto.test/corriqueiro", published_at=NOW, now=NOW)
        first = pick_triaged(db, "saude", NOW)
    assert first is not None and first.source_urls == ("https://fechado.test/manual",)


def test_endereco_a_buscar_guarda_o_www_e_a_identidade_nao():
    """R42: há sítios que só respondem com o `www.`; a deduplicação continua sem ele."""
    from datetime import UTC, datetime

    from app.core.db import session_scope
    from app.services import discovery, sites
    from tests.conftest import fetch

    now = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
    with session_scope() as db:
        sites.sync(db)
        discovery.store_idea(db, site_key="saude", origin="manual", now=now,
                             url="https://WWW.Exemplo.test/artigo/?utm_source=x",
                             topic="Um tema qualquer de saúde mundial")
        picked = discovery.pick(db, "saude", now)
    row = fetch("SELECT canonical_url, source_urls FROM ideas")[0]
    assert row["canonical_url"] == "https://exemplo.test/artigo"
    assert row["source_urls"] == ["https://www.exemplo.test/artigo"]
    assert picked is not None and picked.source_urls == ("https://www.exemplo.test/artigo",)
    assert discovery.canonical_url("https://www.exemplo.test/a") == "https://exemplo.test/a"


def test_feed_em_ordem_cronologica_guarda_os_mais_novos(two_sites, feeds, monkeypatch):
    """22/11: o feed do Health Canada traz 525 itens desde 2020, do mais velho para o mais novo;
    o corte de `feed_max_items` ficava só com os velhos (0 pautas em 14 dias)."""
    monkeypatch.setattr(settings, "feed_max_items", 3)
    words = ("girafa", "cometa", "violino", "farol", "orquídea", "vulcão")
    old = [(f"Notícia antiga sobre {w}", f"https://a.test/velha-{n}", NOW - timedelta(days=400 - n))
           for n, w in enumerate(words)]
    new = [(f"Novidade de hoje: {w} e {w}s", f"https://a.test/nova-{n}",
            NOW - timedelta(hours=n + 1)) for n, w in enumerate(words[:2])]
    feeds["https://a.test/feed"] = rss(*old, *new)
    result = discover("saude")
    assert result.new == 2
    assert sorted(i["canonical_url"] for i in ideas("site_key = 'saude'")) == [
        "https://a.test/nova-0", "https://a.test/nova-1"]


def test_data_do_feed_em_utc_mesmo_com_o_container_noutro_fuso(monkeypatch):
    """22/11: `mktime` lia a hora UTC do feedparser como hora local (TZ=America/Sao_Paulo):
    toda data ficava 3 h no futuro."""
    import time

    monkeypatch.setenv("TZ", "America/Sao_Paulo")
    time.tzset()
    try:
        body = rss(("Item", "https://a.test/x", datetime(2026, 11, 22, 12, 0, tzinfo=UTC)))
        assert discovery._feed_items(body)[0][2] == datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


@pytest.mark.parametrize(("raw", "expected"), [
    ("November 19, 2026 15:57:58 +0000", datetime(2026, 11, 19, 15, 57, 58, tzinfo=UTC)),
    ("2026-11-19T12:00:00-03:00", datetime(2026, 11, 19, 15, 0, tzinfo=UTC)),
    ("2026-11-19 10:00:00", datetime(2026, 11, 19, 10, 0, tzinfo=UTC)),
    ("ontem à tarde", None),
])
def test_data_fora_do_padrao_do_rss(raw, expected):
    """Tax Foundation: «November 19, 2026 15:57:58 +0000» no pubDate — o feedparser não lê,
    o item ficava sem data e era descartado (0 pautas desde sempre)."""
    body = (f'<rss version="2.0"><channel><item><title>T</title><link>https://a.test/x</link>'
            f"<pubDate>{raw}</pubDate></item></channel></rss>").encode()
    assert discovery._feed_items(body)[0][2] == expected


def test_feed_pedido_na_lingua_da_fonte(two_sites, monkeypatch):
    """A AWS servia ao `Accept-Language: pt-BR` o feed em português parado desde 2024."""
    asked: list[str | None] = []

    def fake_fetch(url: str, **kwargs) -> fetcher.Fetched:
        asked.append(kwargs.get("language"))
        return fetcher.Fetched(url=url, status=200, content_type="application/rss+xml",
                               body=rss())

    monkeypatch.setattr(fetcher, "fetch", fake_fetch)
    with session_scope() as db:
        db.execute(text("UPDATE sources SET lang = 'en' WHERE feed_url = 'https://b.test/feed'"))
    discover("saude")
    assert sorted(asked, key=str) == ["en", "pt"]
    assert fetcher._accept_language("en") == "en,*;q=0.5"
    assert fetcher._accept_language("es").startswith("es,")
    assert fetcher._accept_language("pt").startswith(settings.content_locale)
    assert fetcher._accept_language(None).startswith(settings.content_locale)
