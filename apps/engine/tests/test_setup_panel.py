"""E19.5 — assistente de 1.º arranque (Primeiros passos).

O gateway falso é o dos testes da tela de IA; o SMTP e o WordPress falsos são as fixtures dos
testes do cofre, importadas com apelido e usadas por `usefixtures`.
"""
from __future__ import annotations

import re
from urllib.parse import unquote_plus

import httpx
import pytest

from app.core.db import session_scope
from app.services import setup, vault
from app.services.ai import chain, gateway
from tests.conftest import TEST_PASSWORD, fetch, login_as, make_user
from tests.test_ai_panel import FakeGateway
from tests.test_editorial import legacy_sync
from tests.test_vault import WP_SECRET, put
from tests.test_vault import smtp as _smtp_fixture  # noqa: F401
from tests.test_vault import wp as _wp_fixture  # noqa: F401

OPENAI_KEY = "sk-chave-openai-do-assistente-1234"
ANTHROPIC_KEY = "sk-ant-chave-do-assistente-5678"


def location(response) -> str:
    assert response.status_code == 303, response.text
    return unquote_plus(response.headers["location"])


def order(role: str) -> list[str]:
    with session_scope() as db:
        return [p.name for p in chain.load_chain(db, role)]


def all_wp_credentials() -> None:
    for (site_key,) in [(r["site_key"],) for r in fetch("SELECT site_key FROM sites")]:
        put(f"wp.{site_key}.user", "sb-publisher")
        put(f"wp.{site_key}.password", WP_SECRET)


def version_of(page: str, action: str) -> str:
    block = page.split(f'action="{action}"', 1)[1]
    return re.search(r'name="version" value="([0-9a-f]*)"', block).group(1)


@pytest.fixture
def gw(monkeypatch) -> FakeGateway:
    fake = FakeGateway()
    monkeypatch.setattr(gateway, "_transport", httpx.MockTransport(fake))
    return fake


@pytest.fixture
def fresh(panel_client):
    """Instalação nova: blogs semeados (desligados), só as assinaturas sem login nas cadeias."""
    legacy_sync()
    return panel_client


def add_ai(client, preset: str, model: str, key: str):
    return client.post("/dashboard/setup/ai", follow_redirects=False,
                       data={"preset": preset, "model": model, "api_key": key})


# --- Página e acesso ----------------------------------------------------------------------
def test_instalacao_nova_mostra_o_que_falta(fresh):
    page = fresh.get("/dashboard/setup")
    assert page.status_code == 200 and page.headers["cache-control"] == "no-store"
    assert "Primeiros passos" in page.text and "obrigatórios" in page.text
    assert "nenhum provedor verificado" in page.text
    # Os cartões são os mesmos da tela Sistema, voltando para cá.
    assert 'action="/dashboard/system/email"' in page.text
    assert 'name="back" value="setup"' in page.text
    with session_scope() as db:
        assert setup.pending(db)


def test_operador_nao_entra(client):
    legacy_sync()
    login_as(client, "op@example.com", role="operador")
    assert client.get("/dashboard/setup", follow_redirects=False).status_code == 403
    assert client.post("/dashboard/setup/ai", data={"model": "x"},
                       follow_redirects=False).status_code == 403


def test_sala_de_controle_avisa_so_o_admin(fresh, client):
    assert "Configuração inicial incompleta" in fresh.get("/").text
    login_as(client, "op@example.com", role="operador")
    assert "Configuração inicial incompleta" not in client.get("/").text


def test_depois_da_senha_inicial_o_admin_vai_para_o_assistente(client):
    legacy_sync()
    make_user("novo@example.com", "admin", must_change=True)
    login_as(client, "novo@example.com")
    new = "outra-senha-forte-456"
    response = client.post("/conta/senha", follow_redirects=False,
                           data={"current": TEST_PASSWORD, "new": new, "confirm": new})
    assert location(response).startswith("/dashboard/setup")


# --- IA -------------------------------------------------------------------------------------
def test_ia_verifica_e_poe_a_frente_das_cadeias(fresh, gw):
    msg = location(add_ai(fresh, "openai", "gpt-teste", OPENAI_KEY))
    assert "openai-api verificado" in msg and "OUTRA família" in msg
    assert OPENAI_KEY not in msg
    for role in chain.ROLES:
        assert order(role)[0] == "openai-api"
    row = fetch("SELECT family, base_url FROM ai_providers WHERE name = 'openai-api'")[0]
    assert row == {"family": "openai", "base_url": "https://api.openai.com/v1"}
    with session_scope() as db:
        assert not setup.ai_state(db)["ok"]          # só uma família verificada

    msg = location(add_ai(fresh, "anthropic", "claude-teste", ANTHROPIC_KEY))
    assert "verificado" in msg and "OUTRA família" not in msg
    with session_scope() as db:
        state = setup.ai_state(db)
    assert state["ok"] and "revisor" in state["summary"]
    dump = str(fetch("SELECT details FROM audit_log"))
    assert OPENAI_KEY not in dump and ANTHROPIC_KEY not in dump


