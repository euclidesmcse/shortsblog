"""E18.3b entrega 1 (D-109): cofre do sistema — e-mail e credenciais do WordPress pelo portal."""
from __future__ import annotations

import re
import smtplib
from urllib.parse import unquote_plus

import httpx
import pytest
from sqlalchemy import text

from app.core import vault_cli
from app.core.config import settings
from app.core.db import session_scope
from app.services import integrations, readiness, sitepub, vault
from app.services.ai import secrets as ai_secrets
from app.services.notify import mail
from tests.conftest import fetch, login_as
from tests.test_editorial import legacy_sync

WP_SECRET = "abcd efgh ijkl mnop qrst 9876"
SMTP_SECRET = "senha-smtp-de-teste-4321"


def location(response) -> str:
    return unquote_plus(response.headers["location"])


def put(key: str, value: str) -> None:
    with session_scope() as db:
        vault.set_value(db, key, value, "teste")
    vault.invalidate()


def version_of(page: str, form_action: str) -> str:
    block = page.split(f'action="{form_action}"', 1)[1]
    return re.search(r'name="version" value="([0-9a-f]+)"', block).group(1)


class FakeSMTP:
    """SMTP de mentira: aceita só a senha certa e anota para onde cada teste conectou."""

    hosts: list[str] = []
    contexts: list = []

    def __init__(self, host, port, timeout=0):
        self.host = host
        FakeSMTP.hosts.append(host)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context=None):
        FakeSMTP.contexts.append(context)

    def login(self, user, password):
        if password != SMTP_SECRET:
            raise smtplib.SMTPAuthenticationError(535, b"bad")


@pytest.fixture
def smtp(monkeypatch):
    FakeSMTP.hosts, FakeSMTP.contexts = [], []
    monkeypatch.setattr(integrations, "_smtp_factory", FakeSMTP)
    monkeypatch.setattr(settings, "brevo_api_key", "")
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.test")
    monkeypatch.setattr(settings, "smtp_user", "relay@example.test")
    monkeypatch.setattr(settings, "smtp_password", "")


@pytest.fixture
def wp(monkeypatch):
    """WordPress de mentira: aceita só `sb-publisher` com a WP_SECRET."""
    import base64

    def handler(request: httpx.Request) -> httpx.Response:
        good = "Basic " + base64.b64encode(f"sb-publisher:{WP_SECRET}".encode()).decode()
        if request.headers.get("authorization") != good:
            return httpx.Response(401, json={"code": "invalid"})
        return httpx.Response(200, json={"slug": "sb-publisher", "roles": ["author"]})

    monkeypatch.setattr(sitepub, "_transport", httpx.MockTransport(handler))
    legacy_sync()


# --- Serviço ----------------------------------------------------------------
def test_banco_tem_precedencia_e_fica_cifrado(monkeypatch):
    monkeypatch.setattr(settings, "smtp_password", "do-ambiente")
    assert vault.effective("smtp_password") == ("do-ambiente", "ambiente")
    put("smtp_password", SMTP_SECRET)
    assert vault.effective("smtp_password") == (SMTP_SECRET, "banco")
    row = fetch("SELECT value_enc, last4 FROM system_secrets WHERE key = 'smtp_password'")[0]
    assert SMTP_SECRET not in row["value_enc"] and row["last4"] == "4321"
    with session_scope() as db:
        vault.clear(db, "smtp_password", "teste")
    assert vault.effective("smtp_password") == ("do-ambiente", "ambiente")


def test_auditoria_nunca_leva_o_valor_do_segredo():
    legacy_sync()
    put("smtp_password", SMTP_SECRET)
    put("wp.cloud.password", WP_SECRET)
    put("smtp_host", "smtp.novo.test")
    dump = str(fetch("SELECT details FROM audit_log WHERE action LIKE 'system.%'"))
    assert SMTP_SECRET not in dump and WP_SECRET not in dump and "4321" not in dump
    assert "smtp.novo.test" in dump          # parâmetro não secreto leva antes/depois


