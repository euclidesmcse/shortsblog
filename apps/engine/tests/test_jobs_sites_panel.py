"""E18.4b (Jobs) e E18.4c (Sites): telas operacionais, requeue com motivo obrigatório."""
from __future__ import annotations

import re
from urllib.parse import unquote_plus

import pytest
from sqlalchemy import text

from app.api.routes import sites_panel
from app.core.db import session_scope
from app.services import fetcher
from tests.conftest import advance, fetch, login_as
from tests.test_sites import cloud, insert_job  # noqa: F401  (fixture `cloud`)

DAY = "de=2026-10-04&ate=2026-10-06"


@pytest.fixture
def blocked(cloud) -> int:  # noqa: F811
    job = insert_job(cloud, 2, "PLANNED")
    advance(job, "QUEUED", "CLAIMED", "RESEARCHING")
    advance(job, "BLOCKED", blocked_reason="fontes insuficientes: 1 de 2")
    with session_scope() as db:
        for provider in ("claude", "claude", "codex"):
            db.execute(text("""INSERT INTO provider_usage (provider, kind, role, purpose, site,
                                   job_id, outcome)
                               VALUES (:p, 'subscription', 'researcher', 'research', 'cloud',
                                       :j, 'ok')"""), {"p": provider, "j": job})
    return job


def location(response) -> str:
    return unquote_plus(response.headers["location"])


# --- Jobs ------------------------------------------------------------------------------------
def test_lista_filtra_e_mostra_chamadas_e_motivo(panel_client, blocked):
    other = insert_job("cloud", 3, "PLANNED")
    page = panel_client.get(f"/dashboard/jobs?{DAY}&blog=cloud").text
    assert f'href="/dashboard/jobs/{blocked}"' in page and f'href="/dashboard/jobs/{other}"' in page
    assert "fontes insuficientes" in page and "claude 2 · codex 1" in page
    only_blocked = panel_client.get(f"/dashboard/jobs?{DAY}&grupo=bloqueados").text
    assert f'/dashboard/jobs/{blocked}"' in only_blocked
    assert f'/dashboard/jobs/{other}"' not in only_blocked
    assert f'/dashboard/jobs/{blocked}"' not in panel_client.get(
        "/dashboard/jobs?de=2026-11-01&ate=2026-11-02").text


def test_detalhe_mostra_eventos_e_chamadas(panel_client, blocked):
    page = panel_client.get(f"/dashboard/jobs/{blocked}").text
    assert "RESEARCHING" in page and "BLOCKED" in page and "Reenfileirar" in page
    assert re.search(r"<strong>3</strong>", page)  # total de chamadas
    missing = panel_client.get("/dashboard/jobs/999999", follow_redirects=False)
    assert "não existe" in location(missing)


def test_reenfileirar_exige_motivo_e_audita(panel_client, blocked):
    response = panel_client.post(f"/dashboard/jobs/{blocked}/requeue", data={"motivo": "  "},
                                 follow_redirects=False)
    assert "motivo" in location(response)
    assert fetch("SELECT status FROM publication_jobs WHERE id = :i", i=blocked)[0][
        "status"] == "BLOCKED"
    back = f"/dashboard/jobs?{DAY}&grupo=bloqueados"
    response = panel_client.post(f"/dashboard/jobs/{blocked}/requeue", follow_redirects=False,
                                 data={"motivo": "fontes novas no feed", "back": back})
    assert location(response).startswith(back) and "devolvido" in location(response)
    job = fetch("SELECT status, blocked_reason FROM publication_jobs WHERE id = :i", i=blocked)[0]
    assert (job["status"], job["blocked_reason"]) == ("QUEUED", None)
    event = fetch("SELECT details FROM publication_events WHERE job_id = :i AND "
                  "to_status = 'QUEUED' ORDER BY id DESC", i=blocked)[0]
    assert event["details"]["motivo"] == "fontes novas no feed"
    audit = fetch("SELECT actor, action, target, details FROM audit_log "
                  "WHERE action = 'job.requeue'")[0]
    assert (audit["actor"], audit["target"]) == ("euclides@example.com", f"job:{blocked}")
    again = panel_client.post(f"/dashboard/jobs/{blocked}/requeue", follow_redirects=False,
                              data={"motivo": "de novo"})
    assert "já não está BLOCKED" in location(again)


