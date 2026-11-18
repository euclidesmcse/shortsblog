"""E10.5/E10.6 — pedidos de aprovação por lotes, resumo diário e envio com resultado incerto."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import text

from app.core.config import settings
from app.core.db import session_scope
from app.services import approval_links
from app.services.notify import digests, mail
from tests.conftest import advance, fetch
from tests.test_sites import cloud, insert_job  # noqa: F401  (fixture `cloud`: os 6 sites)

TO = "euclides@example.com"
NOON_SP = datetime(2026, 10, 5, 15, 5, tzinfo=UTC)  # 12:05 em São Paulo → lote das 12:00
EVENING = datetime(2026, 10, 5, 23, 59, tzinfo=UTC)  # 20:59 em São Paulo, 2026-10-05
UP = (
    "QUEUED",
    "CLAIMED",
    "RESEARCHING",
    "WRITING",
    "VALIDATING",
    "TRANSLATING",
    "AWAITING_APPROVAL",
)


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(settings, "approval_notify_email", TO)
    monkeypatch.setattr(settings, "public_base_url", "https://painel.exemplo.test")


class Outbox:
    """Um remetente de mentira que guarda o que "enviou" e pode falhar."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []
        self.error: Exception | None = None

    def __call__(self, to: str, name: str, subject: str, html: str) -> str:
        if self.error:
            raise self.error
        self.sent.append((to, subject, html))
        return "msg-123"


def waiting_job(site: str, slot: int, title: str) -> int:
    job = insert_job(site, slot, "PLANNED")
    advance(job, *UP)
    with session_scope() as db:
        db.execute(
            text(
                "INSERT INTO job_translations (job_id, language, is_original, title, html, status) "
                "VALUES (:j, 'pt-BR', true, :t, '<p>x</p>', 'ready')"
            ),
            {"j": job, "t": title},
        )
        db.execute(
            text("UPDATE publication_jobs SET review_report = CAST(:r AS JSONB) WHERE id = :j"),
            {"r": '{"score": 94}', "j": job},
        )
    return job


# --- Lotes de aprovação ------------------------------------------------------------------------


def test_sem_destinatario_ou_sem_pendentes_nao_envia(cloud, monkeypatch):  # noqa: F811
    out = Outbox()
    assert digests.approval_batch(NOON_SP, out)["reason"].startswith("APPROVAL_NOTIFY_EMAIL")
    monkeypatch.setattr(settings, "approval_notify_email", TO)
    assert digests.approval_batch(NOON_SP, out)["reason"] == "nada pendente"
    assert out.sent == [] and fetch("SELECT 1 FROM approval_batches") == []


def test_um_e_mail_com_links_que_funcionam(cloud, configured):  # noqa: F811
    one = waiting_job("cloud", 1, "Título <b>perigoso</b> & cia")
    two = waiting_job("ia", 1, "Segundo artigo")
    out = Outbox()
    result = digests.approval_batch(NOON_SP, out)
    assert result == {"sent": True, "status": "sent", "jobs": 2, "slot": "12:00", "missing": 0}
    ((to, subject, html),) = out.sent
    assert to == TO and subject == "2 artigos à espera da sua aprovação | 2026-10-05 12:00"
    assert "&lt;b&gt;perigoso&lt;/b&gt; &amp; cia" in html and "<b>perigoso</b>" not in html
    assert f"https://painel.exemplo.test/dashboard/review/{one}" in html
    assert "nota 94" in html
    tokens = [part.split('"')[0] for part in html.split("/a/")[1:]]
    assert len(tokens) == 4  # aprovar + rejeitar de cada um
    with session_scope() as db:
        found = {
            (link.job_id, link.action)
            for link in (approval_links.lookup(db, t, NOON_SP) for t in tokens)
        }
    assert found == {(one, "approve"), (one, "reject"), (two, "approve"), (two, "reject")}
    row = fetch("SELECT status, jobs FROM approval_batches")[0]
    assert (row["status"], row["jobs"]) == ("sent", 2)