def test_valor_ilegivel_cai_no_ambiente(monkeypatch):
    put("mail_from_email", "cofre@example.test")
    monkeypatch.setattr(settings, "mail_from_email", "env@example.test")
    monkeypatch.setattr(settings, "sb_secret_key", "outra-chave-qualquer-com-32-caracteres!")
    vault.invalidate()
    assert vault.effective("mail_from_email") == ("env@example.test", "ambiente")
    assert vault.describe(vault.item("mail_from_email"))["unreadable"]


@pytest.mark.parametrize("key,value,error", [
    ("mail_from_email", "sem-arroba", "e-mail"),
    ("smtp_port", "99999", "número"),
    ("smtp_host", "smtp host", "servidor"),
    ("nao.existe", "x", "desconhecido"),
    ("wp.inexistente.user", "x", "Blog desconhecido"),
])
def test_validacao(key, value, error):
    legacy_sync()
    with session_scope() as db, pytest.raises(vault.VaultError, match=error):
        vault.set_value(db, key, value, "teste")


def test_consumidores_leem_o_cofre(monkeypatch, wp):
    sent = {}

    def fake_post(url, json_body, headers):
        sent["key"] = headers["api-key"]
        return httpx.Response(201, json={"messageId": "m1"})

    monkeypatch.setattr(mail.http, "post_json", fake_post)
    monkeypatch.setattr(settings, "brevo_api_key", "xkeysib-do-ambiente")
    monkeypatch.setattr(settings, "mail_from_email", "env@example.test")
    put("brevo_api_key", "xkeysib-do-cofre")
    assert mail.send_transactional("a@example.test", "", "s", "<p>x</p>") == "m1"
    assert sent["key"] == "xkeysib-do-cofre"
    # WordPress: credencial do cofre, readiness também
    put("wp.cloud.user", "sb-publisher")
    put("wp.cloud.password", WP_SECRET)
    site = fetch("SELECT * FROM sites WHERE site_key = 'cloud'")[0]
    assert sitepub.WordPress(site).call("GET", "/wp/v2/users/me")["slug"] == "sb-publisher"
    with session_scope() as db:
        assert "cloud" not in readiness.wordpress_ready(db)["missing"]


# --- Portal ----------------------------------------------------------------
def test_tela_mostra_origem_e_so_mascara(panel_client, smtp):
    put("smtp_password", SMTP_SECRET)
    page = panel_client.get("/dashboard/system")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert SMTP_SECRET not in page.text and "••••4321" in page.text
    assert "smtp.example.test" in page.text and 'href="/dashboard/system"' in page.text


def test_operador_nao_abre_o_sistema(client):
    login_as(client, "op@example.com", "operador")
    assert client.get("/dashboard/system", follow_redirects=False).status_code == 403
    page = client.get("/").text
    assert 'href="/dashboard/system"' not in page


def test_testar_conexao_nao_grava(panel_client, smtp):
    page = panel_client.get("/dashboard/system").text
    response = panel_client.post("/dashboard/system/email", follow_redirects=False, data={
        "action": "test", "smtp_password": SMTP_SECRET,
        "version": version_of(page, "/dashboard/system/email")})
    assert "conectou com STARTTLS" in location(response)
    assert fetch("SELECT count(*) AS n FROM system_secrets")[0]["n"] == 0


def test_teste_smtp_confere_o_certificado(panel_client, smtp):
    import ssl
    page = panel_client.get("/dashboard/system").text
    panel_client.post("/dashboard/system/email", follow_redirects=False, data={
        "action": "test", "smtp_password": SMTP_SECRET,
        "version": version_of(page, "/dashboard/system/email")})
    context = FakeSMTP.contexts[-1]
    assert context is not None and context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname


