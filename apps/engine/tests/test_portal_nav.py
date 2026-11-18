"""E18.4a: menu único do portal, por papel, em todas as telas com sessão."""
from __future__ import annotations

import re

import pytest

from tests.conftest import login_as, make_user

PAGES = ["/", "/dashboard/review", "/dashboard/ideas", "/dashboard/health", "/conta/senha"]
ADMIN_PAGES = ["/dashboard/ai", "/dashboard/users"]
NAV = '<nav class="sb-nav"'


def _nav(html: str) -> str:
    match = re.search(r'<nav class="sb-nav".*?</nav>', html, re.S)
    assert match, "menu do portal ausente"
    return match.group(0)


@pytest.mark.parametrize("path", PAGES + ADMIN_PAGES)
def test_admin_tem_o_menu_completo_em_todas_as_telas(panel_client, path):
    response = panel_client.get(path)
    assert response.status_code == 200
    nav = _nav(response.text)
    for href in ("/dashboard/review", "/dashboard/ideas", "/dashboard/health",
                 "/dashboard/ai", "/dashboard/users", "/conta/senha", "/logout"):
        assert f'"{href}"' in nav
    assert "euclides@example.com" in nav and ">admin<" in nav


@pytest.mark.parametrize("path", PAGES)
def test_operador_nao_ve_as_areas_de_admin_no_menu(client, path):
    login_as(client, "op@example.com", "operador")
    nav = _nav(client.get(path).text)
    assert '"/dashboard/review"' in nav and ">operador<" in nav
    assert '"/dashboard/ai"' not in nav and '"/dashboard/users"' not in nav


@pytest.mark.parametrize("path,label", [("/", "Sala de controle"),
                                        ("/dashboard/review", "Aprovações"),
                                        ("/dashboard/users", "Usuários")])
def test_item_da_pagina_atual_fica_marcado(panel_client, path, label):
    nav = _nav(panel_client.get(path).text)
    current = re.findall(r'<a href="([^"]+)" aria-current="page">([^<]+)</a>', nav)
    assert current == [(path, label)]


def test_detalhe_marca_a_secao(panel_client):
    nav = _nav(panel_client.get("/dashboard/review?site=cloud").text)
    assert 'href="/dashboard/review" aria-current="page"' in nav


def test_login_nao_tem_menu(client):
    assert NAV not in client.get("/login").text


def test_troca_obrigatoria_de_senha_nao_mostra_o_menu(client):
    login_as(client, "novo@example.com", "operador", must_change=True)
    page = client.get("/conta/senha?forced=1")
    assert page.status_code == 200 and NAV not in page.text and "Sair" in page.text


def test_telas_nao_repetem_os_atalhos_antigos(panel_client):
    for path in PAGES[:4] + ADMIN_PAGES:
        html = panel_client.get(path).text
        assert "← Painel" not in html and "← painel do motor" not in html
        assert html.count('action="/logout"') == 1  # só o do menu


def test_menu_nao_aparece_sem_sessao(client):
    make_user("euclides@example.com")
    assert NAV not in client.get("/login?next=/").text
