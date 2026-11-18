"""E18.1 (D-104): usuários do portal, bloqueio, papéis, bootstrap e auditoria."""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.core import panel_auth
from app.core.config import settings
from app.core.db import session_scope
from app.services import users
from tests.conftest import TEST_PASSWORD, fetch, login_as, make_user

EMAIL = "euclides@example.com"


@pytest.fixture(autouse=True)
def _login_settings(monkeypatch):
    monkeypatch.setattr(settings, "panel_password", "senha-antiga-do-painel")
    monkeypatch.setattr(settings, "panel_login_max_failures", 3)
    monkeypatch.setattr(settings, "panel_login_ip_limit", 50)


def _login(client, email=EMAIL, password=TEST_PASSWORD, **kw):
    return client.post("/login", data={"email": email, "password": password, **kw},
                       follow_redirects=False)


# --- hash e regras de senha ------------------------------------------------------
def test_hash_e_argon2id_e_confere():
    digest = users.hash_password("uma senha bem longa 42")
    assert digest.startswith("$argon2id$")
    assert users.verify_password(digest, "uma senha bem longa 42")
    assert not users.verify_password(digest, "outra")
    assert not users.verify_password("lixo", "qualquer")


@pytest.mark.parametrize("bad", ["curta", "aaaaaaaaaaaaaaaa", "euclides-2026-senha"])
def test_senhas_fracas_sao_recusadas(bad):
    with pytest.raises(users.UserError):
        users.check_strength(bad, EMAIL)


# --- login, bloqueio e mensagem neutra ---------------------------------------------
def test_login_com_email_e_senha(client):
    make_user(EMAIL)
    response = _login(client, next="/dashboard/review")
    assert response.status_code == 303 and response.headers["location"] == "/dashboard/review"
    assert client.get("/", follow_redirects=False).status_code == 200
    assert fetch("SELECT last_login_at FROM users")[0]["last_login_at"] is not None


def test_mesma_mensagem_para_email_desconhecido_e_senha_errada(client):
    make_user(EMAIL)
    unknown = _login(client, email="ninguem@example.com")
    wrong = _login(client, password="senha-errada-123")
    assert unknown.status_code == wrong.status_code == 401
    assert users.FAILED in unknown.text and users.FAILED in wrong.text


def test_erros_seguidos_bloqueiam_o_usuario(client):
    make_user(EMAIL)
    for _ in range(3):
        assert _login(client, password="senha-errada-123").status_code == 401
    # bloqueado: nem a senha certa entra, e a mensagem é a mesma
    blocked = _login(client)
    assert blocked.status_code == 401 and users.FAILED in blocked.text
    assert fetch("SELECT locked_until FROM users")[0]["locked_until"] is not None


def test_bloqueio_vencido_recomeca_a_contagem(client):
    """Revisão E18: depois dos 15 min, 1 erro só não pode bloquear de novo."""
    make_user(EMAIL)
    for _ in range(3):
        _login(client, password="senha-errada-123")
    with session_scope() as db:                           # o bloqueio venceu
        db.execute(text("UPDATE users SET locked_until = now() - interval '1 minute'"))
    assert _login(client, password="senha-errada-123").status_code == 401
    row = fetch("SELECT failed_logins, locked_until FROM users")[0]
    assert row["failed_logins"] == 1 and row["locked_until"] is None
    assert _login(client).status_code == 303                # a senha certa entra
    # e os erros seguidos voltam a bloquear normalmente
    for _ in range(3):
        _login(client, password="senha-errada-123")
    assert fetch("SELECT locked_until FROM users")[0]["locked_until"] is not None


def test_acerto_zera_o_contador(client):
    make_user(EMAIL)
    _login(client, password="senha-errada-123")
    _login(client, password="senha-errada-123")
    assert _login(client).status_code == 303
    assert fetch("SELECT failed_logins FROM users")[0]["failed_logins"] == 0


def test_limite_por_ip(client, monkeypatch):
    monkeypatch.setattr(settings, "panel_login_ip_limit", 2)
    make_user(EMAIL)
    _login(client, email="a@example.com")
    _login(client, email="b@example.com")
    assert _login(client).status_code == 401  # o IP estourou: nem o certo entra


