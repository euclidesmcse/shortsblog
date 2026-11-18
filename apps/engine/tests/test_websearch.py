"""Reforço de pesquisa por busca na web (D-063, E14.5b): só ligações, teto, cache, sem bloquear."""
from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.core import cache
from app.core.config import settings
from app.services import terms, websearch

SITE = {"site_key": "cloud"}


class Searx:
    """SearXNG simulado: responde por categoria e guarda os pedidos."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.general = ["https://aws.amazon.com/new/", "https://www.facebook.com/x",
                        "https://aws.amazon.com/other", "http://insegura.test/a",
                        "https://news.example/a", "https://blog.example/b",
                        "https://another.example/c"]
        self.news = ["https://agency.example/n"]
        self.fail: Exception | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            raise self.fail
        params = dict(request.url.params)
        self.requests.append(params)
        urls = self.news if params["categories"] == "news" else self.general
        return httpx.Response(200, json={"results": [{"url": u} for u in urls]})


@pytest.fixture
def searx(monkeypatch) -> Searx:
    fake = Searx()
    monkeypatch.setattr(settings, "searxng_url", "http://searxng:8080")
    monkeypatch.setattr(websearch, "_transport", httpx.MockTransport(fake))
    monkeypatch.setattr(websearch.chain, "run", lambda *a, **k: SimpleNamespace(
        data={"query_en": "AWS announces new service", "query_pt": "AWS lança novo serviço"}))
    # Sem a lista real de sítios sem leitura (D-070): o teste próprio dela vem abaixo.
    monkeypatch.setattr(terms, "no_fetch_domains", lambda: frozenset())
    return fake


def test_sitios_sem_leitura_nao_entram_no_reforco(searx, monkeypatch):
    monkeypatch.setattr(terms, "no_fetch_domains", lambda: frozenset({"amazon.com"}))
    links = websearch.search("AWS anuncia novo serviço", site=SITE)
    # Sem a amazon.com restam 3 domínios utilizáveis: a busca segue até as notícias.
    assert links == ["https://news.example/a", "https://blog.example/b",
                     "https://another.example/c", "https://agency.example/n"]


def test_desligado_por_omissao_nao_faz_nada(monkeypatch):
    monkeypatch.setattr(settings, "searxng_url", "")
    assert websearch.enabled() is False
    assert websearch.search("Qualquer título", site=SITE) == []


def test_so_ligacoes_https_uma_por_dominio_sem_redes_sociais_nem_excluidos(searx):
    links = websearch.search("AWS anuncia novo serviço", site=SITE,
                             exclude=["https://news.example/ja-tenho"])
    # Com o news.example excluído restam 3 utilizáveis: a busca segue até as notícias.
    assert links == ["https://aws.amazon.com/new/", "https://blog.example/b",
                     "https://another.example/c", "https://agency.example/n"]


def test_primeiro_a_categoria_geral_em_ingles_com_tempo_e_noticias_so_se_faltar(searx):
    websearch.search("AWS anuncia novo serviço", site=SITE, need=2)
    assert [r["categories"] for r in searx.requests] == ["general"]
    assert searx.requests[0]["language"] == "en" and searx.requests[0]["time_range"] == "week"
    assert searx.requests[0]["q"] == "AWS announces new service"
    searx.requests.clear()
    searx.general = ["https://one.example/a"]
    websearch.search("Outro assunto totalmente diferente aqui", site=SITE, need=2)
    assert [(r["categories"], r["language"]) for r in searx.requests] == [
        ("general", "en"), ("general", "pt-BR"), ("news", "en")]
    assert searx.requests[1]["q"] == "AWS lança novo serviço"
    assert "time_range" not in searx.requests[2]


def test_ideia_em_portugues_busca_primeiro_em_portugues_e_em_espanhol_no_titulo(searx):
    # E14.5g: a língua da fonte primeiro; depois inglês (D-064).
    searx.general = ["https://one.example/a"]
    stats: dict = {}
    websearch.search("Ministério anuncia novo serviço digital", site=SITE, lang="pt",
                     stats=stats)
    assert [(r["categories"], r["language"]) for r in searx.requests] == [
        ("general", "pt-BR"), ("general", "en"), ("news", "en")]
    assert stats["languages"] == ["pt-BR", "en", "en"] and stats["queries"] == 3
    searx.requests.clear()
    websearch.search("El Gobierno anuncia un nuevo servicio digital", site=SITE, lang="es")
    assert searx.requests[0]["language"] == "es"
    assert searx.requests[0]["q"] == "El Gobierno anuncia un nuevo servicio digital"
    assert [r["language"] for r in searx.requests[1:]] == ["en", "pt-BR", "en"]


def test_cache_evita_nova_consulta_e_o_teto_diario_trava(searx, monkeypatch):
    websearch.search("AWS anuncia novo serviço", site=SITE)
    n = len(searx.requests)
    websearch.search("AWS anuncia novo serviço", site=SITE)
    assert len(searx.requests) == n                       # veio do cache
    monkeypatch.setattr(settings, "searxng_daily_cap", cache.get_daily("searxng_queries"))
    assert websearch.search("Assunto novo sem cache ainda", site=SITE) == []
    assert len(searx.requests) == n


def test_stats_conta_consultas_cache_teto_e_falha(searx, monkeypatch):
    stats: dict = {}
    websearch.search("AWS anuncia novo serviço", site=SITE, stats=stats)
    assert stats == {"queries": len(searx.requests), "languages": ["en"], "cached": False,
                     "capped": False, "job_capped": False, "failed": False}
    websearch.search("AWS anuncia novo serviço", site=SITE, stats=stats)
    assert stats["queries"] == 0 and stats["cached"] is True
    monkeypatch.setattr(settings, "searxng_daily_cap", cache.get_daily("searxng_queries"))
    websearch.search("Assunto novo sem cache ainda", site=SITE, stats=stats)
    assert stats["capped"] is True and stats["queries"] == 0
    monkeypatch.setattr(settings, "searxng_daily_cap", 10_000)
    searx.fail = httpx.ConnectError("down")
    websearch.search("Mais um assunto sem cache", site=SITE, stats=stats)
    assert stats["failed"] is True


def test_orcamento_por_job_trava_sem_esgotar_o_dia(searx, monkeypatch):
    """4/11: um job de pauta difícil gastou as 60 consultas do dia sozinho e todos os jobs
    seguintes rodaram `capped`. O orçamento por job trava o gastador e poupa o teto do dia."""
    monkeypatch.setattr(settings, "searxng_job_cap", 2)
    searx.general = ["https://one.example/a"]     # 1 utilizável: a busca quer todas as rondas
    stats: dict = {}
    websearch.search("Assunto difícil sem fontes", site=SITE, stats=stats, job_id=184)
    assert stats["queries"] == 2                  # parou no orçamento do job, não no do dia
    assert stats["capped"] is True and stats["job_capped"] is True
    assert cache.get_daily("searxng_queries") == 2
    assert cache.get_daily("searxng_queries:job:184") == 2
    # Outro job, e uma busca sem job, seguem: o teto do dia continua com folga.
    searx.general = ["https://aws.amazon.com/new/", "https://news.example/a",
                     "https://blog.example/b", "https://another.example/c"]
    stats2: dict = {}
    websearch.search("Outro assunto de outro job", site=SITE, stats=stats2, job_id=186)
    assert stats2["capped"] is False and stats2["job_capped"] is False and stats2["queries"] > 0
    stats3: dict = {}
    websearch.search("Mais um assunto sem job nenhum", site=SITE, stats=stats3)
    assert stats3["capped"] is False and stats3["queries"] > 0


def test_pauta_de_resgate_busca_no_bolso_proprio_do_orcamento(searx, monkeypatch):
    """10/11: o requeue do 240 gastou o orçamento do job na fila normal e as 5 pautas de
    resgate (D-101) morreram `job_capped` com 0 consultas. O bolso `rescue` garante busca
    ao resgate sem afrouxar nada: mesmo teto por bolso, teto do dia valendo para todos."""
    monkeypatch.setattr(settings, "searxng_job_cap", 2)
    searx.general = ["https://one.example/a"]     # 1 utilizável: a busca quer todas as rondas
    stats: dict = {}
    websearch.search("Assunto difícil sem fontes", site=SITE, stats=stats, job_id=240)
    assert stats["job_capped"] is True            # bolso normal esgotado…
    rescue: dict = {}
    websearch.search("Higiene do sono: o que recomendam as fontes oficiais", site=SITE,
                     stats=rescue, job_id=240, pocket="rescue")
    assert rescue["queries"] == 2                 # …e o resgate ainda busca, no bolso dele,
    assert cache.get_daily("searxng_queries:job:240:rescue") == 2
    assert cache.get_daily("searxng_queries") == 4    # sempre dentro do teto do dia,
    capped: dict = {}
    websearch.search("Outro tema de resgate sem cache", site=SITE, stats=capped,
                     job_id=240, pocket="rescue")
    assert capped["job_capped"] is True and capped["queries"] == 0   # e com teto próprio.


def test_orcamento_por_job_zero_desliga_e_sem_job_nao_debita(searx, monkeypatch):
    monkeypatch.setattr(settings, "searxng_job_cap", 0)
    stats: dict = {}
    websearch.search("AWS anuncia novo serviço", site=SITE, stats=stats, job_id=184)
    assert stats["capped"] is False and stats["job_capped"] is False and stats["queries"] > 0
    # Com o orçamento desligado o contador do job nem é escrito.
    assert cache.get_daily("searxng_queries:job:184") == 0


@pytest.mark.parametrize("failure", [httpx.ConnectError("down"), httpx.ReadTimeout("slow"),
                                     ValueError("json")])
def test_qualquer_falha_devolve_lista_vazia(searx, failure):
    searx.fail = failure
    assert websearch.search("AWS anuncia novo serviço", site=SITE) == []


def test_falha_no_filtro_do_cache_tambem_nao_bloqueia(searx, monkeypatch):
    """Regressão (revisão E14): com as ligações já no cache, o filtro final rodava fora do
    try — um sem-leitura.yaml ausente (TermsConfigError) derrubava o job de produção."""
    websearch.search("AWS anuncia novo serviço", site=SITE)      # enche o cache

    def boom() -> frozenset[str]:
        raise terms.TermsConfigError("sem-leitura.yaml ausente")

    monkeypatch.setattr(terms, "no_fetch_domains", boom)
    stats: dict = {}
    assert websearch.search("AWS anuncia novo serviço", site=SITE, stats=stats) == []
    assert stats["cached"] is True and stats["failed"] is True


def test_resposta_com_erro_http_devolve_lista_vazia(searx, monkeypatch):
    monkeypatch.setattr(websearch, "_transport",
                        httpx.MockTransport(lambda r: httpx.Response(429, text="slow down")))
    assert websearch.search("AWS anuncia novo serviço", site=SITE) == []


def test_a_falta_total_de_ia_sobe_em_vez_de_buscar_com_o_titulo(searx, monkeypatch):
    # R27 (2/11): com todos os provedores fora, buscar com o título cru só queimava o teto
    # diário do SearXNG sem artigo possível — a produção é que deve esperar.
    def boom(*a, **k):
        raise websearch.chain.ChainExhausted("researcher", ())

    monkeypatch.setattr(websearch.chain, "run", boom)
    with pytest.raises(websearch.chain.ChainExhausted):
        websearch.search("Parlamento aprova o novo orçamento da união", site=SITE)
    assert searx.requests == []          # nenhuma consulta gasta no teto diário


def test_se_a_ia_falhar_a_consulta_e_o_proprio_titulo(searx, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("sem provedor")

    monkeypatch.setattr(websearch.chain, "run", boom)
    websearch.search("AWS anuncia novo serviço", site=SITE)
    assert searx.requests[0]["q"] == "AWS anuncia novo serviço"
    assert websearch.queries("Outro título", site=SITE) == {"en": "Outro título",
                                                            "pt": "Outro título"}
    # Regressão (revisão E14): a falha transitória não fica 24 h no cache — com a IA de
    # volta, o mesmo título vira consulta de verdade na chamada seguinte.
    monkeypatch.setattr(websearch.chain, "run", lambda *a, **k: SimpleNamespace(
        data={"query_en": "another service launch", "query_pt": "outro serviço lançado"}))
    assert websearch.queries("Outro título", site=SITE) == {"en": "another service launch",
                                                            "pt": "outro serviço lançado"}


# --- E14.5d: fontes oficiais, data da página e marca no painel --------------------------------


def test_is_official_reconhece_governo_universidade_e_organismos():
    for domain in ("saude.gov.br", "cdc.gov", "who.int", "ecdc.europa.eu", "camara.leg.br",
                   "stf.jus.br", "usp.br.edu.br", "ox.ac.uk", "thelancet.com", "gov.br",
                   "boletim.gob.mx",
                   # E14.5h: multilaterais, parlamentos e governos de outros países
                   "data.unicef.org", "oecd.org", "imf.org", "worldbank.org", "oas.org",
                   "bundestag.de", "assemblee-nationale.fr", "canada.ca", "bag.admin.ch",
                   "mhlw.go.jp", "kdca.go.kr", "health.govt.nz", "sozialministerium.gv.at"):
        assert websearch.is_official(domain), domain
    for domain in ("blog.example", "g1.globo.com", "facebook.com", "gov.example.com", "mp.com",
                   "go.com", "notunicef.org"):
        assert not websearch.is_official(domain), domain
    assert websearch.is_official("g1.globo.com", ["g1.globo.com"])
    assert websearch.is_official("noticias.g1.globo.com", ["www.g1.globo.com"])


def test_blog_com_so_oficiais_filtra_o_reforco(searx):
    searx.general = ["https://blog.example/a", "https://www.saude.gov.br/n",
                    "https://g1.globo.com/x", "https://news.example/b", "https://who.int/y"]
    official = {"site_key": "saude", "config": {"search_policy": {
        "official_only": True, "allow_domains": ["g1.globo.com"]}}}
    links = websearch.search("Vacinação ampliada no país", site=official)
    assert links == ["https://www.saude.gov.br/n", "https://g1.globo.com/x", "https://who.int/y"]
    # Sem a política (outro blog) entram todos, menos redes sociais.
    searx.requests.clear()
    other = websearch.search("Vacinação ampliada no país", site={"site_key": "cloud"})
    assert "https://blog.example/a" in other and len(other) == 5


def test_official_only_continua_consultando_ate_ter_oficiais(searx):
    """Regressão (revisão E14): a parada antecipada contava domínios que o filtro final
    tira — num blog `official_only`, 4 domínios quaisquer na 1.ª rodada encerravam a
    busca e o reforço voltava quase vazio com rodadas ainda por fazer."""
    searx.general = ["https://blog.example/a", "https://news.example/b",
                     "https://media.example/c", "https://outro.example/d",
                     "https://www.saude.gov.br/n"]
    searx.news = ["https://who.int/y", "https://cdc.gov/z"]
    official = {"site_key": "saude", "config": {"search_policy": {"official_only": True}}}
    links = websearch.search("Vacinação ampliada no país", site=official, need=2)
    assert links == ["https://www.saude.gov.br/n", "https://who.int/y", "https://cdc.gov/z"]
    # As rodadas de notícia correram (antes a busca parava na 1.ª rodada geral).
    assert [r["categories"] for r in searx.requests] == ["general", "general", "news"]
