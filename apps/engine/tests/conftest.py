"""Fixtures comuns.

Corre dentro da stack efémera `sb-engine-test` (Postgres e Redis reais, rede
sem internet). As variáveis de ambiente vêm de deploy/stacks/engine/compose.test.yml
e são lidas por `app.core.config` no import — por isso nada de `app` é importado
antes de este ficheiro ser carregado.
"""
from __future__ import annotations

import os
import pathlib

import pytest

if os.environ.get("ENVIRONMENT") != "test":  # pragma: no cover - proteção
    raise RuntimeError(
        "Testes só correm na stack efémera (make test): ENVIRONMENT=test não definido. "
        "Isto evita apontar a suíte para uma base de dados real."
    )

from sqlalchemy import text  # noqa: E402

from app.core.cache import redis_client  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.core.db import engine, run_migrations, session_scope  # noqa: E402

_TABLES = (# multi-site (E4): o CASCADE leva jobs, traduções, eventos, fontes e ideias
           "sites", "daily_digest", "approval_batches")
_SEED = pathlib.Path(settings.migrations_dir) / "002_seed.sql"
_AI_SEED = pathlib.Path(settings.migrations_dir) / "005_ai_seed.sql"


@pytest.fixture(scope="session", autouse=True)
def _schema():
    """Esquema aplicado uma vez por sessão, pelas migrações reais."""
    run_migrations()
    yield


@pytest.fixture(autouse=True)
def _clean_state():
    """Cada teste começa com tabelas vazias, settings semeados e Redis limpo."""
    with engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(_TABLES)} RESTART IDENTITY CASCADE"))
        conn.execute(text("DELETE FROM settings"))
        conn.connection.cursor().execute(_SEED.read_text(encoding="utf-8"))
        # Provedores de IA: volta sempre ao seed (claude, codex, agy).
        # (o CASCADE leva também ai_role_chain e ai_provider_health)
        conn.execute(text("TRUNCATE ai_providers RESTART IDENTITY CASCADE"))
        conn.connection.cursor().execute(_AI_SEED.read_text(encoding="utf-8"))
        conn.execute(text("TRUNCATE provider_usage, ai_site_budgets RESTART IDENTITY"))
        conn.execute(text("TRUNCATE users, audit_log RESTART IDENTITY"))
        conn.execute(text("TRUNCATE system_secrets"))
    redis_client.flushdb()
    from app.services import terms, vault
    terms.invalidate()
    vault.invalidate()
    yield


_REAL_LOAD_ALL = None


@pytest.fixture(autouse=True)
def _sites_baseline(monkeypatch):
    """Os testes partem de "todos os blogs desligados, 5 slots por dia", como o seed original,
    sem depender do estado do piloto em `content-config/` (E14: o `cloud` está ligado, 1 por dia).
    O teste `test_sites_reais_refletem_o_piloto` lê os ficheiros de verdade."""
    global _REAL_LOAD_ALL
    from app.services import sites

    _REAL_LOAD_ALL = _REAL_LOAD_ALL or sites.load_all
    real = _REAL_LOAD_ALL

    def load_all(directory=None):
        loaded = real(directory)
        if directory is None:
            return [s.model_copy(update={"enabled": False, "daily_target": 5}) for s in loaded]
        return loaded

    monkeypatch.setattr(sites, "load_all", load_all)
    yield


@pytest.fixture(autouse=True)
def _no_illustration(monkeypatch):
    """A ilustração (E7) só corre nos testes que a pedem: faz pedidos a bancos de imagens."""
    monkeypatch.setattr(settings, "illustration_enabled", False)


@pytest.fixture(autouse=True)
def _no_host_spacing(monkeypatch):
    """O espaçamento por servidor (R42) só corre nos testes que o pedem: faria a suíte dormir."""
    monkeypatch.setattr(settings, "fetch_host_interval_s", 0)


@pytest.fixture(autouse=True)
def _media_dir(tmp_path, monkeypatch):
    media = tmp_path / "media"
    monkeypatch.setattr(settings, "media_dir", str(media))
    return media


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client


TEST_PASSWORD = "senha-forte-de-teste-123"


def make_user(email: str = "euclides@example.com", role: str = "admin", *,
              password: str = TEST_PASSWORD, must_change: bool = False,
              active: bool = True) -> int:
    """E18.1: usuário do portal direto no banco (o hash é o real, argon2id)."""
    from app.services import users

    with session_scope() as db:
        user_id = users.create(db, email=email, name="", role=role, password=password,
                               actor="teste", must_change=must_change)
        if not active:
            db.execute(text("UPDATE users SET active = false WHERE id = :i"), {"i": user_id})
    return user_id


def login_as(client, email: str = "euclides@example.com", role: str = "admin", **kw):
    """Cria (se preciso) o usuário e põe o cookie de sessão dele no cliente."""
    from app.core.panel_auth import SESSION_COOKIE, make_session

    row = fetch("SELECT id, session_version FROM users WHERE email = :e", e=email)
    if row:
        user_id, version = row[0]["id"], row[0]["session_version"]
    else:
        user_id, version = make_user(email, role, **kw), 1
    client.cookies.set(SESSION_COOKIE, make_session(user_id, version))
    return client


@pytest.fixture
def panel_client(client):
    """Cliente com sessão de um admin (pedido interno, sem Access)."""
    return login_as(client)


@pytest.fixture
def auth() -> dict:
    return {"Authorization": f"Bearer {settings.sb_api_token}"}


def fetch(sql: str, **params):
    with session_scope() as db:
        return db.execute(text(sql), params).mappings().all()


def advance(job_id: int, *states: str, **kwargs) -> None:
    """Leva um job pelos estados dados, cumprindo as regras da máquina de estados:
    lease ao entrar em produção ou publicação, aprovação registada antes de APPROVED."""
    from datetime import UTC, datetime, timedelta

    from app.services import jobs

    with session_scope() as db:
        for state in states:
            extra = dict(kwargs)
            if state in ("CLAIMED", "PUBLISHING"):
                extra.setdefault("lease_owner", "teste")
                extra.setdefault("lease_until_utc", datetime.now(UTC) + timedelta(hours=1))
            if state == "APPROVED":
                db.execute(
                    text("INSERT INTO approvals (job_id, decision, decided_by, channel) "
                         "VALUES (:j, 'approved', 'euclides@example.com', 'panel')"),
                    {"j": job_id},
                )
            jobs.transition(db, job_id, state, **extra)
