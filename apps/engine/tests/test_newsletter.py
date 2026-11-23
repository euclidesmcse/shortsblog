"""E17 — newsletter: cadastro, dupla confirmação, saída de 1 clique, envio diário e webhook."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import text

from app.core.config import settings
from app.core.db import session_scope
from app.services import newsletter, sites, watchdog
from app.services.notify import mail, sends
from tests.conftest import fetch

NOW = datetime(2026, 10, 22, 11, 0, tzinfo=UTC)          # 08:00 em São Paulo
EMAIL = "Leitor.Teste@Example.com"
TOKEN = "nl-token-de-teste"


class Outbox:
    """Remetente falso: guarda o que sairia e pode falhar de propósito."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.error: Exception | None = None

    def __call__(self, to, name, subject, body, **kw) -> str:
        if self.error:
            raise self.error
        self.sent.append({"to": to, "subject": subject, "body": body, **kw})
        return f"msg-{len(self.sent)}"


@pytest.fixture
def nl(monkeypatch) -> list[int]:
    """Newsletter ligada, sites na base, e a fila `fast` trocada por uma lista."""
    monkeypatch.setattr(settings, "newsletter_enabled", True)
    monkeypatch.setattr(settings, "newsletter_api_token", TOKEN)
    monkeypatch.setattr(settings, "newsletter_webhook_token", "brevo-secreto")
    monkeypatch.setattr(settings, "approval_notify_email", "")
    queued: list[int] = []
    monkeypatch.setattr(newsletter, "_enqueue_confirmation", queued.append)
    with session_scope() as db:
        sites.sync(db)
    return queued


def subscribe(email: str = EMAIL, language: str = "pt-BR", **kw) -> dict:
    with session_scope() as db:
        return newsletter.subscribe(db, email=email, language=language,
                                    site_key=kw.pop("site_key", "cloud"), now=NOW, **kw)


def confirmation_token(outbox: Outbox, subscriber_id: int = 1,
                       now: datetime = NOW) -> str:
    newsletter.send_confirmation(subscriber_id, outbox, now=now)
    body = outbox.sent[-1]["text_body"]
    url = next(line for line in body.splitlines() if "/newsletter/confirm?" in line)
    assert url.startswith("https://") and ".shortsblog.com/wp-json/sb/v1/newsletter/" in url
    return parse_qs(urlsplit(url).query)["t"][0]


def act(fn, token: str, *args, now: datetime = NOW) -> dict:
    with session_scope() as db:
        return fn(db, token, *args, now=now)


def subscriber() -> dict:
    return dict(fetch("SELECT * FROM subscribers")[0])


def confirmed(outbox: Outbox, email: str = EMAIL, language: str = "pt-BR") -> int:
    subscribe(email, language)
    sid = fetch("SELECT id FROM subscribers ORDER BY id DESC LIMIT 1")[0]["id"]
    assert act(newsletter.confirm, confirmation_token(outbox, sid))["state"] == "done"
    return sid


def published_post(site: str = "cloud", at: datetime = NOW - timedelta(hours=4)) -> int:
    with session_scope() as db:
        job = db.execute(text(
            """INSERT INTO publication_jobs (site_key, local_date, slot_number, due_at_utc,
                                             produce_after_utc, status, published_at_utc)
               VALUES (:s, :d, 1, :a, :a, 'PUBLISHED_CONFIRMED', :a) RETURNING id"""),
            {"s": site, "d": at.date(), "a": at}).scalar_one()
        for language, title, path in (("pt-BR", "Título em português", ""),
                                      ("en", "Title in English", "en/"),
                                      ("es", "Título en español", "es/")):
            db.execute(text(
                """INSERT INTO job_translations (job_id, language, is_original, title, slug,
                                                 excerpt, html, status, wordpress_url)
                   VALUES (:j, :l, :o, :t, :t, 'Resumo', '<p>x</p>', 'published', :u)"""),
                {"j": job, "l": language, "o": language == "pt-BR", "t": title,
                 "u": f"https://{site}.shortsblog.com/{path}post-{job}/"})
    return job


# --- Cadastro ---------------------------------------------------------------------------------


def test_cadastro_fechado_enquanto_a_newsletter_esta_desligada(nl, monkeypatch):
    monkeypatch.setattr(settings, "newsletter_enabled", False)
    with pytest.raises(newsletter.Closed):
        subscribe()
    assert fetch("SELECT 1 FROM subscribers") == []