@pytest.mark.parametrize("field,value", [("smtp_host", "smtp.atacante.test"),
                                         ("smtp_port", "2525"),
                                         ("smtp_user", "outro@example.test")])
def test_trocar_destino_smtp_exige_redigitar_o_segredo(panel_client, smtp, field, value):
    # Revisão final da E18, achado 4: o segredo guardado não pode ir a um servidor digitado.
    put("smtp_password", SMTP_SECRET)
    page = panel_client.get("/dashboard/system").text
    v = version_of(page, "/dashboard/system/email")
    for action in ("test", "save"):
        response = panel_client.post("/dashboard/system/email", follow_redirects=False, data={
            "action": action, field: value, "smtp_password": "", "version": v})
        assert "digite de novo" in location(response)
    assert FakeSMTP.hosts == []                      # nem chegou a conectar
    assert vault.get(field) != value
    retyped = panel_client.post("/dashboard/system/email", follow_redirects=False, data={
        "action": "test", field: value, "smtp_password": SMTP_SECRET, "version": v})
    assert "digite de novo" not in location(retyped)
    assert FakeSMTP.hosts                            # com o segredo digitado, testa


def test_chave_smtp_do_brevo_tambem_exige_redigitar(panel_client, smtp):
    key = vault.SMTP_KEY_PREFIX + "x" * 40
    put("brevo_api_key", key)
    page = panel_client.get("/dashboard/system").text
    response = panel_client.post("/dashboard/system/email", follow_redirects=False, data={
        "action": "test", "smtp_host": "smtp.atacante.test", "brevo_api_key": "",
        "version": version_of(page, "/dashboard/system/email")})
    assert "digite de novo" in location(response) and "Brevo" in location(response)
    assert FakeSMTP.hosts == []


def test_mudar_so_o_remetente_nao_exige_segredo(panel_client, smtp):
    put("smtp_password", SMTP_SECRET)
    page = panel_client.get("/dashboard/system").text
    response = panel_client.post("/dashboard/system/email", follow_redirects=False, data={
        "action": "save", "mail_from_name": "Outro nome", "smtp_password": "",
        "version": version_of(page, "/dashboard/system/email")})
    assert "Gravado no cofre: mail_from_name" in location(response)


def test_gravar_so_se_o_teste_passar(panel_client, smtp):
    page = panel_client.get("/dashboard/system").text
    v = version_of(page, "/dashboard/system/email")
    bad = panel_client.post("/dashboard/system/email", follow_redirects=False, data={
        "action": "save", "smtp_password": "errada-123456", "version": v})
    assert "Nada gravado" in location(bad)
    assert fetch("SELECT count(*) AS n FROM system_secrets")[0]["n"] == 0
    good = panel_client.post("/dashboard/system/email", follow_redirects=False, data={
        "action": "save", "smtp_password": SMTP_SECRET, "version": v,
        # parâmetro igual ao atual não conta como mudança (não copia o ambiente sem querer)
        "smtp_host": "smtp.example.test", "smtp_password_confirm": ""})
    assert "Gravado no cofre: smtp_password" in location(good)
    assert vault.effective("smtp_password") == (SMTP_SECRET, "banco")
    assert fetch("SELECT key FROM system_secrets") == [{"key": "smtp_password"}]
    assert SMTP_SECRET not in location(good)


def test_forcar_grava_mesmo_com_teste_falho(panel_client, smtp):
    page = panel_client.get("/dashboard/system").text
    response = panel_client.post("/dashboard/system/email", follow_redirects=False, data={
        "action": "save", "smtp_password": "errada-123456", "force": "1",
        "version": version_of(page, "/dashboard/system/email")})
    assert "o teste tinha falhado" in location(response)
    assert vault.get("smtp_password") == "errada-123456"