def test_ia_que_falha_na_verificacao_nao_entra_nas_cadeias(fresh, gw):
    gw.status["openai-api"] = "not_authenticated"
    before = order("writer")
    msg = location(add_ai(fresh, "openai", "gpt-teste", OPENAI_KEY))
    assert "verificação falhou" in msg and "chave recusada" in msg
    assert order("writer") == before
    assert fetch("SELECT 1 FROM ai_providers WHERE name = 'openai-api'")   # guardado para corrigir


def test_ia_recusa_modelo_de_outra_familia(fresh, gw):
    msg = location(add_ai(fresh, "openai", "claude-sonnet", OPENAI_KEY))
    assert "família" in msg and OPENAI_KEY not in msg
    assert not fetch("SELECT 1 FROM ai_providers WHERE kind = 'api'")


# --- Blog, e-mail, WordPress e ligar ------------------------------------------------------------
def test_nome_e_descricao_do_blog(fresh):
    page = fresh.get("/dashboard/setup").text
    v = version_of(page, "/dashboard/setup/site/cloud")
    msg = location(fresh.post("/dashboard/setup/site/cloud", follow_redirects=False, data={
        "name": "Nuvem pelo Mundo", "description": "Nuvem e infraestrutura, do mundo todo.",
        "version": v}))
    assert "atualizado" in msg
    row = fetch("SELECT name, config->>'description' AS d FROM sites WHERE site_key = 'cloud'")
    assert row[0] == {"name": "Nuvem pelo Mundo", "d": "Nuvem e infraestrutura, do mundo todo."}
    # Versão velha (outra aba) é recusada.
    stale = location(fresh.post("/dashboard/setup/site/cloud", follow_redirects=False, data={
        "name": "Outro", "version": v}))
    assert "recarregue" in stale


@pytest.mark.usefixtures("_smtp_fixture", "_wp_fixture")
def test_cartoes_da_sistema_voltam_para_o_assistente(fresh):
    page = fresh.get("/dashboard/setup").text
    v = version_of(page, "/dashboard/system/wp/cloud")
    response = fresh.post("/dashboard/system/wp/cloud", follow_redirects=False, data={
        "action": "save", "user": "sb-publisher", "password": WP_SECRET, "version": v,
        "back": "setup"})
    assert location(response).startswith("/dashboard/setup?") and "#wp-cloud" in location(response)
    assert vault.wp_credentials("cloud") == ("sb-publisher", WP_SECRET)
    # Destino desconhecido cai na tela Sistema (nada de URL livre).
    other = fresh.post("/dashboard/system/email", follow_redirects=False, data={
        "action": "test", "back": "https://evil.test"})
    assert location(other).startswith("/dashboard/system?")


def test_ligar_so_com_ia_e_wordpress(fresh, gw):
    page = fresh.get("/dashboard/setup").text
    assert "Antes de ligar o blog" in page
    v = version_of(page, "/dashboard/setup/site/cloud/enable")
    blocked = location(fresh.post("/dashboard/setup/site/cloud/enable", follow_redirects=False,
                                  data={"version": v}))
    assert "Antes de ligar o blog" in blocked
    assert not fetch("SELECT enabled FROM sites WHERE site_key = 'cloud'")[0]["enabled"]

    add_ai(fresh, "openai", "gpt-teste", OPENAI_KEY)
    add_ai(fresh, "anthropic", "claude-teste", ANTHROPIC_KEY)
    all_wp_credentials()
    page = fresh.get("/dashboard/setup").text
    v = version_of(page, "/dashboard/setup/site/cloud/enable")
    msg = location(fresh.post("/dashboard/setup/site/cloud/enable", follow_redirects=False,
                              data={"version": v}))
    assert "Blog ligado" in msg
    assert fetch("SELECT enabled FROM sites WHERE site_key = 'cloud'")[0]["enabled"]
    assert fetch("SELECT 1 FROM audit_log WHERE action LIKE 'config.%'")
    with session_scope() as db:
        assert not setup.pending(db)
    page = fresh.get("/dashboard/setup").text
    assert "Tudo pronto" in page
    assert "Configuração inicial incompleta" not in fresh.get("/").text
