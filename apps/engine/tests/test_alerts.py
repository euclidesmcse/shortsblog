"""E12.2/E12.3 — sondas dos blogs e alertas por e-mail."""

from __future__ import annotations

import time
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text

from app.core.cache import redis_client
from app.core.config import settings
from app.core.db import session_scope
from app.services import alerts, probes
from app.services.notify import mail
from tests.conftest import advance
from tests.test_sites import cloud, insert_job  # noqa: F401  (fixture `cloud`: os 6 sites)

T0 = 1_800_000_000.0


class Outbox:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.error: Exception | None = None

    def __call__(self, to: str, name: str, subject: str, html: str) -> str:
        if self.error:
            raise self.error
        self.sent.append((subject, html))
        return "ref"


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "approval_notify_email", "euclides@example.com")
    monkeypatch.setattr(settings, "public_base_url", "https://painel.exemplo.test")


def firing(*items: alerts.Firing):
    return {"teste": lambda db: list(items)}


def never(db):
    return []


# --- Sondas ---------------------------------------------------------------------------------


def serve(monkeypatch, handler):
    monkeypatch.setattr(probes, "_transport", httpx.MockTransport(handler))


def test_o_placeholder_do_traefik_conta_como_fora_do_ar(monkeypatch):
    """Com o WordPress parado o Traefik serve "em construção" com 200; isso é o blog em baixo."""
    page = "<html><title>ShortsBlog — Em Construção</title></html>"
    serve(monkeypatch, lambda r: httpx.Response(200, text=page))
    result = probes.fetch("https://x.test/?sbprobe=1")
    assert result["ok"] is False and result["status"] == 200 and "placeholder" in result["error"]
    serve(monkeypatch, lambda r: httpx.Response(200, text="<html><title>Cloud</title></html>"))
    assert probes.fetch("https://x.test/")["ok"] is True


def test_fetch_ok_erro_http_redirecionamento_e_falha_de_ligacao(monkeypatch):
    serve(
        monkeypatch,
        lambda r: httpx.Response({"/ok": 200, "/moved": 301, "/boom": 503}.get(r.url.path, 404)),
    )
    assert probes.fetch("https://x.test/ok")["ok"] is True
    assert probes.fetch("https://x.test/moved")["ok"] is True  # não segue; 3xx conta como vivo
    bad = probes.fetch("https://x.test/boom")
    assert bad["ok"] is False and bad["status"] == 503

    def down(request):
        raise httpx.ConnectError("sem rota")

    serve(monkeypatch, down)
    result = probes.fetch("https://x.test/")
    assert result["ok"] is False and result["status"] == 0 and result["error"] == "ConnectError"


def test_targets_run_e_desde_quando_falha(cloud, configured, monkeypatch):  # noqa: F811
    # Relógio fixo: o carimbo `sbprobe` muda de segundo para segundo, e o `run` volta a
    # gerar os endereços (falhava quando o segundo virava entre as duas chamadas).
    monkeypatch.setattr(probes.time, "time", lambda: 1_795_000_000)
    with session_scope() as db:
        urls = probes.targets(db)
    assert set(urls) == {"cloud", "ia", "financas", "saude", "politica", "culinaria", "painel"}
    assert urls["painel"] == "https://painel.exemplo.test/health/live"
    assert (
        "?sbprobe=" in urls["cloud"] and "sbprobe" not in urls["painel"]
    )  # fura a cache dos blogs
    answers = {"cloud": False}
    by_url = {url: name for name, url in urls.items()}

    def fake(url):
        return {"ok": answers.get(by_url[url], True), "status": 200, "ms": 5, "error": ""}

    with session_scope() as db:
        first = probes.run(db, T0, fake)
        again = probes.run(db, T0 + 60, fake)
    assert first["cloud"]["fail_since"] == T0 and again["cloud"]["fail_since"] == T0  # mantém
    assert first["ia"]["fail_since"] is None
    answers["cloud"] = True
    with session_scope() as db:
        assert probes.run(db, T0 + 120, fake)["cloud"]["fail_since"] is None  # recuperou