def test_cadastro_fica_pendente_e_pede_confirmacao(nl):
    assert subscribe() == {"ok": True}
    row = subscriber()
    assert row["email"] == "leitor.teste@example.com" and row["status"] == "pending"
    assert row["origin_site"] == "cloud" and row["consent_version"]
    assert nl == [row["id"]]


def test_cadastro_repetido_nao_duplica_nem_revela_e_invalida_o_link_antigo(nl):
    out = Outbox()
    subscribe()
    old = confirmation_token(out)
    assert subscribe(EMAIL.upper()) == {"ok": True}
    assert len(fetch("SELECT 1 FROM subscribers")) == 1 and len(nl) == 2
    assert act(newsletter.confirm, old)["state"] == "invalid"     # versão antiga
    assert act(newsletter.confirm, confirmation_token(out))["state"] == "done"
    # Já confirmado: a mesma resposta e nenhum e-mail novo.
    assert subscribe() == {"ok": True} and len(nl) == 2


@pytest.mark.parametrize(("kw", "error"), [({"email": "não é e-mail"}, "email"),
                                           ({"language": "fr"}, "idioma"),
                                           ({"site_key": "outro"}, "blog")])
def test_cadastro_com_dados_invalidos(nl, kw, error):
    args = {"email": EMAIL, "language": "pt-BR", "site_key": "cloud", **kw}
    with session_scope() as db:
        assert newsletter.subscribe(db, now=NOW, **args) == {"ok": False, "error": error}
    assert fetch("SELECT 1 FROM subscribers") == []


def test_campo_isca_e_limites(nl, monkeypatch):
    assert subscribe(honeypot="http://spam") == {"ok": True}
    assert fetch("SELECT 1 FROM subscribers") == []
    monkeypatch.setattr(settings, "newsletter_rate_ip_hour", 2)
    for i in range(2):
        assert subscribe(f"a{i}@example.com", ip="203.0.113.9") == {"ok": True}
    assert subscribe("a9@example.com", ip="203.0.113.9") == {"ok": False, "error": "limite"}
    # Limite por endereço: responde igual, mas não manda outro e-mail.
    monkeypatch.setattr(settings, "newsletter_rate_email_day", 1)
    subscribe("b@example.com")
    sent = len(nl)
    assert subscribe("b@example.com") == {"ok": True} and len(nl) == sent


# --- Confirmação --------------------------------------------------------------------------------


def test_o_link_so_mostra_e_so_o_post_confirma(nl):
    out = Outbox()
    subscribe()
    token = confirmation_token(out)
    assert out.sent[0]["to"] == "leitor.teste@example.com"
    assert out.sent[0]["subject"].startswith("Confirme")
    assert act(newsletter.check, token, "confirm") == {"state": "ready", "language": "pt-BR"}
    assert subscriber()["status"] == "pending"                     # o GET não muda nada
    assert act(newsletter.confirm, token)["state"] == "done"
    assert subscriber()["status"] == "confirmed"
    assert act(newsletter.confirm, token)["state"] == "done"       # repetir não faz mal
    assert act(newsletter.check, token, "confirm")["state"] == "done"


def test_link_de_confirmacao_vencido_ou_adulterado_e_recusado(nl):
    out = Outbox()
    subscribe()
    token = confirmation_token(out)
    late = NOW + timedelta(hours=settings.newsletter_confirm_hours + 1)
    assert act(newsletter.confirm, token, now=late)["state"] == "invalid"
    sid, kind, exp, version, sig = token.split(".")
    for bad in (f"{sid}.{kind}.{int(exp) + 3600}.{version}.{sig}", f"{sid}.u.{exp}.{version}.{sig}",
                token[:-2] + "xx", "lixo", ""):
        assert act(newsletter.confirm, bad)["state"] == "invalid", bad
    assert subscriber()["status"] == "pending"


# --- Saída --------------------------------------------------------------------------------------


