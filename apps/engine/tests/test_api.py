"""API HTTP: saúde, autenticação, painel e mídia."""
from __future__ import annotations

from app.core.config import settings
from tests.conftest import fetch


# --- saúde e métricas -------------------------------------------------------
def test_health(client):
    assert client.get("/health/live").json() == {"status": "ok", "service": "sb-engine"}
    ready = client.get("/health/ready").json()
    assert ready["status"] == "ok"
    assert ready["checks"]["postgres"] == "ok" and ready["checks"]["redis"] == "ok"
    assert "qdrant" not in ready["checks"]
    assert "google" not in ready["checks"] and "esp" not in ready["checks"]


def test_metrics_sb(client):
    body = client.get("/metrics").text
    assert "sb_llm_cost_usd_30d" in body and "growth_" not in body


def test_openapi_sem_rotas_removidas(panel_client):
    client = panel_client
    paths = client.get("/openapi.json").json()["paths"]
    for removed in ("/go/{placement_id}", "/lp/{slug}", "/api/keywords", "/api/bi/kpis",
                    "/hooks/unsubscribe/{token}", "/api/articles/{article_id}/monetize",
                    # E19: o pipeline antigo saiu (fases, artigos e a revisão de artigos).
                    "/api/pipeline/{phase}", "/api/articles",
                    "/dashboard/articles/{article_id}"):
        assert removed not in paths
    assert "/media/{filename}" in paths


# --- autenticação -----------------------------------------------------------
def test_api_exige_token(client, auth, monkeypatch):
    assert client.get("/api/ai/chain").status_code == 401
    assert client.get("/api/ai/chain", headers={"Authorization": "Bearer x"}).status_code == 401
    assert client.get("/api/ai/chain", headers=auth).status_code == 200
    monkeypatch.setattr(settings, "sb_api_token", "")
    assert client.get("/api/ai/chain", headers=auth).status_code == 503


# --- painel -----------------------------------------------------------------
def _cloud_jobs() -> list[int]:
    """Dois jobs do cloud: um publicado e um à espera de aprovação, com título em pt-BR."""
    from datetime import UTC, date, datetime

    from sqlalchemy import text

    from app.core.db import session_scope
    from app.services import planner, sites
    from tests.conftest import advance

    with session_scope() as db:
        sites.sync(db)
        db.execute(text("UPDATE sites SET enabled = true, daily_target = 2 "
                        "WHERE site_key = 'cloud'"))
        planner.plan_day(db, date(2026, 10, 20), now=datetime(2026, 10, 19, 12, tzinfo=UTC))
    ids = [r["id"] for r in fetch("SELECT id FROM publication_jobs ORDER BY slot_number")]
    production = ("QUEUED", "CLAIMED", "RESEARCHING", "WRITING", "VALIDATING", "TRANSLATING",
                  "AWAITING_APPROVAL")
    advance(ids[0], *production)
    advance(ids[0], "APPROVED")
    advance(ids[0], "PUBLISHING")
    advance(ids[0], "PUBLISHED_CONFIRMED", wordpress_url="https://cloud.shortsblog.com/publicado/",
            published_at_utc=datetime.now(UTC))
    advance(ids[1], *production)
    with session_scope() as db:
        for job, title in zip(ids, ("Post publicado", "Post à espera"), strict=True):
            db.execute(text("INSERT INTO job_translations (job_id, language, is_original, title, "
                            "html, status) VALUES (:j, 'pt-BR', true, :t, '<p>x</p>', 'ready')"),
                       {"j": job, "t": title})
    return ids


def test_painel(panel_client):
    published, waiting = _cloud_jobs()
    page = panel_client.get("/")
    assert page.status_code == 200
    html = page.text
    # Os números vêm dos jobs de publicação (não das tabelas antigas do Growth Engine).
    assert '<div class="value">1</div>\n      <div class="sub">nos últimos 30 dias' in html
    assert f'href="/dashboard/review/{waiting}">Post à espera' in html
    assert 'href="https://cloud.shortsblog.com/publicado/"' in html and "Post publicado" in html
    # Horários no fuso da instalação (TZ; PANEL_TIMEZONES vazio): sem Lisboa fixa (E19.6).
    assert "Sao Paulo" in html and "Lisboa" not in html
    assert "ligado · 2/dia" in html and "desligado" in html
    assert "Receita" not in html and "EPC" not in html and "chart.umd" not in html


# --- mídia ------------------------------------------------------------------
def test_media_serve_ficheiro_e_bloqueia_traversal(client, _media_dir):
    _media_dir.mkdir(parents=True, exist_ok=True)
    (_media_dir / "capa.jpg").write_bytes(b"jpeg")
    response = client.get("/media/capa.jpg")
    assert response.status_code == 200 and response.content == b"jpeg"
    assert "max-age" in response.headers["cache-control"]
    assert client.get("/media/..%2f..%2fetc%2fpasswd").status_code == 404
    assert client.get("/media/nao-existe.jpg").status_code == 404


def test_media_serve_capa_por_ia_na_subpasta_illustrations(client, _media_dir):
    (_media_dir / "illustrations").mkdir(parents=True, exist_ok=True)
    (_media_dir / "illustrations" / "job-1-abc.webp").write_bytes(b"webp")
    response = client.get("/media/job-1-abc.webp")
    assert response.status_code == 200 and response.content == b"webp"
    assert client.get("/media/illustrations%2f..%2f..%2fetc%2fpasswd").status_code == 404


# --- E19.6: fusos do portal configuráveis -----------------------------------------------------
def test_horarios_do_portal_no_fuso_configurado(monkeypatch):
    from datetime import UTC, datetime

    from app.api.routes.dashboard import panel_zones, when_local

    moment = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(settings, "panel_timezones", "")
    monkeypatch.setattr(settings, "tz", "Asia/Tokyo")
    assert when_local(moment) == "18/11 21:00 Tokyo"
    # A nossa produção: Lisboa com data e São Paulo ao lado, como antes da E19.6.
    monkeypatch.setattr(settings, "panel_timezones", "Europe/Lisbon=Lisboa, America/Sao_Paulo=SP")
    assert when_local(moment) == "18/11 12:00 Lisboa · 09:00 SP"
    assert when_local(None) == "—"
    # Fuso inválido é ignorado; sem nenhum válido, UTC (a tela não quebra).
    assert [label for _, label in panel_zones("Marte/Olympus,Europe/Paris")] == ["Paris"]
    assert [label for _, label in panel_zones("Marte/Olympus")] == ["UTC"]