def test_segredo_em_branco_mantem_e_versao_velha_recusa(panel_client, smtp):
    put("smtp_password", SMTP_SECRET)
    page = panel_client.get("/dashboard/system").text
    stale = version_of(page, "/dashboard/system/email")
    put("mail_from_name", "Outro nome")
    response = panel_client.post("/dashboard/system/email", follow_redirects=False, data={
        "action": "save", "smtp_password": "", "mail_from_name": "Mais um", "version": stale})
    assert "mudou desde que a tela foi aberta" in location(response)
    page = panel_client.get("/dashboard/system").text
    response = panel_client.post("/dashboard/system/email", follow_redirects=False, data={
        "action": "save", "smtp_password": "", "mail_from_name": "Mais um",
        "version": version_of(page, "/dashboard/system/email")})
    assert "mail_from_name" in location(response)
    assert vault.get("smtp_password") == SMTP_SECRET


def test_voltar_ao_ambiente(panel_client, smtp, monkeypatch):
    monkeypatch.setattr(settings, "smtp_password", SMTP_SECRET)
    put("smtp_password", "outra-senha-0000")
    page = panel_client.get("/dashboard/system").text
    assert "Voltar ao ambiente" in page
    response = panel_client.post("/dashboard/system/clear", follow_redirects=False, data={
        "key": "smtp_password", "version": version_of(page, "/dashboard/system/clear")})
    assert "agora vale o ambiente" in location(response)
    assert vault.effective("smtp_password") == (SMTP_SECRET, "ambiente")
    assert fetch("SELECT action FROM audit_log WHERE action = 'system.secret.clear'")


def test_wordpress_testa_e_grava_por_blog(panel_client, wp):
    page = panel_client.get("/dashboard/system").text
    v = version_of(page, "/dashboard/system/wp/cloud")
    bad = panel_client.post("/dashboard/system/wp/cloud", follow_redirects=False, data={
        "action": "save", "user": "sb-publisher", "password": "errada", "version": v})
    assert "recusou a credencial" in location(bad)
    good = panel_client.post("/dashboard/system/wp/cloud", follow_redirects=False, data={
        "action": "save", "user": "sb-publisher", "password": WP_SECRET, "version": v})
    assert "gravado no cofre" in location(good)
    assert vault.wp_credentials("cloud") == ("sb-publisher", WP_SECRET)
    assert vault.effective("wp.ia.user")[1] != "banco"


def test_wordpress_papel_sem_publicar_e_recusado(monkeypatch, wp):
    monkeypatch.setattr(sitepub, "_transport", httpx.MockTransport(
        lambda r: httpx.Response(200, json={"slug": "leitor", "roles": ["subscriber"]})))
    site = fetch("SELECT * FROM sites WHERE site_key = 'cloud'")[0]
    check = integrations.check_wordpress(site, "leitor", "x")
    assert not check.ok and "não publica" in check.message


def test_brevo_api_testa_a_conta(monkeypatch):
    monkeypatch.setattr(integrations, "_transport", httpx.MockTransport(
        lambda r: httpx.Response(200 if r.headers["api-key"] == "xkeysib-boa" else 401)))
    assert integrations.check_email({"brevo_api_key": "xkeysib-boa"}).ok
    check = integrations.check_email({"brevo_api_key": "xkeysib-ma"})
    assert not check.ok and "401" in check.message


# --- Linha de comando ----------------------------------------------------------------
def test_import_env_item_a_item(monkeypatch, capsys, wp):
    monkeypatch.setenv("SB_WP_CLOUD_APP_USER", "sb-publisher")
    monkeypatch.setenv("SB_WP_CLOUD_APP_PASSWORD", WP_SECRET)
    assert vault_cli.main(["import-env", "wp.cloud.password"]) == 0
    out = capsys.readouterr().out
    assert "Teste de conexão: OK" in out and "Só leitura" in out and WP_SECRET not in out
    assert fetch("SELECT count(*) AS n FROM system_secrets")[0]["n"] == 0
    assert vault_cli.main(["import-env", "wp.cloud.password", "--apply"]) == 0
    assert vault.effective("wp.cloud.password") == (WP_SECRET, "banco")
    assert vault_cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert WP_SECRET not in out and "wp.cloud.password" in out


