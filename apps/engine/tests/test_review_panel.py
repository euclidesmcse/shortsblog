"""E10.1 — painel "Aprovações": ler e decidir os jobs em AWAITING_APPROVAL."""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from app.core.db import session_scope
from tests.conftest import advance, fetch
from tests.test_sites import cloud, insert_job  # noqa: F401  (fixture `cloud`)


@pytest.fixture
def waiting(cloud) -> int:  # noqa: F811
    job = insert_job(cloud, 1, "PLANNED")
    advance(job, "QUEUED", "CLAIMED", "RESEARCHING", "WRITING", "VALIDATING", "TRANSLATING",
            "AWAITING_APPROVAL")
    with session_scope() as db:
        db.execute(text("UPDATE publication_jobs SET review_report = CAST(:r AS JSONB) "
                        "WHERE id = :i"),
                   {"r": json.dumps({"score": 94, "sources": 3, "notes": ["Nota do revisor"]}),
                    "i": job})
        for lang, title in (("pt-BR", "Título <b>pt</b>"), ("en", "Title en"),
                            ("es", "Título es"), ("zh-CN", "标题")):
            db.execute(text("INSERT INTO job_translations (job_id, language, is_original, title, "
                            "html, status) VALUES (:j, :l, :o, :t, :h, 'ready')"),
                       {"j": job, "l": lang, "o": lang == "pt-BR", "t": title,
                        "h": "<p>corpo</p><script>alert(1)</script>"})
        db.execute(text("INSERT INTO article_evidence (job_id, source_url, source_name) "
                        "VALUES (:j, 'https://exemplo.test/a', 'Fonte A')"), {"j": job})
    return job


def post(client, path: str, **data):
    return client.post(f"/dashboard/review{path}", data=data, follow_redirects=False)


def test_exige_sessao_do_painel(client, waiting):
    assert client.get("/dashboard/review", follow_redirects=False).headers[
        "location"].startswith("/login")
    assert post(client, f"/{waiting}/approve").headers["location"].startswith("/login")
    assert fetch("SELECT status FROM publication_jobs WHERE id = :i", i=waiting)[0][
        "status"] == "AWAITING_APPROVAL"


def test_fila_lista_o_job_e_a_pagina_mostra_os_4_idiomas(panel_client, waiting):
    page = panel_client.get("/dashboard/review").text
    assert f"/dashboard/review/{waiting}" in page and "94" in page
    assert "&lt;b&gt;pt&lt;/b&gt;" in page  # título escapado
    detail = panel_client.get(f"/dashboard/review/{waiting}").text
    for label in ("Português", "English", "Español", "中文"):
        assert label in detail
    assert "Fonte A" in detail and "Nota do revisor" in detail
    # o corpo vai num iframe sem permissões: o script nunca corre no painel
    assert "<iframe sandbox" in detail and "<script>alert(1)" not in detail


def test_job_inexistente_da_404(panel_client):
    assert panel_client.get("/dashboard/review/99999").status_code == 404


def test_aprovar_regista_quem_e_muda_o_estado(panel_client, waiting):
    response = post(panel_client, f"/{waiting}/approve", note="ok")
    assert response.status_code == 303 and "msg=" in response.headers["location"]
    assert fetch("SELECT status FROM publication_jobs WHERE id = :i", i=waiting)[0][
        "status"] == "APPROVED"
    row = fetch("SELECT decision, decided_by, channel, note FROM approvals WHERE job_id = :i",
                i=waiting)[0]
    assert (row["decision"], row["decided_by"], row["channel"], row["note"]) == (
        "approved", "euclides@example.com", "panel", "ok")


def test_rejeitar_guarda_o_motivo(panel_client, waiting):
    post(panel_client, f"/{waiting}/reject", note="fora do tom")
    job = fetch("SELECT status, blocked_reason FROM publication_jobs WHERE id = :i", i=waiting)[0]
    assert job["status"] == "REJECTED" and job["blocked_reason"] == "fora do tom"
    assert fetch("SELECT decision FROM approvals WHERE job_id = :i", i=waiting)[0][
        "decision"] == "rejected"