def test_o_mesmo_lote_nao_se_repete_mas_o_seguinte_sim(cloud, configured):  # noqa: F811
    waiting_job("cloud", 1, "Um artigo")
    out = Outbox()
    digests.approval_batch(NOON_SP, out)
    again = digests.approval_batch(NOON_SP + timedelta(minutes=10), out)  # ainda a mesma hora
    assert again["sent"] is False and "já foi enviado" in again["reason"] and len(out.sent) == 1
    assert digests.approval_batch(NOON_SP + timedelta(hours=1), out)["sent"] is True
    assert len(out.sent) == 2


def test_falha_certa_repete_e_incerta_nao(cloud, configured):  # noqa: F811
    waiting_job("cloud", 1, "Um artigo")
    out = Outbox()
    out.error = mail.MailError("Brevo 401")
    assert digests.approval_batch(NOON_SP, out)["status"] == "failed"
    out.error = None
    assert digests.approval_batch(NOON_SP, out)["sent"] is True  # falhou de certeza: tenta de novo
    waiting_job("ia", 1, "Outro")
    later = NOON_SP + timedelta(hours=2)
    out.error = mail.MailUnknown("timeout")
    assert digests.approval_batch(later, out)["status"] == "unknown"
    out.error = None
    again = digests.approval_batch(later, out)
    assert again["sent"] is False and len(out.sent) == 1  # SEND_UNKNOWN: não repete
    assert (
        fetch("SELECT status, error FROM approval_batches ORDER BY slot DESC")[0]["status"]
        == "unknown"
    )


def test_lote_lista_os_que_nao_vieram_com_motivo_numa_linha(cloud, configured):  # noqa: F811
    # Caso do 194 (8/11): um job bloqueado ficava invisível porque o lote só listava os
    # à espera de aprovação. Agora aparece, com o motivo num só fôlego (sem o diagnóstico).
    with session_scope() as db:
        db.execute(text("UPDATE sites SET enabled = true WHERE site_key IN ('cloud', 'saude')"))
    waiting_job("cloud", 1, "Artigo pronto")
    stuck = insert_job("saude", 1, "PLANNED")
    advance(stuck, "QUEUED", "CLAIMED", "BLOCKED",
            blocked_reason="tradução zh-CN: números diferentes | números a mais em: «809»")
    out = Outbox()
    result = digests.approval_batch(NOON_SP, out)
    assert result["sent"] is True and result["jobs"] == 1 and result["missing"] == 1
    ((_, subject, html),) = out.sent
    assert subject == ("1 artigo à espera da sua aprovação | 2026-10-05 12:00 | 1 não veio")
    assert "Não vieram neste lote" in html and f"job #{stuck}" in html and "bloqueado" in html
    assert "números diferentes" in html and "números a mais" not in html  # só a 1.ª parte


def test_lote_sem_pendentes_mas_com_problema_ainda_avisa(cloud, configured):  # noqa: F811
    with session_scope() as db:
        db.execute(text("UPDATE sites SET enabled = true WHERE site_key = 'cloud'"))
        # Um EXPIRED de ontem não entra: o desfecho não se repete em todos os lotes seguintes.
        db.execute(text("""INSERT INTO publication_jobs
                             (site_key, local_date, slot_number, due_at_utc, produce_after_utc,
                              status) VALUES ('cloud', '2026-10-04', 1, '2026-10-04 11:30+00',
                              '2026-10-04 08:30+00', 'EXPIRED')"""))
    stuck = insert_job("cloud", 1, "PLANNED")
    advance(stuck, "QUEUED", "CLAIMED", "BLOCKED", blocked_reason="gates: história duplicada")
    out = Outbox()
    result = digests.approval_batch(NOON_SP, out)
    assert result["sent"] is True and result["jobs"] == 0 and result["missing"] == 1
    ((_, subject, html),) = out.sent
    assert subject == "Nenhum artigo à espera e 1 não veio | 2026-10-05 12:00"
    assert "gates: história duplicada" in html and html.count("job #") == 1


