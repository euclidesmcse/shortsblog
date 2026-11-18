"""E2.8 — papel da aplicação com privilégios mínimos (spec §11.1)."""
from __future__ import annotations

import logging

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError, ProgrammingError

from app.core import migrate
from app.core.config import settings
from app.core.db import engine

ROLE = "sb_app_test"


@pytest.fixture
def app_role():
    migrate.ensure_app_role(ROLE, "senha-um")
    engines = []

    def connect(password: str = "senha-um"):
        url = make_url(settings.database_url).set(username=ROLE, password=password)
        eng = create_engine(url)
        engines.append(eng)
        return eng

    yield connect
    for eng in engines:
        eng.dispose()
    with engine.begin() as conn:
        conn.execute(text(f"DROP OWNED BY {ROLE}"))
        conn.execute(text(f"DROP ROLE IF EXISTS {ROLE}"))


def test_app_faz_dml(app_role):
    with app_role().begin() as conn:
        conn.execute(text("INSERT INTO settings (key, value) VALUES ('teste', '1')"))
        conn.execute(text("UPDATE settings SET value = '2' WHERE key = 'teste'"))
        assert conn.execute(text("SELECT value FROM settings WHERE key = 'teste'")
                            ).scalar_one() == 2
        conn.execute(text("DELETE FROM settings WHERE key = 'teste'"))
        conn.execute(text("SELECT * FROM schema_migrations")).all()  # leitura permitida


@pytest.mark.parametrize("statement", [
    "DROP TABLE settings",
    "TRUNCATE settings",
    "CREATE TABLE intrusa (id int)",
    "ALTER TABLE settings ADD COLUMN x int",
    "INSERT INTO schema_migrations (filename, checksum) VALUES ('x', 'y')",
    "DELETE FROM schema_migrations",
    "CREATE ROLE outro",
])
def test_app_nao_faz_ddl_nem_mexe_nas_migracoes(app_role, statement):
    eng = app_role()
    with pytest.raises(ProgrammingError, match="permission denied|must be owner"), \
            eng.begin() as conn:
        conn.execute(text(statement))


def test_tabelas_futuras_herdam_os_privilegios(app_role):
    eng = app_role()
    with engine.begin() as conn:  # migração futura, feita pelo admin
        conn.execute(text("CREATE TABLE futura (id serial PRIMARY KEY, v text)"))
    try:
        with eng.begin() as conn:
            conn.execute(text("INSERT INTO futura (v) VALUES ('ok')"))
            assert conn.execute(text("SELECT v FROM futura")).scalar_one() == "ok"
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE futura"))


def test_idempotente_e_troca_a_senha(app_role):
    migrate.ensure_app_role(ROLE, "senha-dois")
    with app_role("senha-dois").connect() as conn:
        assert conn.execute(text("SELECT current_user")).scalar_one() == ROLE
    with pytest.raises(OperationalError):
        app_role("senha-um").connect()
    with engine.connect() as conn:
        flags = conn.execute(text("SELECT rolsuper, rolcreatedb, rolcreaterole "
                                  "FROM pg_roles WHERE rolname = :r"), {"r": ROLE}).one()
    assert tuple(flags) == (False, False, False)


def test_nome_de_papel_invalido():
    with pytest.raises(ValueError):
        migrate.ensure_app_role("x; DROP TABLE settings", "p")


def test_main_com_e_sem_senha(monkeypatch, caplog):
    called = []
    # setup_logging() troca os handlers da raiz e apagaria o do caplog.
    monkeypatch.setattr(migrate, "setup_logging", lambda: None)
    monkeypatch.setattr(migrate, "ensure_app_role", lambda r, p: called.append((r, p)))
    monkeypatch.setenv("SB_DB_APP_PASSWORD", "segredo")
    monkeypatch.setenv("SB_DB_APP_USER", "sb_app")
    assert migrate.main() == 0
    assert called == [("sb_app", "segredo")]

    monkeypatch.delenv("SB_DB_APP_PASSWORD")
    with caplog.at_level(logging.WARNING):
        assert migrate.main() == 0
    assert len(called) == 1
    assert any("SB_DB_APP_PASSWORD vazio" in r.getMessage() for r in caplog.records)


def test_api_nao_migra_no_arranque_se_desligado(monkeypatch):
    from fastapi.testclient import TestClient

    from app import main

    calls = []
    monkeypatch.setattr(main, "run_migrations", lambda: calls.append(1) or [])
    monkeypatch.setattr(settings, "run_migrations_on_startup", False)
    with TestClient(main.app):
        pass
    assert calls == []
    monkeypatch.setattr(settings, "run_migrations_on_startup", True)
    with TestClient(main.app):
        pass
    assert calls == [1]


def test_app_nao_altera_sites_nem_reescreve_o_historico(app_role):
    # Revisão E4 #6: `sites` define o destino de publicação; eventos e aprovações só se acrescentam.
    from app.core.db import session_scope
    from app.services import sites

    with session_scope() as db:
        sites.sync(db)
        job = db.execute(text(
            """INSERT INTO publication_jobs
                 (site_key, local_date, slot_number, due_at_utc, produce_after_utc)
               VALUES ('cloud', '2026-10-05', 1, now(), now()) RETURNING id""")).scalar_one()
    migrate.ensure_app_role(ROLE, "senha-um")   # volta a aplicar os privilégios às tabelas novas
    eng = app_role()
    with eng.begin() as conn:                   # o que a app precisa continua a funcionar
        assert conn.execute(text("SELECT count(*) FROM sites")).scalar() == 6
        conn.execute(text("INSERT INTO publication_events (job_id, event_type) "
                          "VALUES (:j, 'teste')"), {"j": job})
        conn.execute(text("INSERT INTO approvals (job_id, decision, decided_by, channel) "
                          "VALUES (:j, 'approved', 'euclides', 'panel')"), {"j": job})
        conn.execute(text("UPDATE publication_jobs SET status = 'QUEUED' WHERE id = :j"),
                     {"j": job})
        # E18 (revisão intermediária #1): o portal edita as colunas editoriais/operacionais.
        conn.execute(text("UPDATE sites SET enabled = true, daily_target = 2, "
                          "config = config WHERE site_key = 'cloud'"))
    forbidden = [
        "UPDATE sites SET wordpress_api_url = 'https://atacante.test/wp-json'",
        "UPDATE sites SET base_url = 'https://atacante.test'",
        "UPDATE sites SET timezone = 'UTC'",
        "UPDATE audit_log SET actor = 'outro'",
        "DELETE FROM audit_log",
        "DELETE FROM sites",
        "INSERT INTO sites (site_key, name, base_url, wordpress_api_url) "
        "VALUES ('novo', 'n', 'https://n.test', 'https://n.test/wp-json')",
        "UPDATE publication_events SET actor = 'outro'",
        "DELETE FROM publication_events",
        "UPDATE approvals SET decision = 'approved'",
        "DELETE FROM approvals",
    ]
    for statement in forbidden:
        with pytest.raises(ProgrammingError, match="permission denied"), eng.begin() as conn:
            conn.execute(text(statement))
