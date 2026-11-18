"""E10.2 — painel "Pauta" (WF-01); E18.2 — fontes opcionais, intemporal, descarte e auditoria."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.core.db import session_scope
from app.services import discovery
from tests.conftest import fetch
from tests.test_sites import cloud  # noqa: F401  (fixture `cloud`: os sites sincronizados)

A = "https://agenciabrasil.ebc.com.br/politica/noticia/2026-10/e-titulo"
B = "https://www.camara.leg.br/noticias/1308107-eleicoes-2026-e-titulo"


def post(client, **data):
    form = {
        "site_key": "cloud",
        "topic": "Como funciona o e-Título e como votar com ele",
        "angle": "",
        "sources": A,
        "priority": "normal",
        "ttl_days": "7",
        **data,
    }
    return client.post("/dashboard/ideas", data=form, follow_redirects=False)


@pytest.fixture
def busca(monkeypatch):
    """Buscador do motor ligado (D-063): a pauta pode vir sem fontes."""
    monkeypatch.setattr(settings, "searxng_url", "http://searxng.test")


def ideas() -> list[dict]:
    return fetch("SELECT * FROM ideas WHERE origin = 'manual' ORDER BY id")


def test_exige_sessao_do_painel(client, cloud):  # noqa: F811
    assert (
        client.get("/dashboard/ideas", follow_redirects=False)
        .headers["location"]
        .startswith("/login")
    )
    assert post(client).headers["location"].startswith("/login")
    assert ideas() == []


def test_pagina_mostra_blogs_formulario_e_minimo_de_fontes(panel_client, cloud):  # noqa: F811
    page = panel_client.get("/dashboard/ideas").text
    assert "<h1>Pauta</h1>" in page and 'name="sources"' in page and 'name="ttl_days"' in page
    for key in ("cloud", "politica", "culinaria"):
        assert key in page
    assert 'data-min="2"' in page


def test_acrescenta_ideia_com_duas_fontes_prioridade_e_validade(panel_client, cloud):  # noqa: F811
    response = post(
        panel_client, sources=f"{A}\n{B}", priority="maxima", ttl_days="14", angle="só serviço"
    )
    assert response.status_code == 303 and "msg=" in response.headers["location"]
    rows = ideas()
    assert len(rows) == 2
    first, second = rows
    assert first["canonical_url"] == A and first["duplicate_of"] is None
    assert second["canonical_url"] == discovery.canonical_url(B)
    assert second["duplicate_of"] == first["id"]  # a 2.ª é fonte extra
    assert first["priority"] == discovery.MANUAL_PRIORITY + 100 and first["angle"] == "só serviço"
    assert first["created_by"] == "euclides@example.com"
    days = (first["expires_at"] - first["created_at"]).days
    assert days in (13, 14)
    page = panel_client.get("/dashboard/ideas").text
    assert "Como funciona o e-Título" in page and "euclides@example.com" in page


@pytest.mark.parametrize(
    "data",
    [
        {"sources": ""},  # sem fontes
        {"sources": "http://exemplo.test/a"},  # não é https
        {"sources": " ".join(f"https://exemplo.test/{n}" for n in range(6))},  # mais de 5
        {"topic": "curto"},  # tema curto
        {"site_key": "inexistente"},
        {"priority": "urgentissima"},
        {"ttl_days": "365"},
    ],
)
def test_entradas_invalidas_sao_recusadas_sem_gravar(panel_client, cloud, data):  # noqa: F811
    response = post(panel_client, **data)
    assert response.status_code == 303 and "err=" in response.headers["location"]
    assert ideas() == []


def test_ligacao_repetida_e_ignorada(panel_client, cloud):  # noqa: F811
    post(panel_client, sources=A)
    response = post(panel_client, sources=A)
    assert "err=" in response.headers["location"]  # tudo já existia
    response = post(panel_client, topic="Outro tema diferente sobre votação", sources=f"{A}\n{B}")
    assert "ignorada" in response.headers["location"] or "ignorada" in __import__(
        "urllib.parse"
    ).parse.unquote(response.headers["location"])
    assert len(ideas()) == 2


def test_a_mais_prioritaria_sai_primeiro_na_escolha(panel_client, cloud):  # noqa: F811
    post(panel_client, topic="Tema normal sobre cadastro eleitoral", sources=A, priority="normal")
    post(panel_client, topic="Tema urgente sobre o voto em trânsito", sources=B, priority="maxima")
    with session_scope() as db:
        picked = discovery.pick(db, "cloud")
    assert picked is not None and picked.topic.startswith("Tema urgente")


def test_painel_principal_liga_a_pagina(panel_client):
    assert 'href="/dashboard/ideas"' in panel_client.get("/").text


# --- E18.2 -------------------------------------------------------------------------------
def test_sem_buscador_a_fonte_continua_obrigatoria(panel_client, cloud):  # noqa: F811
    page = panel_client.get("/dashboard/ideas").text
    assert 'name="timeless"' not in page and "buscador" in page
    response = post(panel_client, sources="")
    assert "err=" in response.headers["location"] and ideas() == []


def test_com_buscador_a_pauta_entra_so_com_o_tema(panel_client, cloud, busca):  # noqa: F811
    page = panel_client.get("/dashboard/ideas").text
    assert "opcional" in page and 'name="timeless"' in page
    response = post(panel_client, sources="", timeless="true",
                    topic="O que muda com a nova diretriz da OMS sobre antibióticos")
    assert "msg=" in response.headers["location"]
    row = ideas()[0]
    assert row["canonical_url"] is None and row["source_urls"] == [] and row["timeless"] is True
    assert row["priority"] == discovery.MANUAL_PRIORITY
    page = panel_client.get("/dashboard/ideas").text
    assert ">busca<" in page and "intemporal" in page


def test_sem_fonte_e_tema_parecido_e_recusado_sem_gravar(panel_client, cloud, busca):  # noqa: F811
    post(panel_client, sources=A, topic="Como funciona o voto em trânsito nas eleições")
    response = post(panel_client, sources="", topic="Como funciona o voto em trânsito nas eleições")
    from urllib.parse import unquote
    assert "parecida" in unquote(response.headers["location"])
    assert len(ideas()) == 1


def test_criar_e_descartar_ficam_na_auditoria(panel_client, cloud):  # noqa: F811
    post(panel_client, sources=A)
    idea_id = ideas()[0]["id"]
    response = panel_client.post(f"/dashboard/ideas/{idea_id}/discard", follow_redirects=False)
    assert "msg=" in response.headers["location"]
    assert fetch("SELECT status FROM ideas WHERE id = :i", i=idea_id)[0]["status"] == "discarded"
    log = fetch("SELECT actor, action, target, details FROM audit_log "
                "WHERE action LIKE 'idea.%' ORDER BY id")
    assert [r["action"] for r in log] == ["idea.create", "idea.discard"]
    assert all(r["actor"] == "euclides@example.com" and r["target"] == f"idea:{idea_id}"
               for r in log)
    assert log[0]["details"]["sources"] == 1 and log[0]["details"]["site"] == "cloud"


def test_so_descarta_pauta_manual_ainda_na_fila(panel_client, cloud):  # noqa: F811
    post(panel_client, sources=A)
    idea_id = ideas()[0]["id"]
    with session_scope() as db:
        db.execute(text("UPDATE ideas SET status = 'taken' WHERE id = :i"), {"i": idea_id})
        rss = discovery.store_idea(db, site_key="cloud", origin="rss",
                                   topic="Outra história do feed do blog", url=B)[1]
    for target in (idea_id, rss, 999999):
        response = panel_client.post(f"/dashboard/ideas/{target}/discard", follow_redirects=False)
        assert "err=" in response.headers["location"]
    assert fetch("SELECT status FROM ideas WHERE id = :i", i=idea_id)[0]["status"] == "taken"
    assert fetch("SELECT status FROM ideas WHERE id = :i", i=rss)[0]["status"] == "new"


def test_operador_cria_e_descarta_pauta(client, cloud):  # noqa: F811
    from tests.conftest import login_as
    login_as(client, "op@example.com", "operador")
    post(client, sources=A)
    idea_id = ideas()[0]["id"]
    assert ideas()[0]["created_by"] == "op@example.com"
    client.post(f"/dashboard/ideas/{idea_id}/discard")
    assert fetch("SELECT status FROM ideas WHERE id = :i", i=idea_id)[0]["status"] == "discarded"


def test_menu_chama_a_tela_de_pauta(panel_client):
    page = panel_client.get("/").text
    assert re.search(r'<a href="/dashboard/ideas"[^>]*>Pauta</a>', page)
    assert "Nova ideia" not in page
