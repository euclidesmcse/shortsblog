"""Envio de e-mail transacional: Brevo (API) ou SMTP genérico."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.notify import mail


def test_mail_via_brevo(monkeypatch):
    captured = {}

    def post_json(url, *, json_body, headers=None, timeout=None):
        captured.update(url=url, body=json_body, headers=headers)
        return SimpleNamespace(status_code=201, text="")

    monkeypatch.setattr(mail.http, "post_json", post_json)
    monkeypatch.setattr(settings, "mail_from_email", "motor@shortsblog.com")
    monkeypatch.setattr(settings, "brevo_api_key", "chave")
    assert mail.send_transactional("e@example.com", "", "Assunto", "<p>x</p>")
    assert captured["url"].endswith("/v3/smtp/email")
    assert captured["body"]["sender"] == {"name": "ShortsBlog",
                                          "email": "motor@shortsblog.com"}
    assert captured["headers"] == {"api-key": "chave"}
    assert "headers" not in captured["body"]
    # E17: a newsletter manda List-Unsubscribe, o seu remetente e a versão em texto.
    mail.send_transactional("e@example.com", "", "A", "<p/>", from_email="news@shortsblog.com",
                            from_name="News", text_body="texto",
                            headers={"List-Unsubscribe": "<https://x.test/u>"})
    assert captured["body"]["sender"] == {"name": "News", "email": "news@shortsblog.com"}
    assert captured["body"]["headers"] == {"List-Unsubscribe": "<https://x.test/u>"}
    assert captured["body"]["textContent"] == "texto"

    monkeypatch.setattr(mail.http, "post_json",
                        lambda *a, **k: SimpleNamespace(status_code=401, text="chave inválida"))
    with pytest.raises(mail.MailError, match="401"):
        mail.send_transactional("e@example.com", "", "A", "<p/>")


def test_mail_via_smtp(monkeypatch):
    import smtplib

    sent_messages = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            self.host = host

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def starttls(self, context=None):
            pass

        def login(self, user, password):
            self.user = user

        def send_message(self, message):
            sent_messages.append(message)

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(settings, "mail_from_email", "motor@shortsblog.com")
    monkeypatch.setattr(settings, "brevo_api_key", "")
    monkeypatch.setattr(settings, "smtp_host", "smtp-relay.brevo.com")
    monkeypatch.setattr(settings, "smtp_user", "u")
    assert mail.send_transactional("e@example.com", "Euclides", "Olá", "<p>x</p>")
    assert sent_messages[0]["To"] == "Euclides <e@example.com>"
    mail.send_transactional("e@example.com", "", "A", "<p/>", from_email="news@shortsblog.com",
                            from_name="News", headers={"List-Unsubscribe-Post":
                                                       "List-Unsubscribe=One-Click"})
    assert sent_messages[1]["From"] == "News <news@shortsblog.com>"
    assert sent_messages[1]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"


def test_mail_sem_configuracao(monkeypatch):
    monkeypatch.setattr(settings, "mail_from_email", "")
    with pytest.raises(mail.MailError, match="MAIL_FROM_EMAIL"):
        mail.send_transactional("e@example.com", "", "A", "<p/>")
    monkeypatch.setattr(settings, "mail_from_email", "motor@shortsblog.com")
    monkeypatch.setattr(settings, "brevo_api_key", "")
    monkeypatch.setattr(settings, "smtp_host", "")
    with pytest.raises(mail.MailError, match="Nenhum canal"):
        mail.send_transactional("e@example.com", "", "A", "<p/>")