def test_motivo_numa_linha():
    assert digests._one_line("abc | diagnóstico longo") == "abc"
    assert digests._one_line(None) == "" and digests._one_line("  a\n b ") == "a b"
    cut = digests._one_line("palavra " * 40)
    assert len(cut) <= 160 and cut.endswith("…")


# --- Resumo diário ------------------------------------------------------------------------------


def make_day(enabled: bool = True) -> None:
    with session_scope() as db:
        db.execute(
            text("UPDATE sites SET enabled = :e WHERE site_key IN ('cloud', 'ia')"), {"e": enabled}
        )
    done = insert_job("cloud", 1, "PLANNED")
    advance(done, *UP, "APPROVED", "PUBLISHING", "PUBLISHED_CONFIRMED")
    blocked = insert_job("cloud", 2, "PLANNED")
    advance(blocked, "QUEUED", "CLAIMED", "BLOCKED", blocked_reason="fontes insuficientes: 1 de 2")
    pending = waiting_job("ia", 1, "Ainda por aprovar")
    with session_scope() as db:
        db.execute(
            text(
                "INSERT INTO job_translations (job_id, language, is_original, title, html, status) "
                "VALUES (:j, 'pt-BR', true, 'Publicado <hoje>', '<p>x</p>', 'ready')"
            ),
            {"j": done},
        )
        db.execute(
            text(
                "UPDATE publication_jobs SET wordpress_url = 'https://cloud.exemplo.test/a/', "
                "published_at_utc = '2026-10-05 13:30+00' WHERE id = :j"
            ),
            {"j": done},
        )
    assert pending


def test_resumo_com_numeros_assunto_e_avisos(cloud, configured, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "wp_publish_mode", "live")
    make_day()
    out = Outbox()
    result = digests.daily_digest(EVENING, out)
    assert result["sent"] is True
    assert result["subject"] == "Resumo diário dos blogs | 2026-10-05 | 1/30 publicados"
    html = out.sent[0][2]
    assert (
        "Publicado &lt;hoje&gt;" in html
        and "10:30" in html
        and "https://cloud.exemplo.test/a/" in html
    )
    assert "⚠️ 1 de 5 publicados" in html  # o cloud publicou menos de 5
    assert "fontes insuficientes: 1 de 2" in html and "Ainda por aprovar" in html
    assert "Modo rascunho" not in html
    assert "Blog desligado" in html  # os outros blogs
    row = fetch("SELECT status, message_reference FROM daily_digest")[0]
    assert (row["status"], row["message_reference"]) == ("sent", "msg-123")


def test_resumo_em_modo_rascunho_avisa_e_sem_atividade_vem_a_zero(cloud, configured):  # noqa: F811
    out = Outbox()
    digests.daily_digest(EVENING, out)
    assert "0/30 publicados" in out.sent[0][1] and "Modo rascunho" in out.sent[0][2]


def test_resumo_uma_vez_por_dia_e_destinatario_e_incerto_nao_repete(cloud, configured):  # noqa: F811
    out = Outbox()
    assert digests.daily_digest(EVENING, out)["sent"] is True
    assert digests.daily_digest(EVENING + timedelta(minutes=1), out)["sent"] is False
    assert len(out.sent) == 1
    out2, next_day = Outbox(), EVENING + timedelta(days=1)
    out2.error = mail.MailUnknown("ligação cortada")
    assert digests.daily_digest(next_day, out2)["status"] == "unknown"
    out2.error = None
    assert digests.daily_digest(next_day, out2)["sent"] is False and out2.sent == []


def test_rotas_pedem_token(client, auth, cloud):  # noqa: F811
    assert client.post("/api/sched/digest").status_code in (401, 403)
    assert client.post("/api/sched/approval-batch").status_code in (401, 403)
    assert (
        client.post("/api/sched/approval-batch", headers=auth)
        .json()["reason"]
        .startswith("APPROVAL")
    )


# --- E10.6: envio e resultado incerto -----------------------------------------------------------