def test_segunda_decisao_e_recusada_sem_deixar_rasto(panel_client, waiting):
    post(panel_client, f"/{waiting}/approve")
    response = post(panel_client, f"/{waiting}/reject")
    assert "err=" in response.headers["location"]
    assert fetch("SELECT status FROM publication_jobs WHERE id = :i", i=waiting)[0][
        "status"] == "APPROVED"
    assert len(fetch("SELECT 1 FROM approvals WHERE job_id = :i", i=waiting)) == 1


def test_painel_principal_liga_a_pagina(panel_client):
    assert 'href="/dashboard/review"' in panel_client.get("/").text


def test_publicar_poe_na_fila_so_jobs_aprovados(panel_client, waiting, monkeypatch):
    from app.api.routes import review

    queued: list[int] = []
    monkeypatch.setattr(review, "_enqueue_publish", queued.append)
    # À espera de aprovação: recusado, nada na fila.
    response = post(panel_client, f"/{waiting}/publish")
    assert "err=" in response.headers["location"] and queued == []
    post(panel_client, f"/{waiting}/approve")
    assert "Publicação no WordPress" in panel_client.get(f"/dashboard/review/{waiting}").text
    response = post(panel_client, f"/{waiting}/publish")
    assert "msg=" in response.headers["location"] and queued == [waiting]
    event = fetch("SELECT actor, details FROM publication_events WHERE job_id = :i "
                  "AND event_type = 'publish_requested'", i=waiting)[0]
    assert event["actor"] == "euclides@example.com" and event["details"]["mode"] == "draft"
    page = panel_client.get(f"/dashboard/review/{waiting}").text
    assert "Criar rascunhos no WordPress" in page and "pedido" in page


def test_publicar_exige_sessao(client, waiting):
    assert post(client, f"/{waiting}/publish").headers["location"].startswith("/login")


def test_segundo_clique_em_publicar_e_ignorado(panel_client, waiting, monkeypatch):
    from app.api.routes import review

    queued: list[int] = []
    monkeypatch.setattr(review, "_enqueue_publish", queued.append)
    post(panel_client, f"/{waiting}/approve")
    post(panel_client, f"/{waiting}/publish")
    again = post(panel_client, f"/{waiting}/publish")
    assert "err=" in again.headers["location"] and queued == [waiting]


def _extra_waiting(cloud, slot: int) -> int:  # noqa: F811
    job = insert_job(cloud, slot, "PLANNED")
    advance(job, "QUEUED", "CLAIMED", "RESEARCHING", "WRITING", "VALIDATING", "TRANSLATING",
            "AWAITING_APPROVAL")
    return job


def test_lote_aprova_so_os_selecionados_com_uma_decisao_cada(
        panel_client, waiting, cloud):  # noqa: F811
    other = _extra_waiting(cloud, 2)
    untouched = _extra_waiting(cloud, 3)
    response = panel_client.post("/dashboard/review/batch", follow_redirects=False,
                                 data={"action": "approve", "ids": [str(waiting), str(other)]})
    assert "msg=" in response.headers["location"]
    status = {r["id"]: r["status"] for r in fetch(
        "SELECT id, status FROM publication_jobs WHERE id = ANY(:i)",
        i=[waiting, other, untouched])}
    assert status == {waiting: "APPROVED", other: "APPROVED", untouched: "AWAITING_APPROVAL"}
    rows = fetch("SELECT job_id, decided_by, channel FROM approvals ORDER BY job_id")
    assert [(r["job_id"], r["decided_by"], r["channel"]) for r in rows] == [
        (waiting, "euclides@example.com", "panel"), (other, "euclides@example.com", "panel")]


def test_lote_salta_os_ja_decididos_sem_travar_os_outros(
        panel_client, waiting, cloud):  # noqa: F811
    other = _extra_waiting(cloud, 2)
    post(panel_client, f"/{waiting}/approve")
    response = panel_client.post("/dashboard/review/batch", follow_redirects=False,
                                 data={"action": "approve", "ids": [str(waiting), str(other)]})
    location = response.headers["location"]
    assert "msg=" in location and "err=" in location
    assert fetch("SELECT status FROM publication_jobs WHERE id = :i", i=other)[0][
        "status"] == "APPROVED"
    assert len(fetch("SELECT 1 FROM approvals WHERE job_id = :i", i=waiting)) == 1


