"""Correções da revisão intermediária da E18 (docs/reviews/E18-review-parcial.md)."""
from __future__ import annotations

import re
from urllib.parse import unquote_plus

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.core import migrate, reimport
from app.core.config import settings
from app.core.db import engine, session_scope
from app.services import editorial
from tests.conftest import fetch, login_as
from tests.test_editorial import legacy_sync, plan, site_form

ROLE = "sb_app_e18"


@pytest.fixture
def app_db():
    """Sessão com o PAPEL DA APP (o que a API usa em produção), não com o dono do banco."""
    migrate.ensure_app_role(ROLE, "senha-e18")
    url = make_url(settings.database_url).set(username=ROLE, password="senha-e18")
    eng = create_engine(url)
    with Session(eng) as db:
        yield db
    eng.dispose()
    with engine.begin() as conn:
        conn.execute(text(f"DROP OWNED BY {ROLE}"))
        conn.execute(text(f"DROP ROLE IF EXISTS {ROLE}"))


def location(response) -> str:
    return unquote_plus(response.headers["location"])


# --- #1 bloqueante: as escritas do portal funcionam com o papel da app ----------------------------
def test_portal_edita_blog_politicas_e_desfaz_com_o_papel_da_app(app_db):
    legacy_sync()
    migrate.ensure_app_role(ROLE, "senha-e18")       # privilégios nas tabelas da sincronização
    form = {"name": "Saúde (teste)", "daily_target": 2, "description": "Descrição nova."}
    assert editorial.edit_site(app_db, "saude", form, "teste")
    app_db.commit()
    entry = app_db.execute(text("SELECT id FROM audit_log WHERE target = 'site:saude' "
                                "AND action = 'config.edit'")).scalar_one()
    editorial.undo(app_db, entry, "teste")
    app_db.commit()
    assert fetch("SELECT name FROM sites WHERE site_key = 'saude'")[0]["name"] == "Saúde"
    assert editorial.edit_policy_own(app_db, "cloud", "Só nuvem.", "teste")
    assert editorial.edit_policy_common(app_db, "Comum novo.", "teste")
    app_db.commit()
    common_entry = app_db.execute(text("SELECT id FROM audit_log "
                                       "WHERE target = 'policy_common:comum'")).scalar_one()
    editorial.undo(app_db, common_entry, "teste")
    app_db.commit()
    assert fetch("SELECT count(*) AS n FROM audit_log WHERE action = 'config.undo'")[0]["n"] >= 7


def test_portal_nao_mexe_em_colunas_tecnicas(app_db):
    legacy_sync()
    migrate.ensure_app_role(ROLE, "senha-e18")
    before = editorial.site_values(app_db, "saude")
    after = {**before, "base_url": "https://atacante.test"}
    with pytest.raises(editorial.EditError, match="técnicas"):
        editorial._save_site(app_db, before, after, "teste")
    app_db.rollback()


def test_importacao_pela_tela_recusa_tecnica_e_aceita_o_resto(panel_client):
    legacy_sync()
    with session_scope() as db:
        db.execute(text("UPDATE sites SET base_url = 'https://outro.test' WHERE site_key = 'ia'"))
    page = panel_client.get("/dashboard/config").text
    fp = re.search(r'name="fingerprint" value="([0-9a-f]+)"', page).group(1)
    response = panel_client.post("/dashboard/config/import", data={"fingerprint": fp},
                                 follow_redirects=False)
    assert "linha de comando" in location(response)
    assert fetch("SELECT base_url FROM sites WHERE site_key = 'ia'")[0]["base_url"] == \
        "https://outro.test"
    # a linha de comando (admin) aplica
    fingerprint = editorial.fingerprint(plan())
    assert reimport.main(["--apply", f"--fingerprint={fingerprint}"]) == 0
    assert fetch("SELECT base_url FROM sites WHERE site_key = 'ia'")[0]["base_url"] == \
        "https://ia.shortsblog.com"


