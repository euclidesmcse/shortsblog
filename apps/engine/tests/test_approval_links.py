"""E10.3 — links de decisão no e-mail: assinados, 72 h, uso único, GET não decide (R7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core import panel_auth
from app.core.config import settings
from app.core.db import session_scope
from app.services import approval_links as links
from tests.conftest import fetch
from tests.test_review_panel import waiting  # noqa: F401  (fixture: um job em AWAITING_APPROVAL)
from tests.test_sites import cloud  # noqa: F401

EMAIL = "euclides@example.com"
ACCESS = {"Cf-Access-Jwt-Assertion": "token-qualquer"}


@pytest.fixture
def access(monkeypatch):
    """O pedido chega pelo domínio público com um Cloudflare Access válido para `EMAIL`."""
    monkeypatch.setattr(settings, "sb_host", "testserver")
    monkeypatch.setattr(panel_auth, "verify_access_token", lambda token: EMAIL)


def make(job: int, action: str = "approve", now: datetime | None = None) -> str:
    with session_scope() as db:
        return links.create(db, job, action, now)


def status(job: int) -> str:
    return fetch("SELECT status FROM publication_jobs WHERE id = :i", i=job)[0]["status"]


# --- O token ------------------------------------------------------------------------------


def test_token_valido_e_adulterados_recusados(waiting):  # noqa: F811
    token = make(waiting)
    with session_scope() as db:
        assert links.lookup(db, token).job_id == waiting
    link_id, exp, sig = token.split(".")
    for bad in (
        f"{link_id}.{exp}.{sig[:-2]}xx",
        f"{link_id}.{int(exp) + 3600}.{sig}",
        f"999999.{exp}.{sig}",
        f"{link_id}.{exp}",
        "lixo",
        "",
        f"{link_id}.abc.{sig}",
    ):
        with session_scope() as db, pytest.raises(links.InvalidLink):
            links.lookup(db, bad)


def test_expira_ao_fim_de_72_horas(waiting):  # noqa: F811
    token = make(waiting)
    with session_scope() as db:
        links.lookup(db, token, datetime.now(UTC) + timedelta(hours=71))
    with session_scope() as db, pytest.raises(links.InvalidLink):
        links.lookup(db, token, datetime.now(UTC) + timedelta(hours=73))


def test_uso_unico_e_irmaos_gastos_juntos(waiting):  # noqa: F811
    approve, reject = make(waiting, "approve"), make(waiting, "reject")
    with session_scope() as db:
        link = links.lookup(db, approve)
        assert links.consume(db, link, EMAIL) is True
        assert links.consume(db, link, EMAIL) is False
    for token in (approve, reject):
        with session_scope() as db, pytest.raises(links.InvalidLink):
            links.lookup(db, token)


def test_chave_diferente_invalida_os_links(waiting, monkeypatch):  # noqa: F811
    token = make(waiting)
    monkeypatch.setattr(settings, "sb_secret_key", "outra-chave-qualquer-0123456789abcdef")
    with session_scope() as db, pytest.raises(links.InvalidLink):
        links.lookup(db, token)


# --- As rotas ------------------------------------------------------------------------------


def test_get_nao_decide_mesmo_repetido_e_mostra_o_botao(client, access, waiting):  # noqa: F811
    token = make(waiting)
    for _ in range(3):  # um scanner de e-mail segue o link várias vezes
        page = client.get(f"/a/{token}", headers=ACCESS)
        assert page.status_code == 200 and "Aprovar para publicação" in page.text
    assert status(waiting) == "AWAITING_APPROVAL"
    assert fetch("SELECT count(*) AS n FROM approvals WHERE job_id = :i", i=waiting)[0]["n"] == 0
    assert (
        page.headers["referrer-policy"] == "no-referrer"
        and "no-store" in page.headers["cache-control"]
    )


def test_post_aprova_com_o_email_do_access_e_canal_email(client, access, waiting):  # noqa: F811
    token = make(waiting, "approve")
    other = make(waiting, "reject")
    page = client.post(f"/a/{token}", headers=ACCESS, data={"note": "ok"})
    assert page.status_code == 200 and "Aprovado" in page.text
    assert status(waiting) == "APPROVED"
    row = fetch(
        "SELECT decision, decided_by, channel, note FROM approvals WHERE job_id = :i", i=waiting
    )[0]
    assert (row["decision"], row["decided_by"], row["channel"], row["note"]) == (
        "approved",
        EMAIL,
        "email",
        "ok",
    )
    # reutilizar o mesmo link, ou usar o irmão, já não funciona e não muda nada
    assert client.post(f"/a/{token}", headers=ACCESS).status_code == 404
    assert client.post(f"/a/{other}", headers=ACCESS).status_code == 404
    assert status(waiting) == "APPROVED"
    assert len(fetch("SELECT 1 FROM approvals WHERE job_id = :i", i=waiting)) == 1


def test_post_rejeita(client, access, waiting):  # noqa: F811
    token = make(waiting, "reject")
    assert (
        "Rejeitado" in client.post(f"/a/{token}", headers=ACCESS, data={"note": "fora do tom"}).text
    )
    job = fetch("SELECT status, blocked_reason FROM publication_jobs WHERE id = :i", i=waiting)[0]
    assert job["status"] == "REJECTED" and job["blocked_reason"] == "fora do tom"


def test_sem_access_a_decisao_e_recusada(client, waiting):  # noqa: F811
    """Pedido pela rede interna (sem Cloudflare Access): o token sozinho não decide."""
    token = make(waiting)
    assert client.post(f"/a/{token}").status_code == 403
    assert status(waiting) == "AWAITING_APPROVAL"
    with session_scope() as db:
        assert links.lookup(db, token).job_id == waiting  # e o link não foi gasto


def test_link_invalido_ou_job_ja_decidido(client, access, waiting):  # noqa: F811
    assert client.get("/a/lixo", headers=ACCESS).status_code == 404
    assert client.post("/a/1.2.xx", headers=ACCESS).status_code == 404
    token = make(waiting)
    with session_scope() as db:  # alguém decide pelo painel entretanto
        from app.services import decisions

        assert (
            decisions.decide(db, waiting, "approved", user="outro@example.com", channel="panel")
            == ""
        )
    page = client.get(f"/a/{token}", headers=ACCESS)
    assert "já foi decidido" in page.text
    assert client.post(f"/a/{token}", headers=ACCESS).status_code == 200  # mostra "já decidido"
    assert len(fetch("SELECT 1 FROM approvals WHERE job_id = :i", i=waiting)) == 1


def test_a_pagina_nao_exige_a_sessao_do_painel(client, access, waiting):  # noqa: F811
    token = make(waiting)
    assert client.get(f"/a/{token}", headers=ACCESS, follow_redirects=False).status_code == 200