def test_lote_rejeita_e_valida_a_entrada(panel_client, waiting):
    assert "err=" in panel_client.post("/dashboard/review/batch", follow_redirects=False,
                                       data={"action": "approve"}).headers["location"]
    assert "err=" in panel_client.post("/dashboard/review/batch", follow_redirects=False,
                                       data={"action": "apagar", "ids": [str(waiting)]}
                                       ).headers["location"]
    panel_client.post("/dashboard/review/batch", data={"action": "reject", "ids": [str(waiting)]})
    assert fetch("SELECT status FROM publication_jobs WHERE id = :i", i=waiting)[0][
        "status"] == "REJECTED"


def test_lote_de_rascunhos_so_poe_na_fila_os_aprovados(
        panel_client, waiting, cloud, monkeypatch):  # noqa: F811
    from app.api.routes import review

    queued: list[int] = []
    monkeypatch.setattr(review, "_enqueue_publish", queued.append)
    pending = _extra_waiting(cloud, 2)
    post(panel_client, f"/{waiting}/approve")
    response = panel_client.post("/dashboard/review/batch", follow_redirects=False,
                                 data={"action": "publish", "ids": [str(waiting), str(pending)]})
    assert queued == [waiting] and "msg=" in response.headers["location"]
    assert "err=" in response.headers["location"]
    page = panel_client.get("/dashboard/review").text
    assert "Aprovados, sem publicar" in page and "Aprovar selecionados" in page


def test_lote_exige_sessao(client, waiting):
    response = client.post("/dashboard/review/batch", follow_redirects=False,
                           data={"action": "approve", "ids": [str(waiting)]})
    assert response.headers["location"].startswith("/login")
    assert fetch("SELECT status FROM publication_jobs WHERE id = :i", i=waiting)[0][
        "status"] == "AWAITING_APPROVAL"


def _due_tomorrow(job: int) -> None:
    with session_scope() as db:
        db.execute(text("UPDATE publication_jobs SET due_at_utc = now() + interval '1 day' "
                        "WHERE id = :i"), {"i": job})


def test_live_antes_do_horario_so_publica_com_confirmacao(
        panel_client, waiting, monkeypatch):
    """Aprovar basta: o agendador publica à hora do slot (D-066). O clique distraído é recusado."""
    from app.api.routes import review
    from app.core.config import settings

    queued: list[int] = []
    monkeypatch.setattr(review, "_enqueue_publish", queued.append)
    monkeypatch.setattr(settings, "wp_publish_mode", "live")
    _due_tomorrow(waiting)
    approved = post(panel_client, f"/{waiting}/approve")
    assert "Publica+sozinho+em" in approved.headers["location"]
    page = panel_client.get(f"/dashboard/review/{waiting}").text
    assert "Agendado:" in page and "Publicar antes do horário" in page
    assert "Publicar agora</button>" not in page
    refused = post(panel_client, f"/{waiting}/publish")
    assert "err=" in refused.headers["location"] and queued == []
    assert fetch("SELECT 1 FROM publication_events WHERE job_id = :i "
                 "AND event_type = 'publish_requested'", i=waiting) == []
    batch = panel_client.post("/dashboard/review/batch", follow_redirects=False,
                              data={"action": "publish", "ids": [str(waiting)]})
    assert "err=" in batch.headers["location"] and queued == []
    assert "(automático)" in panel_client.get("/dashboard/review").text
    early = post(panel_client, f"/{waiting}/publish", early="1")
    assert "msg=" in early.headers["location"] and queued == [waiting]
    event = fetch("SELECT details FROM publication_events WHERE job_id = :i "
                  "AND event_type = 'publish_requested'", i=waiting)[0]
    assert event["details"]["early"] is True and event["details"]["channel"] == "panel"