def test_certificado_com_cache_e_falha_de_ligacao_nao_conta():
    calls = []

    def fetcher(host):
        calls.append(host)
        if host == "b.test":
            raise OSError("sem rede")
        return 30.0

    first = probes.cert_days(["a.test", "b.test"], T0, fetcher)
    assert first == {"a.test": 30.0} and calls == ["a.test", "b.test"]
    probes.cert_days(["a.test"], T0 + 3600, fetcher)  # dentro das 6 h: da cache
    assert calls.count("a.test") == 1


# --- Alertas -------------------------------------------------------------------------------------


def test_so_avisa_depois_do_tempo_repete_de_6_em_6_horas_e_avisa_a_recuperacao(configured):
    out = Outbox()
    rule = firing(alerts.Firing("site_down:cloud", "cloud fora do ar", "HTTP 503", for_s=120))
    assert alerts.evaluate(T0, out, rule)["sent"] == []  # acabou de falhar
    assert alerts.evaluate(T0 + 60, out, rule)["sent"] == []  # ainda dentro dos 2 min
    assert alerts.evaluate(T0 + 130, out, rule)["sent"] == ["site_down:cloud"]
    assert "cloud fora do ar" in out.sent[0][0] and "HTTP 503" in out.sent[0][1]
    assert alerts.evaluate(T0 + 200, out, rule)["sent"] == [] and len(out.sent) == 1
    assert alerts.evaluate(T0 + 130 + 6 * 3600 + 5, out, rule)["sent"] == ["site_down:cloud"]
    assert len(out.sent) == 2
    done = alerts.evaluate(T0 + 7 * 3600, out, {"teste": never})
    assert done["resolved"] == ["site_down:cloud"] and "resolvido" in out.sent[-1][0]
    assert alerts.active() == {}
    alerts.evaluate(T0 + 7 * 3600 + 60, out, {"teste": never})
    assert len(out.sent) == 3  # sem avisos a mais


def test_varios_alertas_novos_vao_num_so_email(configured):
    out = Outbox()
    rule = firing(
        alerts.Firing("disk_high", "disco a 90 %", "limpar"),
        alerts.Firing("redis_down", "Redis em baixo", "ConnectionError"),
    )
    result = alerts.evaluate(T0, out, rule)
    assert sorted(result["sent"]) == ["disk_high", "redis_down"] and len(out.sent) == 1
    assert "2 alertas" in out.sent[0][0]


def test_falha_certa_repete_em_10_minutos_e_incerta_so_em_6_horas(configured):
    out = Outbox()
    rule = firing(alerts.Firing("disk_high", "disco a 90 %", "x"))
    out.error = mail.MailError("Brevo 401")
    assert alerts.evaluate(T0, out, rule)["sent"] == []
    assert alerts.evaluate(T0 + 60, out, rule)["sent"] == []
    out.error = None
    assert alerts.evaluate(T0 + 601, out, rule)["sent"] == ["disk_high"]
    other = firing(alerts.Firing("redis_down", "Redis em baixo", "x"))
    out2 = Outbox()
    out2.error = mail.MailUnknown("timeout")
    alerts.evaluate(T0 + 5000, out2, other)
    out2.error = None
    assert alerts.evaluate(T0 + 5000 + 700, out2, other)["sent"] == [] and out2.sent == []


def test_verificacao_avariada_nao_declara_recuperacao(configured):
    out = Outbox()
    alerts.evaluate(T0, out, firing(alerts.Firing("disk_high", "disco a 90 %", "x")))

    def broken(db):
        raise RuntimeError("falhou")

    result = alerts.evaluate(T0 + 60, out, {"disk_high": broken})
    assert result["resolved"] == [] and "disk_high" in alerts.active()
    assert len(out.sent) == 1


