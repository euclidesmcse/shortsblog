"""Fontes paradas no resumo diário (22/11: os 11 feeds da ONU News falharam semanas a fio)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import text

from app.core.db import session_scope
from app.services import discovery, feed_health, sites
from app.services.notify import digests

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def _source(name: str, *, status: str = "ok", fails: int = 0, site: str = "saude",
            age_days: int = 60) -> int:
    with session_scope() as db:
        return db.execute(
            text("""INSERT INTO sources (site_key, name, feed_url, domain, tier, lang,
                                         last_status, fail_count, created_at)
                    VALUES (:s, :n, :u, 'x.test', 'veiculo', 'pt', :st, :f, :c) RETURNING id"""),
            {"s": site, "n": name, "u": f"https://{abs(hash(name))}.test/feed", "st": status,
             "f": fails, "c": NOW - timedelta(days=age_days)}).scalar_one()


def _idea(source: int, days_ago: int, topic: str) -> None:
    with session_scope() as db:
        _, idea = discovery.store_idea(db, site_key="saude", origin="rss", topic=topic,
                                       url=f"https://x.test/{source}/{days_ago}",
                                       now=NOW, source_id=source)
        db.execute(text("UPDATE ideas SET created_at = :c WHERE id = :i"),
                   {"c": NOW - timedelta(days=days_ago), "i": idea})


def test_erro_seguido_e_fonte_calada_aparecem_e_o_resto_nao():
    with session_scope() as db:
        sites.sync(db)
        db.execute(text("UPDATE sites SET enabled = (site_key = 'saude')"))
        db.execute(text("DELETE FROM sources"))
    dead = _source("ONU News — Saúde", status="http_status", fails=504)
    _source("Erro de agora", status="timeout", fails=3)                  # passageiro: não
    _source("Outro blog morto", status="http_status", fails=500, site="politica")  # desligado
    quiet = _source("Feed abandonado")
    _idea(quiet, 30, "Vacina nova aprovada na Europa para crianças")
    _source("Nunca trouxe nada")
    _source("Acabou de entrar", age_days=2)                              # sem tempo ainda
    alive = _source("Feed ativo")
    _idea(alive, 1, "Campanha contra a dengue começa em três países")
    with session_scope() as db:
        info = feed_health.summary(db, NOW)
    assert [(r["name"], r["runs"]) for r in info["failing"]] == [("ONU News — Saúde", 504)]
    assert [(r["name"], r["days"]) for r in info["silent"]] == [
        ("Feed abandonado", 30), ("Nunca trouxe nada", None)]
    assert dead

    html = "".join(digests._feeds_html(info))
    assert "Fontes paradas" in html and "erro HTTP, 504 leituras seguidas (~21 dias)" in html
    assert "Feed abandonado (30 dias)" in html and "nunca trouxe pauta" in html
    assert digests._feeds_html({"failing": [], "silent": []}) == []


def test_fontes_novas_mostram_o_que_ja_trouxeram():
    """23/11: a próxima poda sai dos números das fontes recentes, não de consulta à mão."""
    with session_scope() as db:
        sites.sync(db)
        db.execute(text("UPDATE sites SET enabled = (site_key = 'saude')"))
        db.execute(text("DELETE FROM sources"))
    old = _source("Fonte antiga", age_days=60)
    _idea(old, 1, "Notícia de uma fonte que não é nova")
    fresh = _source("PLOS Global Public Health", age_days=3)
    _idea(fresh, 1, "Estudo mundial sobre resistência a antibióticos em hospitais")
    _idea(fresh, 2, "Relatório compara vacinação infantil em cinquenta países")
    _source("Sem nada ainda", age_days=1)
    _source("Outro blog", age_days=2, site="politica")                   # blog desligado
    with session_scope() as db:
        db.execute(text("UPDATE ideas SET relevance = CASE WHEN topic LIKE 'Estudo%' THEN 8 "
                        "ELSE 3 END, status = CASE WHEN topic LIKE 'Estudo%' THEN 'used' "
                        "ELSE status END"))
        idea = db.execute(text("SELECT id FROM ideas WHERE topic LIKE 'Estudo%'")).scalar_one()
        db.execute(text("""INSERT INTO publication_jobs (site_key, local_date, slot_number,
                                                         due_at_utc, produce_after_utc,
                                                         status, idea_id)
                           VALUES ('saude', :d, 1, :n, :n, 'PUBLISHED_CONFIRMED', :i)"""),
                   {"d": NOW.date(), "n": NOW, "i": idea})
        rows = feed_health.newcomers(db, NOW)
    assert [(r["name"], r["ideas"], r["good"], r["average"], r["taken"], r["posts"])
            for r in rows] == [("PLOS Global Public Health", 2, 1, 5.5, 1, 1),
                               ("Sem nada ainda", 0, 0, None, 0, 0)]
    html = "".join(digests._new_feeds_html(rows))
    assert "Fontes novas" in html and "PLOS Global Public Health" in html
    assert "2 · 1 · 5,5 · 1 · <b>1</b>" in html and "(nada ainda)" in html
    assert digests._new_feeds_html([]) == []


def test_entrada_de_pautas_do_dia_com_o_que_o_teto_cortou():
    """24/11 (D-118): a medição do teto sai no resumo, sem consulta à mão."""
    with session_scope() as db:
        sites.sync(db)
        db.execute(text("UPDATE sites SET enabled = site_key IN ('saude', 'cloud')"))
        db.execute(text("DELETE FROM sources"))
    big = _source("Medical Xpress")
    small = _source("PLOS")
    with session_scope() as db:
        for source, topic in ((big, "Estudo mundial sobre resistência a antibióticos"),
                              (big, "Relatório compara vacinação infantil em cinquenta países"),
                              (small, "Surto de cólera avança no leste da África")):
            discovery.store_idea(db, site_key="saude", origin="rss", topic=topic,
                                 url=f"https://x.test/{abs(hash(topic))}", now=NOW,
                                 source_id=source)
        db.execute(text("UPDATE ideas SET created_at = :c"), {"c": NOW})
    _idea(small, 2, "Notícia de outro dia que não entra na conta")
    with session_scope() as db:
        db.execute(text("UPDATE ideas SET relevance = CASE WHEN topic LIKE 'Estudo%' THEN 8 "
                        "WHEN topic LIKE 'Relat%' THEN 3 END"))
        for source, n in ((big, 30), (small, 2)):
            db.execute(text("INSERT INTO feed_capped VALUES (:d, :s, :n)"),
                       {"d": NOW.date(), "s": source, "n": n})
        db.execute(text("INSERT INTO feed_capped VALUES (:d, :s, 99)"),
                   {"d": NOW.date() - timedelta(days=1), "s": big})      # outro dia
        rows = feed_health.intake(db, NOW.date())
    assert rows == [
        {"site": "cloud", "ideas": 0, "triaged": 0, "good": 0, "capped": 0, "top_capped": []},
        {"site": "saude", "ideas": 3, "triaged": 2, "good": 1, "capped": 32,
         "top_capped": [("Medical Xpress", 30), ("PLOS", 2)]}]
    html = "".join(digests._intake_html(rows))
    assert "Entrada de pautas" in html and "teto de 40 por feed" in html
    assert "<b>saude</b> — 3 · 2 · 1 · 32" in html and "(Medical Xpress 30, PLOS 2)" in html
    assert digests._intake_html([]) == []
