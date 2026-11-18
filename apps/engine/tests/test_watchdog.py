"""E11.4 — o vigia do agendador: avisa se o n8n deixar de chamar o motor."""

from __future__ import annotations

import pytest

from app.core.cache import redis_client
from app.core.config import settings
from app.services import watchdog
from app.services.notify import mail

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


def fresh(now: float) -> None:
    for route in watchdog.LIMITS:
        watchdog.record(route, now)


def test_sem_nenhuma_chamada_o_vigia_fica_desarmado(configured):
    out = Outbox()
    assert watchdog.check(T0, out)["action"] == "desarmado" and out.sent == []
    assert watchdog.status(T0)["armed"] is False and watchdog.status(T0)["ok"] is False


def test_rota_ainda_nunca_chamada_nao_conta_logo_como_parada(configured):
    """Ao armar (depois de reconstruir o motor) o `discover` pode não ter corrido: não é falha."""
    out = Outbox()
    for route in ("dispatch", "publish-due", "reconcile"):
        watchdog.record(route, T0)
    assert watchdog.check(T0 + 120, out)["action"] == "ok" and out.sent == []
    assert watchdog.status(T0 + 120)["ok"] is True
    for route in (
        "dispatch",
        "publish-due",
        "reconcile",
    ):  # 4 h depois, o discover continua sem vir
        watchdog.record(route, T0 + 4 * 3600 + 60)
    result = watchdog.check(T0 + 4 * 3600 + 120, out)
    assert result["stale"] == ["discover"] and result["action"] == "avisou"


def test_em_dia_nao_avisa(configured):
    out = Outbox()
    fresh(T0)
    assert watchdog.check(T0 + 120, out)["action"] == "ok" and out.sent == []
    assert watchdog.status(T0 + 120)["ok"] is True


def test_parou_avisa_uma_vez_repete_de_6_em_6_horas_e_avisa_a_recuperacao(configured):
    out = Outbox()
    fresh(T0)
    late = T0 + 400  # o dispatch passou os 5 minutos
    assert watchdog.check(late, out)["action"] == "avisou"
    subject, html = out.sent[0]
    assert "deixou de chamar o motor" in subject and "dispatch" in html
    assert watchdog.check(late + 60, out)["action"] == "a aguardar" and len(out.sent) == 1
    assert watchdog.check(late + 3 * 3600, out)["action"] == "a aguardar" and len(out.sent) == 1
    assert watchdog.check(late + 6 * 3600 + 5, out)["action"] == "avisou" and len(out.sent) == 2
    fresh(late + 7 * 3600)  # o n8n voltou
    assert watchdog.check(late + 7 * 3600 + 30, out)["action"] == "recuperou"
    assert "voltou" in out.sent[-1][0] and len(out.sent) == 3
    assert watchdog.check(late + 7 * 3600 + 90, out)["action"] == "ok" and len(out.sent) == 3


def test_texto_do_aviso_conforme_quem_agenda(configured, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "internal_scheduler_enabled", False)
    monkeypatch.setattr(settings, "n8n_url", "https://n8n.exemplo.test")
    out = Outbox()
    fresh(T0)
    watchdog.check(T0 + 400, out)
    subject, html = out.sent[0]
    assert "o n8n deixou" in subject and "https://n8n.exemplo.test" in html and "SB-*" in html

    # E19.2: no pacote quem agenda é o próprio motor; o aviso aponta para o container dele.
    monkeypatch.setattr(settings, "internal_scheduler_enabled", True)
    monkeypatch.setattr(settings, "n8n_url", "")
    from app.core.cache import redis_client
    redis_client.delete("sb:watchdog:state")
    watchdog.check(T0 + 400, out)
    subject, html = out.sent[-1]
    assert "agendador interno deixou" in subject and "sb-engine-scheduler" in html
    assert "n8n" not in html


def test_workflow_desativado_so_uma_rota_parada_tambem_avisa(configured):
    out = Outbox()
    fresh(T0)
    now = T0 + 1000
    for route in ("dispatch", "publish-due", "discover"):  # só o reconcile ficou parado
        watchdog.record(route, now)
    result = watchdog.check(now + 30, out)
    assert result["stale"] == ["reconcile"] and result["action"] == "avisou"


def test_falha_certa_repete_em_10_minutos_e_incerta_so_em_6_horas(configured):
    out = Outbox()
    fresh(T0)
    late = T0 + 400
    out.error = mail.MailError("Brevo 401")
    assert watchdog.check(late, out)["action"] == "falhou"
    assert watchdog.check(late + 60, out)["action"] == "a aguardar"
    out.error = None
    assert watchdog.check(late + 601, out)["action"] == "avisou" and len(out.sent) == 1
    # resultado incerto: não repete antes de 6 horas
    out2 = Outbox()
    redis_client.delete("sb:watchdog:state")
    out2.error = mail.MailUnknown("timeout")
    assert watchdog.check(late + 4000, out2)["action"] == "incerto"
    out2.error = None
    assert watchdog.check(late + 4000 + 700, out2)["action"] == "a aguardar" and out2.sent == []


