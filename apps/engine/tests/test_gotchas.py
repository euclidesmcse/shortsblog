"""Regressão das armadilhas reais do Growth Engine (docs/GOTCHAS-growth-engine.md).

Um teste (ou grupo) por item, com o número do GOTCHA no nome. Onde a armadilha
vive numa biblioteca (httpx, RQ), o teste usa a biblioteca verdadeira com a rede
simulada por `httpx.MockTransport`. Os itens 3, 5–8 eram do pipeline antigo
(fases do RQ e SDKs da OpenAI/Anthropic) e saíram com ele na E19.
"""
from __future__ import annotations

import httpx
import pytest
from rq import SimpleWorker
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from tenacity import wait_none

from app.core import cache, db
from app.core.config import settings
from app.utils import http


# --- 1. SQL cru das migrações: `:` em JSON e `%` em PL/pgSQL ------------------
@pytest.fixture
def fresh_database(monkeypatch):
    """Base de dados nova e vazia, só para este teste (o bug só aparecia aí)."""
    url = make_url(settings.database_url)
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        conn.execute(text("DROP DATABASE IF EXISTS sb_gotcha"))
        conn.execute(text("CREATE DATABASE sb_gotcha"))
    fresh = create_engine(url.set(database="sb_gotcha"))
    monkeypatch.setattr(db, "engine", fresh)
    yield fresh
    fresh.dispose()
    with admin.connect() as conn:
        conn.execute(text("DROP DATABASE IF EXISTS sb_gotcha"))
    admin.dispose()


def test_gotcha1_migracoes_reais_aplicam_em_base_vazia(fresh_database):
    applied = db.run_migrations()
    assert applied == ["001_init.sql", "002_seed.sql", "003_cluster_index.sql",
                               "004_ai_providers.sql", "005_ai_seed.sql",
                               "006_ai_provider_health.sql", "007_provider_usage.sql",
                               "008_provider_usage_outcome.sql", "009_agy_disabled.sql",
                               "010_multisite.sql", "011_discovery.sql",
                               "012_review_report.sql", "013_idea_relevance.sql",
                               "014_illustration.sql", "015_mail_and_links.sql",
                               "016_newsletter.sql", "017_newsletter_origin.sql",
                               "018_newsletter_covers.sql", "019_rescue_origin.sql",
                               "020_users.sql", "021_idea_timeless.sql",
                               "022_system_secrets.sql", "023_idea_country.sql",
                               "024_drop_legacy_pipeline.sql"]
    with fresh_database.connect() as conn:
        # JSON com ':' do seed chegou intacto (não foi lido como bind).
        value = conn.execute(text("SELECT value FROM settings "
                                  "WHERE key = 'pipeline.enabled_phases'")).scalar_one()
        assert value == {"editorial": True, "production": True, "publishing": True}
        # O DO $$ ... format('%1$s') da 001 corre (o teste seguinte cobre o '%' isolado). Os
        # gatilhos que ele criava eram das tabelas do pipeline antigo, que a 024 apaga (D-111).
        assert conn.execute(text("SELECT count(*) FROM pg_proc WHERE proname = 'touch_updated_at'")
                            ).scalar_one() == 1
        assert conn.execute(text("SELECT to_regclass('articles')")).scalar_one() is None


def test_gotcha1_sql_com_dois_pontos_e_percentagem(fresh_database, tmp_path, monkeypatch):
    (tmp_path / "900_armadilha.sql").write_text(
        "CREATE TABLE t (v JSONB, s TEXT);\n"
        "INSERT INTO t VALUES ('{\"volume\":1.0,\"a\":\":b\"}', format('%1$s-%1$s', 'x'));\n",
        encoding="utf-8")
    monkeypatch.setattr(settings, "migrations_dir", str(tmp_path))
    assert db.run_migrations() == ["900_armadilha.sql"]
    with fresh_database.connect() as conn:
        row = conn.execute(text("SELECT v, s FROM t")).one()
    assert row.v == {"volume": 1.0, "a": ":b"} and row.s == "x-x"


# --- 2. RQ precisa de Redis sem decode_responses ------------------------------
def _soma(a: int, b: int) -> int:
    return a + b


def test_gotcha2_duas_conexoes_redis():
    assert cache.redis_client.connection_pool.connection_kwargs.get("decode_responses") is True
    assert not cache.rq_redis.connection_pool.connection_kwargs.get("decode_responses")


def test_gotcha2_worker_le_a_fila_sem_unicode_error():
    job = cache.queue_default.enqueue(_soma, 2, 3)
    SimpleWorker([cache.queue_default], connection=cache.rq_redis).work(burst=True)
    job.refresh()
    assert job.get_status() == "finished" and job.return_value() == 5


# --- 4. httpx: .json() ignora o charset declarado -----------------------------
def test_gotcha4_charset_iso_8859_1(monkeypatch):
    body = '["ação", "pão"]'.encode("iso-8859-1")
    headers = {"content-type": "text/javascript; charset=ISO-8859-1"}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body,
                                                                    headers=headers))
    real_client = httpx.Client
    monkeypatch.setattr(http.httpx, "Client",
                        lambda **kw: real_client(transport=transport, **kw))

    assert http.get_json("https://exemplo.test/sugestoes") == ["ação", "pão"]
    # A armadilha: o atalho do httpx descodifica como UTF-8 e rebenta.
    with pytest.raises(UnicodeDecodeError):
        httpx.Response(200, content=body, headers=headers).json()


def test_gotcha4_http_utilitarios(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/500":
            return httpx.Response(500)
        if request.url.path == "/naojson":
            return httpx.Response(200, text="<html>")
        return httpx.Response(404 if request.url.path == "/404" else 200, text="ok")

    real_client = httpx.Client
    monkeypatch.setattr(http.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(http.get.retry, "wait", wait_none())

    assert http.get_text("https://exemplo.test/ok") == "ok"
    assert http.get_text("https://exemplo.test/404") == ""
    assert http.get_json("https://exemplo.test/404") is None
    assert http.get_json("https://exemplo.test/naojson") is None
    with pytest.raises(httpx.HTTPStatusError):
        http.get("https://exemplo.test/500")
    assert sum(r.url.path == "/500" for r in seen) == 3  # retry em 5xx
    assert seen[0].headers["user-agent"].startswith("ShortsBlogBot/")
    assert http.post_json("https://exemplo.test/p", json_body={"a": 1}).status_code == 200