def test_import_env_recusa_se_o_teste_falha(monkeypatch, capsys, wp):
    monkeypatch.setenv("SB_WP_CLOUD_APP_USER", "sb-publisher")
    monkeypatch.setenv("SB_WP_CLOUD_APP_PASSWORD", "errada")
    assert vault_cli.main(["import-env", "wp.cloud.password", "--apply"]) == 2
    assert fetch("SELECT count(*) AS n FROM system_secrets")[0]["n"] == 0


def test_rekey_recifra_cofre_e_chaves_de_ia(monkeypatch, capsys):
    put("smtp_password", SMTP_SECRET)
    with session_scope() as db:
        db.execute(text("UPDATE ai_providers SET api_key_enc = :v WHERE name = 'claude'"),
                   {"v": ai_secrets.encrypt("sk-ia-1234")})
    new_key = "chave-nova-de-teste-com-mais-de-32-caracteres"
    monkeypatch.setenv("SB_SECRET_KEY_NEW", new_key)
    assert vault_cli.main(["rekey"]) == 0                 # só confere
    assert vault.get("smtp_password") == SMTP_SECRET
    assert vault_cli.main(["rekey", "--apply"]) == 0
    assert "sessões do painel caem" in capsys.readouterr().out
    monkeypatch.setattr(settings, "sb_secret_key", new_key)
    vault.invalidate()
    assert vault.effective("smtp_password") == (SMTP_SECRET, "banco")
    enc = fetch("SELECT api_key_enc FROM ai_providers WHERE name = 'claude'")[0]["api_key_enc"]
    assert ai_secrets.decrypt(enc) == "sk-ia-1234"


def test_rekey_nao_muda_nada_se_algo_nao_abre(monkeypatch):
    with session_scope() as db:
        db.execute(text("INSERT INTO system_secrets (key, value_enc) VALUES ('smtp_user', 'lixo')"))
    monkeypatch.setenv("SB_SECRET_KEY_NEW", "chave-nova-de-teste-com-mais-de-32-caracteres")
    assert vault_cli.main(["rekey", "--apply"]) == 2
    assert fetch("SELECT value_enc FROM system_secrets")[0]["value_enc"] == "lixo"


# --- R71: blog com hífen no site_key --------------------------------------------------------------
def test_r71_site_key_com_hifen_tem_item_e_le_o_ambiente(monkeypatch):
    """`sites.Site` aceita hífen; o cofre também tem de aceitar, e o ambiente usa `_`."""
    user_item = vault.item("wp.meu-blog.user")
    password_item = vault.item("wp.meu-blog.password")
    assert (user_item.key, password_item.secret) == ("wp.meu-blog.user", True)
    assert vault.wp_env_name("meu-blog", "password") == "SB_WP_MEU_BLOG_APP_PASSWORD"
    assert vault.wp_env_name("cloud", "user") == "SB_WP_CLOUD_APP_USER"   # os 6 blogs: igual
    monkeypatch.setenv("SB_WP_MEU_BLOG_APP_USER", "sb-publisher")
    monkeypatch.setenv("SB_WP_MEU_BLOG_APP_PASSWORD", WP_SECRET)
    vault.invalidate()
    assert vault.wp_credentials("meu-blog") == ("sb-publisher", WP_SECRET)
    assert vault.effective("wp.meu-blog.password") == (WP_SECRET, "ambiente")


@pytest.mark.parametrize("key", ["wp.Meu-Blog.user", "wp.meu_blog.user", "wp.-x.user",
                                 "wp.9blog.user", "wp.meu-blog.token"])
def test_r71_chaves_fora_do_padrao_do_site_key(key):
    with pytest.raises(vault.VaultError, match="desconhecido"):
        vault.item(key)