def test_sem_destinatario_nao_envia(monkeypatch):
    monkeypatch.setattr(settings, "approval_notify_email", "")
    out = Outbox()
    assert alerts.evaluate(T0, out, firing(alerts.Firing("disk_high", "d", "x")))["sent"] == []
    assert out.sent == []


# --- As regras ------------------------------------------------------------------------------------


def test_regra_dos_sites_usa_as_sondas(cloud, configured):  # noqa: F811
    with session_scope() as db:
        probes.run(
            db, T0, lambda url: {"ok": "cloud" not in url, "status": 503, "ms": 9, "error": ""}
        )
        found = alerts.check_sites(db)
    assert [f.key for f in found] == ["site_down:cloud"] and found[0].for_s == 120


def test_regra_dos_provedores_orcamentos_e_fila(cloud, configured):  # noqa: F811
    with session_scope() as db:
        db.execute(
            text(
                "UPDATE ai_providers SET enabled = true, daily_call_limit = 10 "
                "WHERE name = 'claude'"
            )
        )
        db.execute(
            text(
                "INSERT INTO ai_provider_health (provider_id, status, source) "
                "SELECT id, 'not_authenticated', 'probe' FROM ai_providers WHERE name = 'claude'"
            )
        )
        db.execute(
            text(
                "INSERT INTO provider_usage (provider, kind, role, outcome) "
                "SELECT 'claude', 'subscription', 'writer', 'ok' FROM generate_series(1, 8)"
            )
        )
        db.execute(text("UPDATE sites SET enabled = true WHERE site_key = 'cloud'"))
    job = insert_job("cloud", 1, "PLANNED")
    advance(job, "QUEUED")
    with session_scope() as db:
        assert [f.key for f in alerts.check_providers(db)] == ["provider_auth:claude"]
        budget = alerts.check_budget(db)
        assert [f.key for f in budget] == ["budget:provider:claude:calls_day"] and "80 %" in budget[
            0
        ].title
        assert [f.key for f in alerts.check_queue(db)] == [
            "queue_stopped"
        ]  # o slot venceu há muito
        db.execute(text("DELETE FROM provider_usage"))
        assert alerts.check_budget(db) == []


def test_regra_do_disco_dos_certificados_e_da_infraestrutura(cloud, monkeypatch):  # noqa: F811
    monkeypatch.setattr(
        alerts.shutil, "disk_usage", lambda p: SimpleNamespace(total=100, used=90, free=10)
    )
    assert [f.key for f in alerts.check_disk(None)] == ["disk_high"]
    monkeypatch.setattr(
        alerts.shutil, "disk_usage", lambda p: SimpleNamespace(total=100, used=50, free=50)
    )
    assert alerts.check_disk(None) == []
    monkeypatch.setattr(
        probes,
        "cert_days",
        lambda hosts, *a, **k: {"cloud.shortsblog.com": 10.0, "ia.shortsblog.com": 60.0},
    )
    with session_scope() as db:
        assert [f.key for f in alerts.check_certs(db)] == ["cert:cloud.shortsblog.com"]
    assert alerts.check_infra(None) == []
    monkeypatch.setattr(redis_client, "ping", lambda: (_ for _ in ()).throw(ConnectionError("x")))
    assert [f.key for f in alerts.check_infra(None)] == ["redis_down"]


def test_avaliacao_completa_sem_problemas_nao_avisa(cloud, configured, monkeypatch):  # noqa: F811
    monkeypatch.setattr(
        alerts.shutil, "disk_usage", lambda p: SimpleNamespace(total=100, used=10, free=90)
    )
    monkeypatch.setattr(probes, "cert_days", lambda hosts, *a, **k: {})
    out = Outbox()
    result = alerts.evaluate(time.time(), out)
    assert result["firing"] == [] and out.sent == []


