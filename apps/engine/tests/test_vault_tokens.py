"""E18.3b entrega 2 (D-109): tokens com carência de 24 h e chaves de IA na engrenagem."""
from __future__ import annotations

import asyncio
import re
from urllib.parse import unquote_plus

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.core import security, vault_cli
from app.core.config import settings
from app.core.db import session_scope
from app.services import vault
from tests.conftest import fetch, login_as

OLD = "token-antigo-do-ambiente-0001"


def location(response) -> str:
    return unquote_plus(response.headers["location"])


def token_version(page: str) -> str:
    block = page.split('id="tokens"', 1)[1]
    return re.search(r'name="version" value="([0-9a-f]+)"', block).group(1)


def rotate(key: str) -> str:
    with session_scope() as db:
        token = vault.rotate_token(db, key, "teste")
    return token


def api_call(authorization: str) -> int:
    try:
        asyncio.run(security.require_api_token(authorization))
    except HTTPException as exc:
        return exc.status_code
    return 200


@pytest.fixture
def env_token(monkeypatch):
    monkeypatch.setattr(settings, "sb_api_token", OLD)


# --- Serviço ---------------------------------------------------------------------
def test_troca_aceita_o_novo_e_o_anterior_durante_a_carencia(env_token):
    assert api_call(f"Bearer {OLD}") == 200
    new = rotate("sb_api_token")
    assert new != OLD and len(new) >= 40
    assert api_call(f"Bearer {new}") == 200
    assert api_call(f"Bearer {OLD}") == 200            # carência
    assert api_call("Bearer outro") == 401
    assert api_call(OLD) == 401                         # sem o prefixo Bearer
    with session_scope() as db:                          # carência vencida
        db.execute(text("UPDATE system_secrets SET previous_until = now() - interval '1 min'"))
    vault.invalidate()
    assert api_call(f"Bearer {OLD}") == 401
    assert api_call(f"Bearer {new}") == 200


def test_encerrar_carencia(env_token):
    new = rotate("sb_api_token")
    with session_scope() as db:
        assert vault.end_grace(db, "sb_api_token", "teste")
    assert not vault.token_matches("sb_api_token", OLD)
    assert vault.token_matches("sb_api_token", new)
    with session_scope() as db:
        assert not vault.end_grace(db, "sb_api_token", "teste")


def test_diz_qual_token_foi_apresentado(env_token):
    """R61: só a chave NOVA prova que o outro lado já passou ao novo e a carência pode acabar."""
    assert vault.token_match("sb_api_token", OLD) == "current"      # do ambiente, sem troca
    new = rotate("sb_api_token")
    assert vault.token_match("sb_api_token", new) == "current"
    assert vault.token_match("sb_api_token", OLD) == "previous"
    assert vault.token_match("sb_api_token", "outro") == ""
    assert vault.token_match("sb_api_token", "") == ""
    with session_scope() as db:
        vault.end_grace(db, "sb_api_token", "teste")
    assert vault.token_match("sb_api_token", OLD) == ""


def test_segunda_troca_mantem_so_o_imediatamente_anterior(env_token):
    first = rotate("sb_api_token")
    second = rotate("sb_api_token")
    assert vault.token_matches("sb_api_token", first)
    assert not vault.token_matches("sb_api_token", OLD)
    assert vault.token_matches("sb_api_token", second)


def test_sem_token_anterior_nao_ha_carencia(monkeypatch):
    monkeypatch.setattr(settings, "pauta_webhook_token", "")
    rotate("pauta_webhook_token")
    row = fetch("SELECT previous_enc, previous_until FROM system_secrets")[0]
    assert row["previous_enc"] is None and row["previous_until"] is None


def test_auditoria_e_banco_sem_o_token(env_token):
    new = rotate("sb_api_token")
    dump = str(fetch("SELECT * FROM audit_log")) + str(fetch("SELECT * FROM system_secrets"))
    assert new not in dump and OLD not in dump
    assert fetch("SELECT action FROM audit_log")[0]["action"] == "system.token.rotate"


def test_so_tokens_sao_trocados():
    with session_scope() as db, pytest.raises(vault.VaultError, match="não é um token"):
        vault.rotate_token(db, "smtp_password", "teste")


@pytest.mark.parametrize("key,setting", [("newsletter_webhook_token", "newsletter_webhook_token"),
                                         ("pauta_webhook_token", "pauta_webhook_token"),
                                         ("newsletter_api_token", "newsletter_api_token")])
def test_rotas_de_webhook_aceitam_o_token_do_cofre(client, monkeypatch, key, setting):
    monkeypatch.setattr(settings, setting, OLD)
    new = rotate(key)
    if key == "newsletter_api_token":
        def call(tok):
            return client.post("/api/newsletter/unsubscribe", json={"token": "x"},
                               headers={"Authorization": f"Bearer {tok}"}).status_code
    else:
        path = ("/api/newsletter/brevo-event" if key == "newsletter_webhook_token"
                else "/api/pauta/brevo-inbound")
        monkeypatch.setattr(settings, "newsletter_api_token", "relay-do-wp-0001")
        body = ({"key": None, "event": "hard_bounce", "email": "a@example.test"}
                if key == "newsletter_webhook_token" else {"key": None, "sender": "a@b.c"})

        def call(tok):
            return client.post(path, json={**body, "key": tok},
                               headers={"Authorization": "Bearer relay-do-wp-0001"}).status_code
    assert call("errado") == 401
    assert call(new) != 401 and call(OLD) != 401