def test_usuario_desativado_nao_entra(client):
    make_user(EMAIL, active=False)
    assert _login(client).status_code == 401


# --- bootstrap: o PANEL_PASSWORD cria o 1.º admin ----------------------------------
def test_primeiro_acesso_cria_o_admin_e_obriga_a_trocar(client):
    page = client.get("/login")
    assert "Primeiro acesso" in page.text
    response = _login(client, password="senha-antiga-do-painel")
    assert response.status_code == 303
    assert response.headers["location"] == "/conta/senha?forced=1"
    row = fetch("SELECT email, role, must_change_password, last_login_at FROM users")[0]
    assert (row["email"], row["role"], row["must_change_password"]) == (EMAIL, "admin", True)
    assert row["last_login_at"] is not None  # o 1.º acesso também é um login
    # com a troca pendente, o resto do painel manda para a troca
    blocked = client.get("/", follow_redirects=False)
    assert blocked.headers["location"] == "/conta/senha?forced=1"
    changed = client.post("/conta/senha", follow_redirects=False, data={
        "current": "senha-antiga-do-painel", "new": "minha-senha-nova-forte",
        "confirm": "minha-senha-nova-forte"})
    assert changed.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 200


def test_depois_do_primeiro_admin_o_panel_password_nao_vale(client):
    make_user(EMAIL)
    assert _login(client, password="senha-antiga-do-painel").status_code == 401
    assert fetch("SELECT count(*) AS n FROM users")[0]["n"] == 1


def test_bootstrap_com_senha_errada_nao_cria_nada(client):
    assert _login(client, password="senha-errada-chutada").status_code == 401
    assert fetch("SELECT count(*) AS n FROM users")[0]["n"] == 0


# --- sessão revogável ----------------------------------------------------------------
def test_trocar_a_senha_derruba_as_outras_sessoes(client):
    from fastapi.testclient import TestClient

    from app.main import app

    make_user(EMAIL)
    other = login_as(TestClient(app), EMAIL)
    assert other.get("/", follow_redirects=False).status_code == 200
    login_as(client, EMAIL)
    client.post("/conta/senha", data={"current": TEST_PASSWORD, "new": "outra-senha-bem-forte",
                                      "confirm": "outra-senha-bem-forte"})
    assert client.get("/", follow_redirects=False).status_code == 200   # esta continua
    assert other.get("/", follow_redirects=False).status_code == 303    # a outra caiu


def test_troca_de_senha_valida_a_atual_e_a_confirmacao(panel_client):
    wrong = panel_client.post("/conta/senha", data={
        "current": "errada", "new": "outra-senha-bem-forte", "confirm": "outra-senha-bem-forte"})
    assert wrong.status_code == 400 and "não confere" in wrong.text
    mismatch = panel_client.post("/conta/senha", data={
        "current": TEST_PASSWORD, "new": "outra-senha-bem-forte", "confirm": "diferente-1234567"})
    assert mismatch.status_code == 400


# --- papéis ---------------------------------------------------------------------------
@pytest.mark.parametrize("path", ["/dashboard/ai", "/dashboard/users", "/docs"])
def test_operador_nao_entra_nas_areas_de_admin(client, path):
    login_as(client, "op@example.com", "operador")
    assert client.get(path, follow_redirects=False).status_code == 403


def test_operador_aprova_e_ve_o_painel(client):
    login_as(client, "op@example.com", "operador")
    assert client.get("/dashboard/review").status_code == 200
    home = client.get("/")
    assert home.status_code == 200 and "/dashboard/users" not in home.text


def test_admin_ve_o_link_de_usuarios(panel_client):
    assert "/dashboard/users" in panel_client.get("/").text


