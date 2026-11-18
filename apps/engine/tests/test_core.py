"""Núcleo: utilitários de texto, configuração, segurança, migrações e Redis."""
from __future__ import annotations

import logging

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.core import cache, security
from app.core.config import Settings, settings
from app.core.db import get_setting, run_migrations, session_scope
from app.utils import text as t
from tests.conftest import fetch


# --- utils/text -------------------------------------------------------------
def test_normalize_remove_acentos_e_pontuacao():
    assert t.normalize("  Ação: Cópia de Segurança!! ") == "acao copia de seguranca"


def test_slugify_e_truncate():
    assert t.slugify("Backup de Servidores em 2026") == "backup-de-servidores-em-2026"
    assert t.truncate("curto", 10) == "curto"
    cut = t.truncate("uma frase bem mais longa do que o limite", 20)
    assert len(cut) <= 20 and cut.endswith("…") and " " not in cut[-2:]


def test_word_count_ignora_html_e_vazio():
    assert t.word_count("<p>duas palavras</p>") == 2
    assert t.word_count("   ") == 0
    assert t.reading_minutes(10) == 1
    assert t.reading_minutes(2200) == 10


def test_markdown_para_html_com_tabela():
    html = t.to_html("# T\n\n| a | b |\n| --- | --- |\n| 1 | 2 |")
    assert "<table>" in html and "<h1" in html
    assert t.strip_html("<b>a</b>  <i>b</i>") == "a b"
    assert t.content_hash("Ação") == t.content_hash("acao")


def test_extract_headings():
    heads = t.extract_headings("# A\n## B\ntexto\n### C\n##### demasiado fundo")
    assert [(h["level"], h["text"]) for h in heads] == [(1, "A"), (2, "B"), (3, "C")]


# --- config -----------------------------------------------------------------
def test_defaults_tem_identidade_sb(monkeypatch):
    for var in ("SB_API_TOKEN", "DATABASE_URL", "REDIS_URL"):
        monkeypatch.delenv(var, raising=False)
    fresh = Settings(_env_file=None)
    assert fresh.sb_host == "painel.shortsblog.com"
    assert "sb-engine-postgres" in fresh.database_url
    assert not hasattr(fresh, "qdrant_url") and not hasattr(fresh, "openai_api_key")
    assert not hasattr(fresh, "growth_api_token")
    assert not hasattr(fresh, "esp_provider")


def test_mail_ready_exige_remetente_e_canal(monkeypatch):
    from app.services import vault

    monkeypatch.setattr(settings, "mail_from_email", "")
    monkeypatch.setattr(settings, "brevo_api_key", "k")
    assert not vault.mail_ready()
    monkeypatch.setattr(settings, "mail_from_email", "motor@shortsblog.com")
    assert vault.mail_ready()
    monkeypatch.setattr(settings, "brevo_api_key", "")
    monkeypatch.setattr(settings, "smtp_host", "")
    assert not vault.mail_ready()


# --- security ---------------------------------------------------------------
async def _call_auth(header: str):
    await security.require_api_token(authorization=header)


def test_token_api(monkeypatch):
    import asyncio

    asyncio.run(_call_auth(f"Bearer {settings.sb_api_token}"))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_call_auth("Bearer errado"))
    assert exc.value.status_code == 401

    monkeypatch.setattr(settings, "sb_api_token", "")
    with pytest.raises(HTTPException) as exc:
        asyncio.run(_call_auth("Bearer qualquer"))
    assert exc.value.status_code == 503


def test_sign_unsign_e_hash_ip():
    token = security.sign({"article_id": 7})
    assert security.unsign(token) == {"article_id": 7}
    with pytest.raises(HTTPException):
        security.unsign(token + "x")
    digest = security.hash_ip("203.0.113.9")
    assert len(digest) == 32 and "203" not in digest


# --- migrações --------------------------------------------------------------
def _tables() -> set[str]:
    rows = fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    return {r["tablename"] for r in rows}


def test_esquema_so_com_tabelas_do_escopo():
    tables = _tables()
    for kept in ("settings", "schema_migrations", "sites", "publication_jobs",
                 "provider_usage"):
        assert kept in tables
    # D-111 (E19): as do pipeline herdado saíram na migração 024.
    for removed in ("keywords", "content_briefs", "articles", "article_revisions",
                    "article_images", "llm_usage", "pipeline_runs", "affiliate_programs",
                    "leads", "social_posts", "email_sends",
                    "gsc_metrics", "backlinks", "kpi_daily", "serp_snapshots",
                    "keyword_discoveries"):
        assert removed not in tables


def test_migracoes_idempotentes():
    assert run_migrations() == []
    files = [r["filename"] for r in fetch("SELECT filename FROM schema_migrations")]
    assert sorted(files) == ["001_init.sql", "002_seed.sql", "003_cluster_index.sql",
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


def test_migracao_editada_avisa_e_nao_reaplica(caplog):
    with session_scope() as db:
        db.execute(text("UPDATE schema_migrations SET checksum = 'outro' "
                        "WHERE filename = '002_seed.sql'"))
    with caplog.at_level(logging.WARNING):
        assert run_migrations() == []
    assert any("mudou depois de aplicada" in r.getMessage() for r in caplog.records)


def test_seed_e_get_setting():
    with session_scope() as db:
        phases = get_setting(db, "pipeline.enabled_phases")
        assert phases == {"editorial": True, "production": True, "publishing": True}
        assert get_setting(db, "nao.existe", "padrao") == "padrao"


# --- cache / locks ----------------------------------------------------------
def test_lock_exclusivo():
    assert cache.acquire_lock("fase-x", ttl=30)
    assert not cache.acquire_lock("fase-x", ttl=30)
    cache.release_lock("fase-x")
    assert cache.acquire_lock("fase-x", ttl=30)


def test_contador_diario_e_cache_json():
    assert cache.get_daily("articles") == 0
    assert cache.incr_daily("articles") == 1
    assert cache.incr_daily("articles", 2) == 3
    assert cache.get_daily("articles") == 3
    cache.cache_set("k", {"a": [1, "ç"]}, ttl=30)
    assert cache.cache_get("k") == {"a": [1, "ç"]}
    assert cache.cache_get("ausente") is None
