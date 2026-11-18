"""D-110: cobertura mundial como regra do código, igual para todos os blogs.

Cada ideia sabe de que país trata (a triagem preenche); a escolha recusa a ideia que estoura a
cota do blog na janela — no máximo `world_brazil_max` posts centrados no Brasil e
`world_country_max` num mesmo país qualquer. A recusada fica na fila; a pauta manual passa.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.core.db import session_scope
from app.services import discovery, sites
from tests.conftest import fetch

NOW = datetime(2026, 11, 16, 15, 0, tzinfo=UTC)


@pytest.fixture
def blog():
    with session_scope() as db:
        sites.sync(db)
        db.execute(text("UPDATE sites SET enabled = true WHERE site_key = 'politica'"))


def idea(topic: str, country: str | None, *, origin: str = "rss", priority: int = 0,
         relevance: int = 8) -> int:
    with session_scope() as db:
        _, idea_id = discovery.store_idea(db, site_key="politica", origin=origin, topic=topic,
                                          url=None, priority=priority, now=NOW)
        db.execute(text("UPDATE ideas SET country = :c, relevance = :r WHERE id = :i"),
                   {"c": country, "r": relevance, "i": idea_id})
    return int(idea_id)


def published(country: str, *, days_ago: int = 1, status: str = "PUBLISHED_CONFIRMED",
              slot: int = 1) -> None:
    """Um post do blog na janela, feito de uma ideia do país dado."""
    used = idea(f"Post anterior {country} {days_ago} {slot} {status}", country)
    due = NOW - timedelta(days=days_ago)
    with session_scope() as db:
        db.execute(text("UPDATE ideas SET status = 'used' WHERE id = :i"), {"i": used})
        db.execute(text("""INSERT INTO publication_jobs (site_key, local_date, slot_number,
                                  due_at_utc, produce_after_utc, status, idea_id)
                           VALUES ('politica', :d, :n, :t, :t, :st, :i)"""),
                   {"d": due.date(), "n": slot, "t": due, "st": status, "i": used})


def pick() -> discovery.Picked | None:
    with session_scope() as db:
        return discovery.pick(db, "politica", NOW)


def status(idea_id: int) -> str:
    return fetch(f"SELECT status FROM ideas WHERE id = {idea_id}")[0]["status"]


def test_codigo_do_pais_normalizado():
    assert discovery.country_code("br") == "BR"
    assert discovery.country_code(" us ") == "US"
    for value in ("", None, "Brasil", "mundial", "B", "123"):
        assert discovery.country_code(value) == discovery.GLOBAL


def test_regra_vale_para_todos_os_blogs_sem_configuracao_por_blog():
    # É configuração global do motor, não do blog: um blog novo herda a mesma regra.
    assert settings.world_brazil_max == 1
    assert settings.world_country_max == 2
    assert settings.world_window_days == 7


def test_brasil_cheio_retem_a_ideia_brasileira_e_escolhe_outra(blog):
    published("BR")
    brazil = idea("Senado aprova projeto de lei estadual", "BR", relevance=10)
    world = idea("Cúpula do G20 fecha acordo sobre dívida", "XX", relevance=6)
    picked = pick()
    assert picked is not None and picked.id == world
    assert status(brazil) == "new"                 # retida, não descartada


def test_primeiro_post_do_brasil_na_janela_passa(blog):
    brazil = idea("Congresso brasileiro vota reforma", "BR")
    picked = pick()
    assert picked is not None and picked.id == brazil


def test_post_fora_da_janela_nao_conta(blog):
    published("BR", days_ago=settings.world_window_days + 1)
    brazil = idea("STF julga caso de repercussão", "BR")
    assert pick().id == brazil


def test_so_contam_posts_feitos_nao_os_bloqueados(blog):
    published("BR", status="BLOCKED")
    published("BR", status="REJECTED", slot=2)
    brazil = idea("Câmara aprova orçamento", "BR")
    assert pick().id == brazil


def test_post_ja_produzido_para_amanha_conta(blog):
    published("BR", days_ago=-1, status="APPROVED")
    idea("Governo federal anuncia pacote", "BR")
    assert pick() is None


def test_nenhum_pais_passa_de_dois_na_janela(blog):
    published("US", days_ago=1)
    published("US", days_ago=2)
    us = idea("Congresso americano aprova orçamento", "US", relevance=10)
    fr = idea("França convoca eleição antecipada", "FR", relevance=6)
    assert pick().id == fr
    assert status(us) == "new"


def test_mundial_nao_tem_cota(blog):
    for day in range(1, 6):
        published("XX", days_ago=day)
    world = idea("ONU publica relatório sobre clima", "XX")
    assert pick().id == world


def test_ideia_de_feed_sem_pais_espera_a_triagem(blog):
    untriaged = idea("Notícia ainda sem triagem", None)
    assert pick() is None
    assert status(untriaged) == "new"


def test_pauta_manual_e_escolha_do_editor_e_passa(blog):
    published("BR")
    manual = idea("Pauta do editor sobre o Brasil", "BR", origin="manual", priority=100)
    assert pick().id == manual


def test_pauta_de_resgate_conta_e_respeita_a_cota(blog):
    published("BR")
    rescue = idea("Federalismo no Brasil explicado", "BR", origin="rescue", priority=90)
    world = idea("Como funcionam os parlamentos", "XX", relevance=6)
    assert pick().id == world
    assert status(rescue) == "new"


def test_store_idea_guarda_o_pais(blog):
    with session_scope() as db:
        _, idea_id = discovery.store_idea(db, site_key="politica", origin="rescue",
                                          topic="Como funcionam as eleições", url=None,
                                          now=NOW, country="fr")
        _, other = discovery.store_idea(db, site_key="politica", origin="rescue",
                                        topic="Sistemas parlamentares comparados", url=None,
                                        now=NOW, country="")
    assert fetch(f"SELECT country FROM ideas WHERE id = {idea_id}")[0]["country"] == "FR"
    assert fetch(f"SELECT country FROM ideas WHERE id = {other}")[0]["country"] == "XX"


def test_triagem_grava_o_pais_e_o_prompt_traz_a_regra(blog, monkeypatch):
    from app.services.ai import chain

    first = idea("Prefeitura de Campinas reforma praça", None, relevance=0)
    second = idea("Eleições na Alemanha", None, relevance=0)
    with session_scope() as db:
        db.execute(text("UPDATE ideas SET relevance = NULL"))
    seen: dict = {}

    def fake_run(role, task, **kwargs):
        seen["task"], seen["schema"] = task, kwargs["schema"]
        return SimpleNamespace(data={"scores": [{"id": first, "score": 2, "country": "br"},
                                                {"id": second, "score": 8, "country": "DE"}],
                                     "groups": []})

    monkeypatch.setattr(chain, "run", fake_run)
    monkeypatch.setattr(settings, "triage_cache_minutes", 0)
    with session_scope() as db:
        site = sites.get(db, "politica")
        discovery.triage(db, site)
    rows = {r["id"]: r for r in fetch("SELECT id, country, status FROM ideas")}
    assert rows[first]["country"] == "BR" and rows[first]["status"] == "discarded"
    assert rows[second]["country"] == "DE" and rows[second]["status"] == "new"
    assert "country" in seen["schema"]["properties"]["scores"]["items"]["required"]
    assert "cobre o MUNDO (D-110)" in seen["task"]
    assert "inclusive o Brasil" in seen["task"]
