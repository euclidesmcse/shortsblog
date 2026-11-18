"""E2.9 / D-018 — Cloudflare Access (JWT) + login próprio do painel."""
from __future__ import annotations

import time
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.core import panel_auth
from app.core.config import settings
from tests.conftest import TEST_PASSWORD, fetch, login_as, make_user
from tests.test_review_panel import waiting  # noqa: F401  (fixture `waiting`)
from tests.test_sites import cloud  # noqa: F401  (fixture `cloud`, usada por `waiting`)

TEAM = "equipa-teste.cloudflareaccess.com"
AUD = "a" * 64
HOST = "painel.shortsblog.com"
EMAIL = "euclides@example.com"

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def token(*, email: str = EMAIL, aud: str = AUD, iss: str = f"https://{TEAM}",
          exp_in: int = 600, key=_KEY) -> str:
    now = int(time.time())
    claims = {"email": email, "aud": [aud], "iss": iss, "iat": now, "exp": now + exp_in,
              "sub": "id-do-utilizador"}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "k1"})


@pytest.fixture(autouse=True)
def access(monkeypatch):
    monkeypatch.setattr(settings, "cf_access_team_domain", TEAM)
    monkeypatch.setattr(settings, "cf_access_aud", AUD)
    monkeypatch.setattr(settings, "panel_allowed_emails", f"{EMAIL}, outro@example.com")
    monkeypatch.setattr(settings, "panel_password", "senha-do-painel")
    monkeypatch.setattr(settings, "sb_host", HOST)
    # Sem rede: a "chave pública do team" é a do par gerado aqui.
    fake_jwks = SimpleNamespace(
        get_signing_key_from_jwt=lambda _t: SimpleNamespace(key=_KEY.public_key()))
    monkeypatch.setattr(panel_auth, "_jwks_client", lambda team: fake_jwks)


def public(**headers) -> dict:
    return {"host": HOST, **headers}


def logged_in(client, email: str = EMAIL, role: str = "admin"):
    return login_as(client, email, role)


# --- 1.ª camada: Cloudflare Access ------------------------------------------
def test_publico_sem_token_e_negado(client):
    response = client.get("/", headers=public(), follow_redirects=False)
    assert response.status_code == 403


@pytest.mark.parametrize("bad", [
    token(aud="b" * 64),
    token(iss="https://outra-equipa.cloudflareaccess.com"),
    token(exp_in=-60),
    token(email="intruso@example.com"),
    token(key=_OTHER_KEY),
    "nem-sequer-um-jwt",
])
def test_publico_com_token_invalido_e_negado(client, bad):
    logged_in(client)
    response = client.get("/", headers=public(**{panel_auth.ACCESS_HEADER: bad}))
    assert response.status_code == 403


def test_publico_com_token_valido_e_sessao_entra(client):
    logged_in(client)
    response = client.get("/", headers=public(**{panel_auth.ACCESS_HEADER: token()}))
    assert response.status_code == 200 and "Sair" in response.text


def test_cookie_do_access_tambem_serve(client):
    logged_in(client)
    client.cookies.set(panel_auth.ACCESS_COOKIE, token())
    assert client.get("/", headers=public()).status_code == 200


def test_publico_sem_access_configurado_falha_fechado(client, monkeypatch):
    monkeypatch.setattr(settings, "cf_access_aud", "")
    logged_in(client)
    response = client.get("/", headers=public(**{panel_auth.ACCESS_HEADER: token()}))
    assert response.status_code == 403


def test_liveness_publico_sem_token(client):
    assert client.get("/health/live", headers=public()).status_code == 200
    assert client.get("/health/ready", headers=public()).status_code == 403


def test_api_pelo_dominio_publico_exige_access_e_bearer(client, auth):
    assert client.get("/api/ai/chain", headers=public(**auth)).status_code == 403
    ok = public(**auth, **{panel_auth.ACCESS_HEADER: token()})
    assert client.get("/api/ai/chain", headers=ok).status_code == 200


def test_rede_interna_nao_passa_pelo_access(client, auth):
    """n8n e healthchecks: Host interno, só o bearer conta."""
    internal = {"host": "sb-engine-api:8000"}
    assert client.get("/api/ai/chain", headers={**internal, **auth}).status_code == 200
    assert client.get("/health/ready", headers=internal).status_code == 200


