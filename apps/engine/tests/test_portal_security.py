"""E18.5: segurança do portal por papel, varrendo TODAS as rotas do painel.

As rotas são descobertas na própria app: uma tela nova entra nestes testes sozinha. E toda rota
tem de estar classificada (admin ou operador) — uma rota nova sem classificação reprova, para a
decisão ser explícita.
"""
from __future__ import annotations

import re

import pytest

from app.core import panel_auth
from tests.conftest import login_as

# Áreas que o operador usa (E18.1d: aprovar/rejeitar, pauta, requeue, consulta).
OPERATOR_PREFIXES = ("/conta/", "/dashboard/review", "/dashboard/ideas", "/dashboard/health",
                     "/dashboard/jobs", "/dashboard/sites")
PARAMS = {"site_key": "cloud", "name": "claude", "role": "writer", "job_id": "999999",
          "idea_id": "999999", "user_id": "999999",
          "audit_id": "999999"}


def _walk(routes):
    for route in routes:
        if hasattr(route, "original_router"):        # FastAPI recente embrulha o include_router
            yield from _walk(route.original_router.routes)
        else:
            yield route


def panel_routes() -> list[tuple[str, str]]:
    from app.main import app

    found = set()
    for route in _walk(app.routes):
        path = getattr(route, "path", "")
        if path == "/" or path.startswith(("/dashboard", "/conta")):
            for method in getattr(route, "methods", None) or ():
                if method != "HEAD":
                    found.add((method, path))
    return sorted(found)


def concrete(path: str) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: PARAMS.get(m.group(1), "x"), path)


def is_admin(path: str) -> bool:
    return panel_auth.is_admin_path(path)


ROUTES = panel_routes()
POSTS = [(m, p) for m, p in ROUTES if m == "POST"]


def test_ha_rotas_e_todas_estao_classificadas():
    assert len(ROUTES) > 40
    unclassified = [p for _, p in ROUTES
                    if p != "/" and not is_admin(p) and not p.startswith(OPERATOR_PREFIXES)]
    assert unclassified == [], f"classifique estas rotas (admin ou operador): {unclassified}"


@pytest.mark.parametrize("method,path", ROUTES)
def test_sem_sessao_vai_ao_login_sem_executar_nada(client, method, path):
    response = client.request(method, concrete(path), follow_redirects=False)
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith("/login?next=")
    if method == "POST":
        assert "expired=1" in location          # a ação não foi aplicada, e a tela avisa


@pytest.mark.parametrize("method,path", [r for r in ROUTES if is_admin(r[1])])
def test_operador_recebe_403_nas_areas_de_admin(client, method, path):
    login_as(client, "op@example.com", "operador")
    assert client.request(method, concrete(path), follow_redirects=False).status_code == 403


@pytest.mark.parametrize("path", [p for m, p in ROUTES if m == "GET" and not is_admin(p)])
def test_operador_abre_as_telas_dele(client, path, monkeypatch):
    login_as(client, "op@example.com", "operador")
    response = client.get(concrete(path), follow_redirects=False)
    assert response.status_code not in (403, 500), (path, response.status_code)


@pytest.mark.parametrize("method,path", POSTS)
def test_post_de_outra_origem_e_recusado_em_todas_as_rotas(panel_client, method, path):
    response = panel_client.post(concrete(path), headers={"origin": "https://evil.example"},
                                 follow_redirects=False)
    assert response.status_code == 403


@pytest.mark.parametrize("path", [p for m, p in ROUTES if m == "GET"])
def test_admin_abre_todas_as_telas_sem_erro(panel_client, path, monkeypatch):
    from app.services.ai import gateway

    monkeypatch.setattr(gateway, "login_state", lambda *_: {"status": "none"})
    response = panel_client.get(concrete(path), follow_redirects=False)
    assert response.status_code < 500, (path, response.status_code)


@pytest.mark.parametrize("method,path", POSTS)
def test_troca_de_senha_pendente_bloqueia_todas_as_acoes(client, method, path):
    login_as(client, "novo@example.com", "admin", must_change=True)
    response = client.request(method, concrete(path), follow_redirects=False)
    if path == "/conta/senha":
        assert response.status_code != 303 or "forced" not in response.headers.get("location", "")
    else:
        assert response.status_code == 303
        assert response.headers["location"] == "/conta/senha?forced=1"


def test_sessao_de_usuario_desativado_cai_em_todas_as_telas(client):
    from sqlalchemy import text

    from app.core.db import session_scope

    login_as(client, "op@example.com", "operador")
    with session_scope() as db:
        db.execute(text("UPDATE users SET active = false WHERE email = 'op@example.com'"))
    for method, path in ROUTES:
        response = client.request(method, concrete(path), follow_redirects=False)
        assert response.status_code == 303, (method, path)