# --- gestão de usuários (admin) ---------------------------------------------------------
def test_admin_cria_usuario_e_a_senha_aparece_so_uma_vez(panel_client):
    response = panel_client.post("/dashboard/users", follow_redirects=False,
                                 data={"email": "Nova@Example.com", "role": "operador"})
    assert response.status_code == 200 and "Senha provisória de nova@example.com" in response.text
    row = fetch("SELECT role, must_change_password FROM users WHERE email = 'nova@example.com'")[0]
    assert row["role"] == "operador" and row["must_change_password"] is True
    # a senha não fica na auditoria nem reaparece na página
    assert "password" not in str(fetch("SELECT details FROM audit_log"))
    assert "Senha provisória" not in panel_client.get("/dashboard/users").text


def test_email_repetido_e_recusado(panel_client):
    response = panel_client.post("/dashboard/users", data={"email": EMAIL, "role": "admin"})
    assert "Já existe" in response.text


def test_redefinir_senha_derruba_a_sessao_do_usuario(panel_client):
    from fastapi.testclient import TestClient

    from app.main import app

    victim = login_as(TestClient(app), "op@example.com", "operador")
    user_id = fetch("SELECT id FROM users WHERE email = 'op@example.com'")[0]["id"]
    response = panel_client.post(f"/dashboard/users/{user_id}/reset")
    assert "Senha provisória de op@example.com" in response.text
    assert victim.get("/", follow_redirects=False).status_code == 303


def test_desativar_derruba_a_sessao(panel_client):
    from fastapi.testclient import TestClient

    from app.main import app

    victim = login_as(TestClient(app), "op@example.com", "operador")
    user_id = fetch("SELECT id FROM users WHERE email = 'op@example.com'")[0]["id"]
    panel_client.post(f"/dashboard/users/{user_id}/active", data={"active": "false"})
    assert victim.get("/", follow_redirects=False).status_code == 303
    assert fetch("SELECT active FROM users WHERE id = :i", i=user_id)[0]["active"] is False


def test_admin_nao_se_desativa_nem_se_rebaixa(panel_client):
    me = fetch("SELECT id FROM users WHERE email = :e", e=EMAIL)[0]["id"]
    off = panel_client.post(f"/dashboard/users/{me}/active", data={"active": "false"})
    down = panel_client.post(f"/dashboard/users/{me}/role", data={"role": "operador"})
    assert "própria conta" in off.text and "próprio papel" in down.text


def test_ultimo_admin_nao_pode_ser_desativado():
    admin = make_user(EMAIL)
    with session_scope() as db, pytest.raises(users.UserError):
        users.set_active(db, admin, False, "teste")


def test_mudar_papel_fica_na_auditoria(panel_client):
    user_id = make_user("op@example.com", "operador")
    panel_client.post(f"/dashboard/users/{user_id}/role", data={"role": "admin"})
    row = fetch("SELECT actor, action, target, details FROM audit_log "
                "WHERE action = 'user.role'")[0]
    assert (row["actor"], row["target"]) == (EMAIL, "op@example.com")
    assert row["details"] == {"from": "operador", "to": "admin"}


# --- Access opcional (pacote da E19) ------------------------------------------------------
def test_sem_access_exigido_o_dominio_publico_usa_so_o_login(client, monkeypatch):
    monkeypatch.setattr(settings, "sb_host", "painel.exemplo.com")
    monkeypatch.setattr(settings, "panel_require_access", True)
    login_as(client)
    headers = {"host": "painel.exemplo.com"}
    assert client.get("/", headers=headers).status_code == 403
    monkeypatch.setattr(settings, "panel_require_access", False)
    assert client.get("/", headers=headers).status_code == 200


def test_client_ip_so_confia_no_cf_quando_o_access_validou():
    from types import SimpleNamespace

    def req(access, cf):
        headers = {"cf-connecting-ip": cf} if cf else {}
        return SimpleNamespace(state=SimpleNamespace(access_email=access), headers=headers,
                               client=SimpleNamespace(host="10.0.0.9"))

    assert panel_auth.client_ip(req(EMAIL, "203.0.113.7")) == "203.0.113.7"
    assert panel_auth.client_ip(req(None, "203.0.113.7")) == "10.0.0.9"


def test_senha_provisoria_sempre_passa_na_validacao():
    """Email curto (`n@…`): a senha sorteada nunca pode conter o nome do email."""
    for _ in range(300):
        users.check_strength(users.temporary_password("n@example.com"), "n@example.com")