# --- Portal ----------------------------------------------------------------------
def test_portal_gera_mostra_uma_vez_e_lista_onde_atualizar(panel_client, env_token):
    page = panel_client.get("/dashboard/system").text
    assert "Tokens de webhook" in page and "n8n" in page
    v = token_version(page)
    refused = panel_client.post("/dashboard/system/token/sb_api_token/rotate",
                                data={"version": v}, follow_redirects=False)
    assert "marque a confirmação" in location(refused)
    shown = panel_client.post("/dashboard/system/token/sb_api_token/rotate",
                              data={"version": v, "confirm": "1"})
    assert shown.status_code == 200 and shown.headers["cache-control"] == "no-store"
    token = re.search(r'aria-label="token novo"|readonly value="([^"]+)"', shown.text)
    new = re.search(r'readonly value="([^"]+)"', shown.text).group(1)
    assert token and vault.token_matches("sb_api_token", new)
    assert "Copie agora" in shown.text and "Header Auth" in shown.text
    again = panel_client.get("/dashboard/system").text
    assert new not in again and "anterior vale até" in again
    # reenviar o mesmo formulário (F5) não troca de novo: a versão mudou
    stale = panel_client.post("/dashboard/system/token/sb_api_token/rotate",
                              data={"version": v, "confirm": "1"}, follow_redirects=False)
    assert "mudou desde que a tela foi aberta" in location(stale)
    assert vault.token_matches("sb_api_token", new)


def test_portal_encerra_a_carencia(panel_client, env_token):
    rotate("sb_api_token")
    page = panel_client.get("/dashboard/system").text
    response = panel_client.post("/dashboard/system/token/sb_api_token/end-grace",
                                 data={"version": token_version(page)}, follow_redirects=False)
    assert "deixou de valer" in location(response)
    assert not vault.token_matches("sb_api_token", OLD)


def test_operador_nao_troca_token(client, env_token):
    login_as(client, "op@example.com", "operador")
    response = client.post("/dashboard/system/token/sb_api_token/rotate",
                           data={"confirm": "1"}, follow_redirects=False)
    assert response.status_code == 403
    assert vault.effective("sb_api_token") == (OLD, "ambiente")


def test_token_desconhecido_ou_nao_token(panel_client):
    for key in ("nao_existe", "smtp_password"):
        response = panel_client.post(f"/dashboard/system/token/{key}/rotate",
                                     data={"confirm": "1"}, follow_redirects=False)
        assert "Token desconhecido" in location(response)


# --- Chaves de IA e detalhes -----------------------------------------------------
def test_resumo_das_chaves_de_ia_sem_valor(panel_client):
    from app.services.ai import secrets as ai_secrets

    with session_scope() as db:
        db.execute(text("INSERT INTO ai_providers (name, kind, family, model, base_url, "
                        "api_key_enc, enabled) VALUES ('deepx', 'api', 'deepx', 'm1', "
                        "'https://api.deepx.test', :k, false)"),
                   {"k": ai_secrets.encrypt("sk-chave-secreta-7777")})
    page = panel_client.get("/dashboard/system").text
    assert "Chaves e cota diária de IA" in page and "assinatura" in page
    assert "••••7777" in page and "sk-chave-secreta" not in page


def test_chave_de_ia_vai_para_a_auditoria_sem_valor(panel_client):
    panel_client.post("/dashboard/ai/api-providers", data={
        "name": "deepx", "family": "deepx", "base_url": "https://api.deepx.test",
        "model": "m1", "api_key": "sk-chave-secreta-7777"})
    rows = fetch("SELECT action, target, details FROM audit_log WHERE action LIKE 'ai.%'")
    assert rows and rows[0]["action"] == "ai.provider.save" and rows[0]["target"] == "ai:deepx"
    assert rows[0]["details"]["key_changed"] is True
    assert "sk-chave-secreta" not in str(rows)


def test_senha_smtp_vazia_com_chave_smtp_do_brevo(panel_client, monkeypatch):
    monkeypatch.setattr(settings, "smtp_password", "")
    monkeypatch.setattr(settings, "brevo_api_key", "xsmtpsib-chave-smtp-0000")
    assert vault.describe(vault.item("smtp_password"))["source"] == "brevo"
    assert "usa a chave SMTP do Brevo" in panel_client.get("/dashboard/system").text


def test_cli_status_e_import_de_token(env_token, capsys):
    assert vault_cli.main(["import-env", "sb_api_token", "--apply"]) == 0
    assert vault.effective("sb_api_token") == (OLD, "banco")
    rotate("sb_api_token")
    assert vault_cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "sb_api_token" in out and "anterior vale até" in out and OLD not in out