def test_sem_destinatario_nao_envia(monkeypatch):
    monkeypatch.setattr(settings, "approval_notify_email", "")
    fresh(T0)
    out = Outbox()
    assert watchdog.check(T0 + 400, out)["action"] == "sem destinatário" and out.sent == []


def test_as_rotas_do_agendador_registam_a_chamada(client, auth):
    client.post("/api/sched/dispatch", headers=auth)
    age = watchdog.ages()["dispatch"]
    assert age is not None and age < 5
    assert watchdog.ages()["reconcile"] is None
    assert client.post("/api/sched/dispatch").status_code in (401, 403)  # sem token não conta


def test_health_scheduler_e_metrica(client, auth):
    assert client.get("/health/scheduler").status_code == 200  # desarmado
    fresh(__import__("time").time())
    assert client.get("/health/scheduler").status_code == 200
    redis_client.set("sb:sched:last:dispatch", "1000")  # há muito tempo
    response = client.get("/health/scheduler")
    assert response.status_code == 503 and response.json()["stale"] == ["dispatch"]
    metrics = client.get("/metrics").text
    assert (
        "sb_sched_ok 0.0" in metrics
        and 'sb_sched_last_call_age_seconds{route="dispatch"}' in metrics
    )


def _keep_alive(now: float, skip: tuple[str, ...]) -> None:
    for route in watchdog.LIMITS:
        if route not in skip:
            watchdog.record(route, now)


def test_plan_day_parado_ha_mais_de_25_horas_avisa(configured):
    out = Outbox()
    fresh(T0)
    _keep_alive(T0 + 24 * 3600, skip=("plan-day",))
    assert watchdog.check(T0 + 24 * 3600 + 60, out)["action"] == "ok"   # 24 h: dentro do limite
    _keep_alive(T0 + 25 * 3600 + 120, skip=("plan-day",))
    result = watchdog.check(T0 + 25 * 3600 + 180, out)
    assert result["stale"] == ["plan-day"] and result["action"] == "avisou"


def test_approval_batch_parado_ha_mais_de_16_horas_avisa(configured):
    out = Outbox()
    fresh(T0)
    _keep_alive(T0 + 15 * 3600, skip=("approval-batch",))
    assert watchdog.check(T0 + 15 * 3600 + 60, out)["action"] == "ok"
    _keep_alive(T0 + 16 * 3600 + 120, skip=("approval-batch",))
    result = watchdog.check(T0 + 16 * 3600 + 180, out)
    assert result["stale"] == ["approval-batch"] and result["action"] == "avisou"


def test_digest_parado_ha_mais_de_25_horas_avisa(configured):
    out = Outbox()
    fresh(T0)
    _keep_alive(T0 + 24 * 3600, skip=("digest",))
    assert watchdog.check(T0 + 24 * 3600 + 60, out)["action"] == "ok"
    _keep_alive(T0 + 25 * 3600 + 120, skip=("digest",))
    result = watchdog.check(T0 + 25 * 3600 + 180, out)
    assert result["stale"] == ["digest"] and result["action"] == "avisou"


def test_newsletter_ligada_depois_de_armado_nao_avisa_logo(configured, monkeypatch):
    """Ligar a newsletter com o vigia armado há dias não é falha: conta a partir de quando ligou."""
    out = Outbox()
    fresh(T0)
    later = T0 + 3 * 86400
    fresh(later)
    monkeypatch.setattr(settings, "newsletter_enabled", True)
    assert watchdog.check(later + 60, out)["action"] == "ok" and out.sent == []
    fresh(later + 25 * 3600)
    result = watchdog.check(later + 25 * 3600 + 120, out)
    assert result["stale"] == ["newsletter"] and result["action"] == "avisou"


def test_newsletter_religada_conta_do_zero(configured, monkeypatch):
    out = Outbox()
    monkeypatch.setattr(settings, "newsletter_enabled", True)
    fresh(T0)
    watchdog.record("newsletter", T0)
    assert watchdog.status(T0 + 60)["ok"] is True
    monkeypatch.setattr(settings, "newsletter_enabled", False)
    later = T0 + 5 * 86400
    fresh(later)
    assert watchdog.status(later)["ok"] is True
    monkeypatch.setattr(settings, "newsletter_enabled", True)
    assert watchdog.check(later + 60, out)["action"] == "ok" and out.sent == []