def test_volta_so_para_dentro_de_jobs(panel_client, blocked):
    response = panel_client.post(f"/dashboard/jobs/{blocked}/requeue", follow_redirects=False,
                                 data={"motivo": "teste de volta", "back": "https://evil.test/"})
    assert location(response).startswith(f"/dashboard/jobs/{blocked}?")


def test_operador_ve_e_reenfileira(client, blocked):
    login_as(client, "op@example.com", "operador")
    assert client.get(f"/dashboard/jobs?{DAY}").status_code == 200
    client.post(f"/dashboard/jobs/{blocked}/requeue", data={"motivo": "pelo operador"})
    assert fetch("SELECT actor FROM audit_log WHERE action = 'job.requeue'")[0]["actor"] == \
        "op@example.com"


# --- Sites -----------------------------------------------------------------------------------
@pytest.mark.parametrize("html,expected", [
    ("<meta name='robots' content='noindex, nofollow' />", True),
    ('<META NAME="robots" CONTENT="max-image-preview:large, noindex">', True),
    ("<meta name='robots' content='max-image-preview:large' />", False),
    ("<p>noindex é uma palavra</p>", False),
])
def test_noindex_na_home(html, expected):
    assert sites_panel.robots_noindex(html) is expected


def test_tela_de_sites_mostra_estado_e_indexavel(panel_client, cloud, monkeypatch):  # noqa: F811
    with session_scope() as db:
        db.execute(text("UPDATE sites SET enabled = true WHERE site_key IN ('cloud', 'ia')"))
    pages = {"https://cloud.shortsblog.com/":
             "<meta name='robots' content='max-image-preview:large'>",
             "https://ia.shortsblog.com/": "<meta name='robots' content='noindex, nofollow'>"}
    calls: list[str] = []

    def fake(url, **kwargs):
        calls.append(url)
        if url not in pages:
            raise fetcher.FetchError("network_error", url)
        return fetcher.Fetched(url=url, status=200, content_type="text/html",
                               body=pages[url].encode())

    monkeypatch.setattr(sites_panel.fetcher, "fetch", fake)
    page = panel_client.get("/dashboard/sites").text
    assert "Cloud e Infraestrutura" in page and "indexável" in page and "noindex" in page
    assert 'href="/dashboard/config/sites/cloud"' in page          # admin vê «Editar»
    panel_client.get("/dashboard/sites")
    assert len(calls) == 2                                         # cache de 10 min no Redis


def test_homes_sao_lidas_ao_mesmo_tempo(monkeypatch):
    """Revisão E18: com o cache frio, as homes eram lidas uma a uma (até 8 s cada)."""
    import threading

    monkeypatch.setattr(sites_panel.redis_client, "get", lambda key: None)
    monkeypatch.setattr(sites_panel.redis_client, "set", lambda *a, **k: None)
    barrier = threading.Barrier(3, timeout=5)      # só passa se as 3 leituras estiverem juntas

    def fake(url, **kwargs):
        barrier.wait()
        return fetcher.Fetched(url=url, status=200, content_type="text/html", body=b"<p>ok</p>")

    monkeypatch.setattr(sites_panel.fetcher, "fetch", fake)
    targets = [(k, f"https://{k}.shortsblog.com") for k in ("cloud", "ia", "saude")]
    assert sites_panel.indexable_all(targets) == {"cloud": True, "ia": True, "saude": True}
    assert sites_panel.indexable_all([]) == {}


def test_operador_ve_sites_sem_links_de_edicao(client, cloud, monkeypatch):  # noqa: F811
    monkeypatch.setattr(sites_panel, "indexable", lambda *a: None)
    login_as(client, "op@example.com", "operador")
    page = client.get("/dashboard/sites").text
    assert "Sites" in page and "/dashboard/config/sites/" not in page


def test_menu_tem_jobs_e_sites(panel_client):
    page = panel_client.get("/").text
    assert 'href="/dashboard/jobs"' in page and 'href="/dashboard/sites"' in page