def test_saida_de_um_clique_apaga_o_email_e_permite_voltar(nl):
    out = Outbox()
    sid = confirmed(out)
    published_post()
    newsletter.send_daily(NOW, out, pause=lambda s: None)
    edition = out.sent[-1]
    url = edition["headers"]["List-Unsubscribe"].strip("<>")
    assert edition["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert url in edition["body"].replace("&amp;", "&")
    token = parse_qs(urlsplit(url).query)["t"][0]
    assert act(newsletter.check, token, "unsubscribe")["state"] == "ready"
    assert act(newsletter.unsubscribe, token)["state"] == "done"
    row = subscriber()
    assert row["status"] == "unsubscribed" and row["email"] is None and row["email_hash"]
    assert act(newsletter.unsubscribe, token)["state"] == "done"   # repetir não faz mal
    # A edição seguinte não chega a quem saiu.
    sent = len(out.sent)
    newsletter.send_daily(NOW + timedelta(days=1, hours=-6), out, pause=lambda s: None)
    published_post(at=NOW + timedelta(days=1, hours=-7))
    newsletter.send_daily(NOW + timedelta(days=1), out, pause=lambda s: None)
    assert len(out.sent) == sent
    # Voltar a assinar reaproveita a linha (pelo hash) e pede nova confirmação.
    subscribe()
    row = subscriber()
    assert row["id"] == sid and row["status"] == "pending" and row["email"]


# --- Envio diário ---------------------------------------------------------------------------------


def test_dia_sem_post_nao_envia(nl):
    out = Outbox()
    confirmed(out)
    out.sent.clear()
    result = newsletter.send_daily(NOW, out, pause=lambda s: None)
    assert result["sent"] == 0 and result["reason"] == "sem posts no dia" and out.sent == []
    # Post de ontem não entra na edição de hoje.
    published_post(at=NOW - timedelta(days=1))
    assert newsletter.send_daily(NOW, out, pause=lambda s: None)["sent"] == 0


def test_post_aprovado_depois_do_envio_entra_na_edicao_seguinte(nl):
    """Regressão (16/11): saúde e política publicadas às 10:37 UTC, depois da edição das 10:01,
    não saíam em edição nenhuma — a de amanhã só olhava o dia dela."""
    out = Outbox()
    confirmed(out)
    out.sent.clear()
    early = published_post("cloud", at=NOW - timedelta(hours=4))       # 07:00 UTC: entra hoje
    assert newsletter.send_daily(NOW, out, pause=lambda s: None)["posts"] == 1
    late = published_post("saude", at=NOW + timedelta(hours=1))         # depois do envio
    tomorrow = NOW + timedelta(days=1)
    result = newsletter.send_daily(tomorrow, out, pause=lambda s: None)
    assert result["sent"] == 1 and result["posts"] == 1
    body = out.sent[-1]["body"]
    assert f"post-{late}/" in body and f"post-{early}/" not in body   # sem repetir o de ontem


def test_edicao_nao_recua_mais_que_o_limite(nl, monkeypatch):
    """Newsletter parada dias: a edição de volta não traz tudo o que saiu nesse tempo."""
    monkeypatch.setattr(settings, "newsletter_catchup_hours", 72)
    out = Outbox()
    confirmed(out)
    out.sent.clear()
    published_post("cloud", at=NOW - timedelta(hours=4))
    newsletter.send_daily(NOW, out, pause=lambda s: None)
    later = NOW + timedelta(days=10)
    old = published_post("ia", at=later - timedelta(days=5))
    recent = published_post("saude", at=later - timedelta(days=2))
    newsletter.send_daily(later, out, pause=lambda s: None)
    body = out.sent[-1]["body"]
    assert f"post-{recent}/" in body and f"post-{old}/" not in body


def test_reenvio_no_mesmo_dia_mantem_a_janela(nl):
    """Chamar de novo no mesmo dia (limite do Brevo, falha) não encurta a janela da edição."""
    out = Outbox()
    confirmed(out)
    out.sent.clear()
    published_post("cloud", at=NOW - timedelta(hours=4))
    newsletter.send_daily(NOW, out, pause=lambda s: None)
    with session_scope() as db:
        assert newsletter.edition_start(db, NOW + timedelta(hours=2)) == \
            newsletter.edition_start(db, NOW - timedelta(hours=1))


def test_envio_no_idioma_do_assinante_e_sem_duplicar(nl):
    out = Outbox()
    confirmed(out, "pt@example.com", "pt-BR")
    confirmed(out, "en@example.com", "en")
    confirmed(out, "zh@example.com", "zh-CN")          # sem tradução zh: vai a versão pt-BR
    subscribe("pendente@example.com")                  # não confirmou: não recebe
    out.sent.clear()
    job = published_post()
    result = newsletter.send_daily(NOW, out, pause=lambda s: None)
    assert result["sent"] == 3 and result["posts"] == 1
    by_to = {m["to"]: m for m in out.sent}
    assert f"https://cloud.shortsblog.com/en/post-{job}/" in by_to["en@example.com"]["body"]
    assert "Title in English" in by_to["en@example.com"]["body"]
    assert f"https://cloud.shortsblog.com/post-{job}/" in by_to["zh@example.com"]["body"]
    # Assunto da proposta de 4/11 (opção A): o título do dia, não a data ISO.
    assert by_to["en@example.com"]["subject"] == "Title in English"
    assert by_to["pt@example.com"]["subject"] == "Título em português"
    assert all(m["from_email"] == (settings.newsletter_from_email or settings.mail_from_email)
               for m in out.sent)
    again = newsletter.send_daily(NOW + timedelta(hours=1), out, pause=lambda s: None)
    assert again["sent"] == 0 and again["skipped"] == 3 and len(out.sent) == 3


def test_assunto_junta_titulos_e_o_corpo_leva_preheader_data_e_selos(nl):
    """Formato de 4/11: assunto com até 2 títulos + «e mais N»; corpo com preheader
    escondido, data por extenso no cabeçalho e selo colorido por blog."""
    out = Outbox()
    confirmed(out)
    out.sent.clear()
    for site in ("cloud", "ia", "financas"):
        published_post(site)
    assert newsletter.send_daily(NOW, out, pause=lambda s: None)["sent"] == 1
    message = out.sent[0]
    assert message["subject"] == ("Título em português · Título em português"
                                  " — e mais 1")
    body = message["body"]
    assert "display:none" in body and "3 posts novos" in body          # preheader + contagem
    assert "quinta-feira · 22 de outubro de 2026" in body              # NOW é 22/10, quinta
    assert "#6d28d9" in body and "#047857" in body                     # selos de ia e financas
    assert "Ler no blog" in body
    assert "<img" not in body                                          # sem capa gravada


def test_capa_gravada_pelo_publicador_entra_no_email(nl):
    out = Outbox()
    confirmed(out, "pt@example.com", "pt-BR")
    confirmed(out, "en@example.com", "en")
    out.sent.clear()
    job = published_post()
    with session_scope() as db:
        db.execute(text(
            """INSERT INTO image_assets (job_id, provider, license, license_proof_url,
                                         local_path, wordpress_media_urls, alt_texts)
               VALUES (:j, 'openverse', 'CC0', 'https://openverse.org/x', 'capa.webp',
                       CAST(:u AS JSONB), CAST(:a AS JSONB))"""),
            {"j": job,
             "u": json.dumps({"pt-BR": f"https://cloud.shortsblog.com/up/sb-{job}-pt.webp",
                              "en": f"https://cloud.shortsblog.com/up/sb-{job}-en.webp"}),
             "a": json.dumps({"pt-BR": "uma capa", "en": "a cover"})})
    assert newsletter.send_daily(NOW, out, pause=lambda s: None)["sent"] == 2
    by_to = {m["to"]: m for m in out.sent}
    assert (f'src="https://cloud.shortsblog.com/up/sb-{job}-pt.webp"'
            in by_to["pt@example.com"]["body"])
    assert 'alt="uma capa"' in by_to["pt@example.com"]["body"]
    assert (f'src="https://cloud.shortsblog.com/up/sb-{job}-en.webp"'
            in by_to["en@example.com"]["body"])


def test_falha_certa_tenta_de_novo_e_incerto_nao_se_repete(nl):
    out = Outbox()
    confirmed(out)
    out.sent.clear()
    published_post()
    out.error = mail.MailError("recusado")
    assert newsletter.send_daily(NOW, out, pause=lambda s: None)["failed"] == 1
    out.error = None
    assert newsletter.send_daily(NOW, out, pause=lambda s: None)["sent"] == 1
    confirmed(out, "outro@example.com")
    out.sent.clear()
    out.error = mail.MailUnknown("timeout")
    assert newsletter.send_daily(NOW, out, pause=lambda s: None)["unknown"] == 1
    out.error = None
    result = newsletter.send_daily(NOW, out, pause=lambda s: None)
    assert result["sent"] == 0 and out.sent == []        # SEND_UNKNOWN: não repete


def test_assinante_sem_post_no_seu_idioma_nao_recebe_edicao_vazia(nl):
    """Regressão (revisão E14): num dia em que nenhum post tem o idioma do assinante nem o
    pt-BR de reserva, saía um e-mail com a introdução e zero posts."""
    out = Outbox()
    confirmed(out, "zh@example.com", "zh-CN")
    confirmed(out, "en@example.com", "en")
    out.sent.clear()
    at = NOW - timedelta(hours=4)
    with session_scope() as db:
        job = db.execute(text(
            """INSERT INTO publication_jobs (site_key, local_date, slot_number, due_at_utc,
                                             produce_after_utc, status, published_at_utc)
               VALUES ('cloud', :d, 1, :a, :a, 'PUBLISHED_CONFIRMED', :a) RETURNING id"""),
            {"d": at.date(), "a": at}).scalar_one()
        db.execute(text(
            """INSERT INTO job_translations (job_id, language, is_original, title, slug, excerpt,
                                             html, status, wordpress_url)
               VALUES (:j, 'en', false, 'Only in English', 'only-en', 'Resumo', '<p>x</p>',
                       'published', 'https://cloud.shortsblog.com/en/only-en/')"""), {"j": job})
    result = newsletter.send_daily(NOW, out, pause=lambda s: None)
    assert result["sent"] == 1 and result["no_language"] == 1
    assert [m["to"] for m in out.sent] == ["en@example.com"]
    # O assinante zh não ficou marcado como enviado: num dia com posts no idioma, recebe.
    assert fetch("SELECT count(*) AS n FROM newsletter_sends")[0]["n"] == 1


def test_pending_orfao_de_execucao_caida_volta_a_tentar(nl):
    """Worker caiu entre a reserva e o registro: a linha fica `pending` e, sem isto,
    o assinante perderia a edição para sempre (a chamada seguinte deve reenviar)."""
    out = Outbox()
    confirmed(out)
    out.sent.clear()
    published_post()
    edition = NOW.astimezone(newsletter.local_zone()).date()
    with session_scope() as db:
        sid = db.execute(text("SELECT id FROM subscribers")).scalar_one()
        assert sends.claim(db, "newsletter_sends",            # reserva… e a execução morre aqui
                           {"edition_date": edition, "subscriber_id": sid},
                           {"posts": 1, "error": None})
    result = newsletter.send_daily(NOW, out, pause=lambda s: None)
    assert result["sent"] == 1 and len(out.sent) == 1
    assert fetch("SELECT status FROM newsletter_sends")[0]["status"] == "sent"


def test_limite_diario_adia_e_avisa_o_operador(nl, monkeypatch):
    out = Outbox()
    for i in range(3):
        confirmed(out, f"l{i}@example.com")
    out.sent.clear()
    published_post()
    monkeypatch.setattr(settings, "newsletter_daily_limit", 2)
    monkeypatch.setattr(settings, "approval_notify_email", "op@example.com")
    warned: list[str] = []
    monkeypatch.setattr(mail, "send_transactional", lambda to, *a, **k: warned.append(to) or "x")
    result = newsletter.send_daily(NOW, out, pause=lambda s: None)
    assert result["sent"] == 2 and result["deferred"] == 1
    assert warned == ["op@example.com"]
    newsletter.send_daily(NOW, out, pause=lambda s: None)
    assert warned == ["op@example.com"]                    # um aviso por dia


def test_pausa_entre_lotes(nl, monkeypatch):
    out = Outbox()
    for i in range(3):
        confirmed(out, f"p{i}@example.com")
    published_post()
    monkeypatch.setattr(settings, "newsletter_batch_size", 2)
    pauses: list[float] = []
    newsletter.send_daily(NOW, out, pause=pauses.append)
    assert pauses == [settings.newsletter_batch_pause_s]


# --- Webhook do Brevo -----------------------------------------------------------------------------


def test_bounce_ou_reclamacao_suprime(nl):
    out = Outbox()
    confirmed(out)
    with session_scope() as db:
        assert newsletter.brevo_event(db, "delivered", EMAIL) is False
        assert newsletter.brevo_event(db, "hard_bounce", EMAIL) is True
    row = subscriber()
    assert row["status"] == "suppressed" and row["email"] is None
    assert row["suppressed_reason"] == "hard_bounce"
    nl.clear()
    assert subscribe() == {"ok": True} and nl == []         # suprimido: não volta sozinho


# --- API para o WordPress -------------------------------------------------------------------------


def test_api_exige_o_token_da_newsletter(nl, client):
    body = {"email": EMAIL, "language": "pt-BR", "site_key": "cloud"}
    assert client.post("/api/newsletter/subscribe", json=body).status_code == 401
    other = {"Authorization": f"Bearer {settings.sb_api_token}"}
    assert client.post("/api/newsletter/subscribe", json=body, headers=other).status_code == 401
    ok = {"Authorization": f"Bearer {TOKEN}"}
    assert client.post("/api/newsletter/subscribe", json=body, headers=ok).json() == {"ok": True}
    assert client.get("/api/newsletter/status", headers=ok).json() == {"open": True}


def test_api_confirma_sai_e_recebe_o_webhook(nl, client, monkeypatch):
    ok = {"Authorization": f"Bearer {TOKEN}"}
    out = Outbox()
    client.post("/api/newsletter/subscribe", headers=ok,
                json={"email": EMAIL, "language": "en", "site_key": "ia"})
    # A API confere a validade com o relógio real: o token nasce com ele (com NOW fixo, o teste
    # passou a falhar quando o relógio ultrapassou NOW + 48 h).
    token = confirmation_token(out, now=datetime.now(UTC))
    assert client.post("/api/newsletter/check/confirm", headers=ok,
                       json={"token": token}).json() == {"state": "ready", "language": "en"}
    assert client.post("/api/newsletter/confirm", headers=ok,
                       json={"token": token}).json()["state"] == "done"
    assert client.post("/api/newsletter/brevo-event", headers=ok,
                       json={"key": "errada", "event": "spam", "email": EMAIL}).status_code == 401
    assert client.post("/api/newsletter/brevo-event", headers=ok, json={
        "key": "brevo-secreto", "event": "spam", "email": EMAIL}).json() == {"suppressed": True}
    monkeypatch.setattr(settings, "newsletter_enabled", False)
    assert client.post("/api/newsletter/subscribe", headers=ok, json={
        "email": "x@example.com", "language": "en", "site_key": "ia"}).json() == {
        "ok": False, "error": "fechado"}


def test_rota_do_agendador_poe_o_envio_na_fila(nl, client, auth, monkeypatch):
    from app.core.cache import QUEUES

    calls: list = []
    monkeypatch.setattr(QUEUES["fast"], "enqueue",
                        lambda fn, **kw: calls.append(fn) or type("J", (), {"id": "rq1"})())
    response = client.post("/api/sched/newsletter", headers=auth)
    assert response.json() == {"queued": True, "rq_job": "rq1"}
    assert calls == [newsletter.send_daily]


def test_vigia_so_conta_a_newsletter_quando_ligada(nl, monkeypatch):
    assert "newsletter" in watchdog.limits()
    monkeypatch.setattr(settings, "newsletter_enabled", False)
    assert "newsletter" not in watchdog.limits()


def test_painel_mostra_contagens_sem_emails(nl, panel_client):
    out = Outbox()
    confirmed(out)
    subscribe("pendente@example.com")
    page = panel_client.get("/dashboard/health").text
    assert "Newsletter" in page and "cadastro aberto" in page
    # nenhum email de assinante (o menu do portal mostra só o do usuário logado, E18.4a)
    lowered = page.lower()
    assert "leitor.teste@example.com" not in lowered and "pendente@example.com" not in lowered
    assert lowered.count("example.com") == lowered.count("euclides@example.com")


def test_nenhum_email_aparece_nos_logs(nl, caplog):
    caplog.set_level(logging.DEBUG)
    out = Outbox()
    sid = confirmed(out)
    published_post()
    newsletter.send_daily(NOW, out, pause=lambda s: None)
    with session_scope() as db:
        newsletter.brevo_event(db, "hard_bounce", EMAIL)
    assert sid and "example.com" not in caplog.text.lower()


def _waiting_post(site: str, due: datetime, status: str = "AWAITING_APPROVAL") -> int:
    with session_scope() as db:
        return db.execute(text(
            """INSERT INTO publication_jobs (site_key, local_date, slot_number, due_at_utc,
                                             produce_after_utc, status)
               VALUES (:s, :d, 2, :a, :a, :st) RETURNING id"""),
            {"s": site, "d": due.date(), "a": due, "st": status}).scalar_one()


def _publish(job: int, at: datetime) -> None:
    with session_scope() as db:
        db.execute(text("UPDATE publication_jobs SET status = 'PUBLISHED_CONFIRMED', "
                        "published_at_utc = :a WHERE id = :j"), {"a": at, "j": job})
        for language, path in (("pt-BR", ""), ("en", "en/")):
            db.execute(text(
                """INSERT INTO job_translations (job_id, language, is_original, title, slug,
                                                 excerpt, html, status, wordpress_url)
                   VALUES (:j, :l, :o, 'Post do dia', 'post', 'Resumo', '<p>x</p>',
                           'published', :u)"""),
                {"j": job, "l": language, "o": language == "pt-BR",
                 "u": f"https://saude.shortsblog.com/{path}post-{job}/"})


def test_edicao_espera_os_posts_do_dia_a_espera_de_aprovacao(nl, monkeypatch):
    """23/11: a edição das 07:00 saiu com 1 post — os 6 do dia foram aprovados às 07:01.
    Com post do dia à espera, a edição fica retida e sai quando a fila esvazia."""
    monkeypatch.setattr(settings, "newsletter_hold_until", "10:00")
    out = Outbox()
    confirmed(out)
    out.sent.clear()
    seven = datetime(2026, 10, 22, 10, 0, tzinfo=UTC)                 # 07:00 em SP
    published_post("cloud", at=seven - timedelta(hours=2))             # das 05:00, já no ar
    waiting = _waiting_post("saude", due=seven - timedelta(hours=3))   # slot das 04:00
    _waiting_post("ia", due=seven - timedelta(hours=27), status="APPROVED")  # atrasado: não conta
    held = newsletter.send_daily(seven, out, pause=lambda s: None)
    assert held["held"] == 1 and held["sent"] == 0 and out.sent == []
    assert newsletter.held_ready(seven + timedelta(minutes=1)) is False   # ainda à espera
    _publish(waiting, seven + timedelta(minutes=1))
    later = seven + timedelta(minutes=2)
    assert newsletter.held_ready(later) is True
    assert newsletter.held_ready(later) is False                       # só um ganha
    result = newsletter.send_daily(later, out, pause=lambda s: None, final=True)
    assert result["sent"] == 1 and result["posts"] == 2                # o que já estava e o retido
    assert f"post-{waiting}/" in out.sent[-1]["body"]


def test_edicao_retida_sai_no_limite_mesmo_com_post_a_espera(nl, monkeypatch):
    monkeypatch.setattr(settings, "newsletter_hold_until", "10:00")
    out = Outbox()
    confirmed(out)
    out.sent.clear()
    seven = datetime(2026, 10, 22, 10, 0, tzinfo=UTC)
    published_post("cloud", at=seven - timedelta(hours=2))
    _waiting_post("saude", due=seven - timedelta(hours=3))
    assert newsletter.send_daily(seven, out, pause=lambda s: None)["held"] == 1
    assert newsletter.held_ready(seven + timedelta(hours=2, minutes=59)) is False
    assert newsletter.held_ready(seven + timedelta(hours=3)) is True       # 10:00 em SP
    # Depois do limite, mesmo sem `final`, a edição já não se retém.
    assert newsletter.send_daily(seven + timedelta(hours=3), out,
                                 pause=lambda s: None)["sent"] == 1
    monkeypatch.setattr(settings, "newsletter_hold_until", "")           # vazio desliga
    assert "held" not in newsletter.send_daily(seven + timedelta(days=1), out,
                                               pause=lambda s: None)


def test_publish_due_solta_a_edicao_retida(nl, monkeypatch, client, auth):
    queued: list = []
    monkeypatch.setattr("app.api.routes.sched.QUEUES",
                        {"fast": type("Q", (), {"enqueue": lambda self, f, **kw:
                                                queued.append((f, kw))})()})
    monkeypatch.setattr(newsletter, "held_ready", lambda now=None: True)
    body = client.post("/api/sched/publish-due", headers=auth).json()
    assert body["newsletter"] == "released"
    assert queued[0][0] is newsletter.send_daily and queued[0][1]["kwargs"] == {"final": True}