# --- 2.ª camada: login próprio ------------------------------------------------
def test_painel_sem_sessao_vai_para_o_login(client):
    for path in ("/", "/dashboard/review/1", "/docs", "/openapi.json"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == f"/login?next={path}"


def test_sessao_adulterada_ou_expirada(client, monkeypatch):
    user_id = make_user(EMAIL)
    client.cookies.set(panel_auth.SESSION_COOKIE, panel_auth.make_session(user_id, 1) + "x")
    assert client.get("/", follow_redirects=False).status_code == 303
    client.cookies.set(panel_auth.SESSION_COOKIE, panel_auth.make_session(user_id, 1))
    monkeypatch.setattr(settings, "panel_session_hours", -1)
    assert client.get("/", follow_redirects=False).status_code == 303


def test_cookie_do_login_unico_antigo_nao_vale_mais(client):
    make_user(EMAIL)
    old = panel_auth._serializer().dumps({"email": EMAIL})
    client.cookies.set(panel_auth.SESSION_COOKIE, old)
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_pelo_dominio_publico(client):
    make_user(EMAIL)
    headers = public(**{panel_auth.ACCESS_HEADER: token()})
    page = client.get("/login?next=/dashboard/review/3", headers=headers)
    assert page.status_code == 200 and EMAIL in page.text and "readonly" in page.text

    wrong = client.post("/login", data={"password": "errada", "next": "/"}, headers=headers)
    assert wrong.status_code == 401 and "incorretos" in wrong.text

    response = client.post("/login", headers=headers, follow_redirects=False,
                           data={"password": TEST_PASSWORD, "next": "/dashboard/review/3"})
    assert response.status_code == 303
    assert response.headers["location"] == "/dashboard/review/3"
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=strict" in cookie
    assert panel_auth.read_session(client.cookies.get(panel_auth.SESSION_COOKIE)) == (1, 1)


def test_cookie_sem_secure_com_o_portal_em_http(client, monkeypatch):
    """E19.2: o pacote em http (teste local) — com Secure o navegador descartaria a sessão."""
    monkeypatch.setattr(settings, "public_base_url", f"http://{HOST}")
    make_user(EMAIL)
    headers = public(**{panel_auth.ACCESS_HEADER: token()})
    response = client.post("/login", headers=headers, follow_redirects=False,
                           data={"password": TEST_PASSWORD, "next": "/"})
    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" not in cookie and "SameSite=strict" in cookie


def test_pelo_access_nao_se_entra_com_outro_email(client):
    make_user(EMAIL)
    make_user("outro@example.com")
    headers = public(**{panel_auth.ACCESS_HEADER: token()})
    response = client.post("/login", headers=headers, follow_redirects=False,
                           data={"email": "outro@example.com", "password": TEST_PASSWORD})
    assert response.status_code == 401


@pytest.mark.parametrize("target", ["//evil.example.com", "https://evil.example.com", "evil",
                                    "/\\evil.example.com", "/\\/evil.example.com",
                                    "/ok\\..\\x", "/\t/evil.example.com", "/x\r\nSet-Cookie: a=b"])
def test_login_nao_redireciona_para_fora(client, target):
    make_user(EMAIL)
    response = client.post("/login", data={"email": EMAIL, "password": TEST_PASSWORD,
                                           "next": target}, follow_redirects=False)
    assert response.headers["location"] == "/"


def test_sem_usuarios_nem_senha_ninguem_entra(client, monkeypatch):
    monkeypatch.setattr(settings, "panel_password", "")
    assert "Nenhum usuário cadastrado" in client.get("/login").text
    response = client.post("/login", data={"email": EMAIL, "password": "x"})
    assert response.status_code == 401
    assert fetch("SELECT count(*) AS n FROM users")[0]["n"] == 0


def test_aprovacao_registada_com_o_email_do_access(client, waiting):  # noqa: F811
    """Fluxo real de browser: https no domínio público, cookie Secure devolvido."""
    from fastapi.testclient import TestClient

    from app.main import app

    make_user(EMAIL)
    browser = TestClient(app, base_url=f"https://{HOST}")
    headers = {panel_auth.ACCESS_HEADER: token()}
    assert browser.post("/login", headers=headers,
                        data={"password": TEST_PASSWORD}).status_code == 200
    browser.post(f"/dashboard/review/{waiting}/approve", headers=headers,
                 data={"decided_by": "forjado", "approved_by": "forjado"})
    assert fetch("SELECT decided_by FROM approvals WHERE job_id = :i",
                 i=waiting)[0]["decided_by"] == EMAIL


def test_logout(client):
    logged_in(client)
    response = client.post("/logout", follow_redirects=False)
    assert response.headers["location"] == "/login"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f'{panel_auth.SESSION_COOKIE}=""') and "Max-Age=0" in cookie