def test_desfazer_importacao_tecnica_so_pela_linha_de_comando():
    legacy_sync()
    with session_scope() as db:
        db.execute(text("UPDATE sites SET timezone = 'UTC' WHERE site_key = 'ia'"))
    with session_scope() as db:
        editorial.apply(db, plan(), "teste")
    audit_id = fetch("SELECT id FROM audit_log WHERE action = 'config.import'")[0]["id"]
    with session_scope() as db, pytest.raises(editorial.EditError, match="técnicas"):
        editorial.undo(db, audit_id, "teste")


# --- #2 CLI: não sobrescreve o portal sem confirmar ---------------------------------------
def test_cli_exige_confirmacao_para_sobrescrever_o_portal(panel_client, capsys):
    legacy_sync()
    panel_client.post("/dashboard/config/sites/saude", data=site_form(daily_target="2"))
    assert reimport.main([]) == 0
    out = capsys.readouterr().out
    fp = re.search(r"Impressão deste diff: (\w+)", out).group(1)
    assert "SOBRESCREVER=1" in out and "site:saude" in out
    assert reimport.main(["--apply", f"--fingerprint={fp}"]) == 2
    assert fetch("SELECT daily_target FROM sites WHERE site_key = 'saude'")[0]["daily_target"] == 2
    assert reimport.main(["--apply", f"--fingerprint={fp}", "--sobrescrever-portal"]) == 0


# --- #3 logout revoga, #4 auditoria só acrescenta, #6 no-store ----------------------------
def test_sair_revoga_a_sessao_no_servidor(panel_client):
    from fastapi.testclient import TestClient

    from app.core import panel_auth
    from app.main import app

    stolen = TestClient(app)
    stolen.cookies.set(panel_auth.SESSION_COOKIE,
                       panel_client.cookies.get(panel_auth.SESSION_COOKIE))
    assert stolen.get("/", follow_redirects=False).status_code == 200
    panel_client.post("/logout")
    assert stolen.get("/", follow_redirects=False).status_code == 303
    assert fetch("SELECT action FROM audit_log WHERE action = 'user.logout'")


def test_paginas_de_senha_nao_ficam_em_cache(panel_client):
    created = panel_client.post("/dashboard/users", data={"email": "n@example.com",
                                                          "role": "operador"})
    assert "Senha provisória" in created.text and created.headers["cache-control"] == "no-store"
    assert panel_client.get("/conta/senha").headers["cache-control"] == "no-store"


# --- #5 edição simultânea -------------------------------------------------------------------------
def test_formulario_antigo_nao_sobrescreve_mudanca_mais_recente(panel_client):
    legacy_sync()
    page = panel_client.get("/dashboard/config/sites/saude").text
    stale = re.search(r'name="version" value="([0-9a-f]+)"', page).group(1)
    panel_client.post("/dashboard/config/sites/saude", data=site_form(daily_target="2"))
    response = panel_client.post("/dashboard/config/sites/saude", follow_redirects=False,
                                 data=site_form(daily_target="3", version=stale))
    assert "mudou desde que a tela foi aberta" in location(response)
    assert fetch("SELECT daily_target FROM sites WHERE site_key = 'saude'")[0]["daily_target"] == 2


@pytest.mark.parametrize("page_path,post_path,field", [
    ("/dashboard/config/policies", "/dashboard/config/policies/common", "text"),
    ("/dashboard/config/newsletters", "/dashboard/config/newsletters", None),
    ("/dashboard/config/sem-leitura", "/dashboard/config/sem-leitura", None),
])
def test_listas_e_politicas_tambem_recusam_versao_velha(panel_client, page_path, post_path,
                                                        field):
    legacy_sync()
    panel_client.get(page_path)
    data = {"version": "0" * 16}
    if field:
        data[field] = "Outro texto comum."
    elif "newsletters" in post_path:
        data.update(name=["X"], sender_domain=["x.example"], site_key=["ia"], lang=["en"])
    else:
        data.update(action="add", domain="novo.example", reason="teste")
    response = panel_client.post(post_path, data=data, follow_redirects=False)
    assert "mudou desde que a tela foi aberta" in location(response)


def test_operador_nao_ve_versao_de_config(client):
    login_as(client, "op@example.com", "operador")
    assert client.get("/dashboard/config/sites/saude", follow_redirects=False).status_code == 403
