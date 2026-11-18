"""Prontidão real do painel: provedores do gateway e credenciais do WordPress por site."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core.db import session_scope
from app.services import readiness
from tests.conftest import fetch


def _health(name: str, status: str) -> None:
    with session_scope() as db:
        db.execute(
            text("""INSERT INTO ai_provider_health (provider_id, status, source)
                    SELECT id, :s, 'probe' FROM ai_providers WHERE name = :n
                    ON CONFLICT (provider_id) DO UPDATE SET status = :s"""),
            {"s": status, "n": name})


def test_papel_pronto_so_com_provedor_ligado_e_sonda_ok():
    with session_scope() as db:
        assert readiness.role_ready(db, "writer")["ok"] is False   # nenhuma sonda ainda
    _health("claude", "ok")
    with session_scope() as db:
        state = readiness.role_ready(db, "writer")
    assert state == {"ok": True, "providers": ["claude"]}
    _health("claude", "not_authenticated")
    with session_scope() as db:
        assert readiness.role_ready(db, "writer")["ok"] is False


def test_wordpress_conta_os_sites_com_credencial(monkeypatch):
    with session_scope() as db:
        for key in ("alfa", "beta"):
            db.execute(text("INSERT INTO sites (site_key, name, base_url, wordpress_api_url) "
                            "VALUES (:k, :k, 'https://x.test', 'https://x.test/wp-json')"),
                       {"k": key})
    keys = [r["site_key"] for r in fetch("SELECT site_key FROM sites ORDER BY site_key")]
    for key in keys:
        monkeypatch.delenv(f"SB_WP_{key.upper()}_APP_USER", raising=False)
        monkeypatch.delenv(f"SB_WP_{key.upper()}_APP_PASSWORD", raising=False)
    with session_scope() as db:
        state = readiness.wordpress_ready(db)
    assert state["ok"] is False and state["have"] == 0 and state["total"] == len(keys)
    for key in keys:
        monkeypatch.setenv(f"SB_WP_{key.upper()}_APP_USER", "u")
        monkeypatch.setenv(f"SB_WP_{key.upper()}_APP_PASSWORD", "p")
    with session_scope() as db:
        state = readiness.wordpress_ready(db)
    assert state["ok"] is True and state["have"] == len(keys) and state["missing"] == []
    monkeypatch.delenv(f"SB_WP_{keys[0].upper()}_APP_PASSWORD")
    with session_scope() as db:
        assert readiness.wordpress_ready(db)["missing"] == [keys[0]]


@pytest.mark.parametrize("path", ["/dashboard", "/health/ready"])
def test_paginas_nao_dependem_das_chaves_antigas(client, path):
    response = client.get(path)
    assert response.status_code == 200
    if path == "/health/ready":
        checks = response.json()["checks"]
        assert "openai" not in checks and "anthropic" not in checks
        assert set(checks) >= {"redacao", "revisao", "wordpress"}