def test_backup_parado_so_avisa_depois_da_primeira_pulsacao(configured):
    """R52: sem pulsação nenhuma (pacote sem backup ligado ao motor) não há aviso falso."""
    from app.services import backup_beat

    with session_scope() as db:
        assert alerts.check_backup(db, T0) == []
    assert backup_beat.main(["abc"]) == 2  # epoch inválido: uso, nada gravado
    assert backup_beat.main([str(T0 - 25 * 3600)]) == 0
    with session_scope() as db:
        assert backup_beat.last_ok(db) == T0 - 25 * 3600
        assert alerts.check_backup(db, T0) == []  # 25 h: dentro da folga de 26 h
    backup_beat.record(T0 - 30 * 3600)
    with session_scope() as db:
        (item,) = alerts.check_backup(db, T0)
    assert item.key == "backup_stale" and item.title == "backup sem sucesso há 30 h"
    # Pelo avaliador: vai por e-mail e avisa a recuperação quando o backup volta
    out = Outbox()
    checks = {"backup": lambda db: alerts.check_backup(db, T0)}
    assert alerts.evaluate(T0, out, checks)["firing"] == ["backup_stale"]
    assert "backup sem sucesso" in out.sent[-1][0] + out.sent[-1][1]
    backup_beat.record(T0)
    alerts.evaluate(T0 + 60, out, checks)
    assert "resolvido" in out.sent[-1][0]


# --- Painel de saúde e métricas -------------------------------------------------------------------


def test_painel_de_saude_exige_sessao(client):
    assert client.get("/dashboard/health", follow_redirects=False).status_code == 303


def test_painel_de_saude_mostra_o_estado(panel_client, cloud, configured):  # noqa: F811
    with session_scope() as db:
        probes.run(
            db,
            time.time(),
            lambda url: {"ok": "ia" not in url, "status": 503, "ms": 7, "error": ""},
        )
    alerts.evaluate(time.time(), Outbox(), firing(alerts.Firing("disk_high", "disco a 90 %", "x")))
    page = panel_client.get("/dashboard/health").text
    assert "Saúde" in page and "disk_high" in page and "painel" in page
    assert "fora há" in page and "Agendador" in page and "Provedores de IA" in page
    assert 'href="/dashboard/health"' in panel_client.get("/").text


def test_metricas_das_sondas_e_dos_alertas(client, cloud, configured):  # noqa: F811
    with session_scope() as db:
        probes.run(
            db,
            time.time(),
            lambda url: {"ok": "ia" not in url, "status": 200, "ms": 250, "error": ""},
        )
    alerts.evaluate(
        time.time(), Outbox(), firing(alerts.Firing("redis_down", "Redis em baixo", "x"))
    )
    text_ = client.get("/metrics").text
    assert 'sb_site_up{site="ia"} 0.0' in text_ and 'sb_site_up{site="cloud"} 1.0' in text_
    assert 'sb_site_latency_seconds{site="cloud"} 0.25' in text_
    assert 'sb_alert_active{alert="redis_down"} 1.0' in text_


def test_volume_de_cli_acima_do_teto_avisa(monkeypatch):
    """R31: o gateway mede os volumes das CLIs; acima do teto o motor avisa."""
    from app.services.ai import gateway

    sizes = {"claude": 1024, "codex": 600 * 1024 * 1024, "agy": 10}

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/health/storage"
        return httpx.Response(200, json=sizes)

    monkeypatch.setattr(gateway, "_transport", httpx.MockTransport(handle))
    with session_scope() as db:
        firing = alerts.check_ai_storage(db)
    assert [f.key for f in firing] == ["ai_storage:codex"]
    assert "600 MB" in firing[0].title

    monkeypatch.setattr(gateway, "_transport",
                        httpx.MockTransport(lambda r: httpx.Response(502)))
    with session_scope() as db:
        assert alerts.check_ai_storage(db) == []           # gateway mudo: outro alerta cuida