def _brevo(monkeypatch, behaviour):
    monkeypatch.setattr(settings, "mail_from_email", "motor@shortsblog.com")
    monkeypatch.setattr(settings, "brevo_api_key", "chave")
    monkeypatch.setattr(mail.http, "post_json", behaviour)


def test_brevo_devolve_a_referencia(monkeypatch):
    _brevo(
        monkeypatch,
        lambda *a, **k: SimpleNamespace(
            status_code=201, text="", json=lambda: {"messageId": "<abc@brevo>"}
        ),
    )
    assert mail.send_transactional(TO, "", "Assunto", "<p>x</p>") == "<abc@brevo>"


@pytest.mark.parametrize(
    "raised, kind",
    [
        (httpx.ConnectError("sem rota"), mail.MailError),
        (httpx.ReadTimeout("lento"), mail.MailUnknown),
        (httpx.RemoteProtocolError("cortada"), mail.MailUnknown),
    ],
)
def test_excecoes_de_rede_certa_ou_incerta(monkeypatch, raised, kind):
    def boom(*a, **k):
        raise raised

    _brevo(monkeypatch, boom)
    with pytest.raises(kind) as caught:
        mail.send_transactional(TO, "", "A", "<p/>")
    assert type(caught.value) is kind


@pytest.mark.parametrize(
    "code, kind",
    [
        (500, mail.MailUnknown),
        (503, mail.MailUnknown),
        (400, mail.MailError),
        (401, mail.MailError),
        (429, mail.MailError),
    ],
)
def test_codigos_http(monkeypatch, code, kind):
    _brevo(monkeypatch, lambda *a, **k: SimpleNamespace(status_code=code, text="x"))
    with pytest.raises(kind) as caught:
        mail.send_transactional(TO, "", "A", "<p/>")
    assert type(caught.value) is kind


def test_chave_smtp_do_brevo_usa_o_relay_e_nao_a_api(monkeypatch):
    """`xsmtpsib-…` é uma chave SMTP (a API dá 401): envia-se pelo relay com essa chave."""
    import smtplib

    calls: dict = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=0):
            calls["host"], calls["port"] = host, port

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context=None):
            calls["tls"] = context

        def login(self, user, password):
            calls["login"] = (user, password)

        def send_message(self, message):
            calls["to"] = message["To"]

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(mail.http, "post_json", lambda *a, **k: pytest.fail("não devia usar a API"))
    monkeypatch.setattr(settings, "mail_from_email", "painel@shortsblog.com")
    monkeypatch.setattr(settings, "brevo_api_key", "xsmtpsib-abc123")
    monkeypatch.setattr(settings, "smtp_host", "smtp-relay.brevo.com")
    monkeypatch.setattr(settings, "smtp_port", 587)
    monkeypatch.setattr(settings, "smtp_user", "login@smtp-brevo.com")
    monkeypatch.setattr(settings, "smtp_password", "")
    assert mail.send_transactional(TO, "", "Teste", "<p>x</p>") == "enviado"
    assert calls["host"] == "smtp-relay.brevo.com" and calls["port"] == 587 and calls["tls"]
    import ssl
    assert calls["tls"].verify_mode == ssl.CERT_REQUIRED     # STARTTLS confere o certificado
    assert calls["login"] == ("login@smtp-brevo.com", "xsmtpsib-abc123") and calls["to"] == TO


def test_chave_de_api_normal_continua_a_usar_a_api(monkeypatch):
    sent = {}
    monkeypatch.setattr(settings, "mail_from_email", "painel@shortsblog.com")
    monkeypatch.setattr(settings, "brevo_api_key", "xkeysib-abc")
    monkeypatch.setattr(settings, "smtp_host", "smtp-relay.brevo.com")
    monkeypatch.setattr(
        mail.http,
        "post_json",
        lambda *a, **k: (
            sent.update(ok=True)
            or SimpleNamespace(status_code=201, text="", json=lambda: {"messageId": "m1"})
        ),
    )
    assert mail.send_transactional(TO, "", "Teste", "<p>x</p>") == "m1" and sent["ok"]