# --- R46c: POST vindo de outra origem -----------------------------------------
def test_post_de_outra_origem_e_recusado(client):
    logged_in(client)
    hdr = public(**{panel_auth.ACCESS_HEADER: token(), "origin": "https://evil.example"})
    response = client.post("/dashboard/review/batch", headers=hdr, data={"action": "approve"},
                           follow_redirects=False)
    assert response.status_code == 403


def test_post_da_mesma_origem_ou_sem_origem_passa(client):
    logged_in(client)
    base = {panel_auth.ACCESS_HEADER: token()}
    own = f"https://{HOST}"
    for extra in ({"origin": own}, {"referer": f"{own}/dashboard/review"}, {}):
        response = client.post("/dashboard/review/batch", headers=public(**base, **extra),
                               data={"action": "approve"}, follow_redirects=False)
        assert response.status_code == 303  # chegou ao handler (sem ids: volta com erro)


def test_get_com_origem_estranha_nao_e_afetado(client):
    logged_in(client)
    response = client.get("/", headers=public(**{panel_auth.ACCESS_HEADER: token(),
                                                 "origin": "https://evil.example"}))
    assert response.status_code == 200


# --- Sessão vencida: aviso em vez de clique perdido (12/11, lote de 13/11) -------
def _seconds_left(html: str) -> int:
    import re
    match = re.search(r"Date\.now\(\) \+ \((-?\d+) - 60\) \* 1000", html)
    assert match, "faixa de sessão expirada ausente"
    return int(match.group(1))


def test_post_sem_sessao_volta_a_pagina_de_origem_com_aviso(client):
    hdr = public(**{panel_auth.ACCESS_HEADER: token(),
                    "referer": f"https://{HOST}/dashboard/review?site=saude"})
    response = client.post("/dashboard/review/batch", headers=hdr,
                           data={"action": "approve", "job_ids": "270"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == (
        "/login?next=%2Fdashboard%2Freview%3Fsite%3Dsaude&expired=1")


@pytest.mark.parametrize("referer", ["", "https://evil.example/dashboard/review"])
def test_post_sem_sessao_sem_referer_proprio_volta_ao_inicio(client, referer):
    hdr = public(**{panel_auth.ACCESS_HEADER: token()}, **({"referer": referer} if referer else {}))
    response = client.post("/dashboard/review/batch", headers=hdr, data={"action": "approve"},
                           follow_redirects=False)
    assert response.headers["location"] == "/login?next=%2F&expired=1"


def test_login_avisa_que_a_acao_nao_foi_aplicada(client):
    page = client.get("/login?next=/dashboard/review&expired=1",
                      headers=public(**{panel_auth.ACCESS_HEADER: token()}))
    assert "NÃO foi aplicada" in page.text and 'value="/dashboard/review"' in page.text
    assert "NÃO foi aplicada" not in client.get(
        "/login", headers=public(**{panel_auth.ACCESS_HEADER: token()})).text


def test_pagina_traz_o_prazo_da_sessao_propria(client):
    logged_in(client)
    left = _seconds_left(client.get("/").text)  # rede interna: só o login próprio conta
    assert settings.panel_session_hours * 3600 - 5 <= left <= settings.panel_session_hours * 3600


def test_prazo_e_o_do_access_quando_vence_antes(client):
    logged_in(client)
    html = client.get("/dashboard/review",
                      headers=public(**{panel_auth.ACCESS_HEADER: token(exp_in=300)})).text
    assert 290 <= _seconds_left(html) <= 300
    assert "Recarregar e entrar de novo" in html


def test_login_nao_traz_a_faixa(client):
    assert "sb-expired" not in client.get("/login").text