def test_live_depois_do_horario_publica_com_um_clique(panel_client, waiting, monkeypatch):
    from app.api.routes import review
    from app.core.config import settings

    queued: list[int] = []
    monkeypatch.setattr(review, "_enqueue_publish", queued.append)
    monkeypatch.setattr(settings, "wp_publish_mode", "live")
    post(panel_client, f"/{waiting}/approve")            # slot de 05/10: já passou
    assert "Publicar agora</button>" in panel_client.get(f"/dashboard/review/{waiting}").text
    assert "msg=" in post(panel_client, f"/{waiting}/publish").headers["location"]
    assert queued == [waiting]
    assert fetch("SELECT details FROM publication_events WHERE job_id = :i "
                 "AND event_type = 'publish_requested'", i=waiting)[0]["details"]["early"] is False


# ---------------------------------------------------------------------------
# Botão «Devolver à fila» (jobs BLOCKED; pedido do Euclides em 9/11)
# ---------------------------------------------------------------------------

@pytest.fixture
def blocked(cloud) -> int:  # noqa: F811
    job = insert_job(cloud, 2, "PLANNED")
    advance(job, "QUEUED", "CLAIMED", "RESEARCHING")
    advance(job, "BLOCKED", blocked_reason="sem ideias na fila")
    with session_scope() as db:
        db.execute(text("UPDATE publication_jobs SET retry_after_utc = now() + interval '2 hours' "
                        "WHERE id = :i"), {"i": job})
    return job


def test_devolver_a_fila_regista_quem_e_limpa_o_motivo(panel_client, blocked):
    page = panel_client.get(f"/dashboard/review/{blocked}").text
    assert "Devolver à fila" in page
    response = post(panel_client, f"/{blocked}/requeue", note="quero tentar de novo")
    assert response.status_code == 303 and "msg=" in response.headers["location"]
    job = fetch("SELECT status, blocked_reason, retry_after_utc FROM publication_jobs "
                "WHERE id = :i", i=blocked)[0]
    assert (job["status"], job["blocked_reason"], job["retry_after_utc"]) == ("QUEUED", None, None)
    event = fetch("SELECT actor, details FROM publication_events WHERE job_id = :i "
                  "AND to_status = 'QUEUED' ORDER BY id DESC", i=blocked)[0]
    assert event["actor"] == "euclides@example.com"
    assert event["details"]["motivo"] == "quero tentar de novo"


def test_devolver_a_fila_so_vale_para_blocked(panel_client, waiting, blocked):
    # Num job à espera de aprovação: recusado, estado intacto, sem botão na página.
    response = post(panel_client, f"/{waiting}/requeue")
    assert "err=" in response.headers["location"]
    assert fetch("SELECT status FROM publication_jobs WHERE id = :i", i=waiting)[0][
        "status"] == "AWAITING_APPROVAL"
    assert "Devolver à fila" not in panel_client.get(f"/dashboard/review/{waiting}").text
    # Sem motivo: recusado (E18.4b), o job continua BLOCKED.
    response = post(panel_client, f"/{blocked}/requeue")
    assert "err=" in response.headers["location"]
    assert fetch("SELECT status FROM publication_jobs WHERE id = :i", i=blocked)[0][
        "status"] == "BLOCKED"
    # Repetir a devolução não duplica: a segunda é recusada.
    post(panel_client, f"/{blocked}/requeue", note="tentar de novo")
    response = post(panel_client, f"/{blocked}/requeue", note="tentar de novo")
    assert "err=" in response.headers["location"]
    assert len(fetch("SELECT 1 FROM publication_events WHERE job_id = :i "
                     "AND to_status = 'QUEUED' AND from_status = 'BLOCKED'", i=blocked)) == 1
    assert post(panel_client, "/99999/requeue").headers["location"].count("err=") == 1


def test_devolver_a_fila_exige_sessao_do_painel(client, blocked):
    assert post(client, f"/{blocked}/requeue").headers["location"].startswith("/login")
    assert fetch("SELECT status FROM publication_jobs WHERE id = :i", i=blocked)[0][
        "status"] == "BLOCKED"
