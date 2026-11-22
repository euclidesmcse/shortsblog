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
