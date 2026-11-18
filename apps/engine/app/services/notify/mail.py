"""Envio de e-mail transacional ao operador (aprovações, resumo diário).

Dois canais: API transacional do Brevo (se `BREVO_API_KEY`) ou SMTP (o relay
SMTP do Brevo serve). Sem nenhum dos dois, o envio falha com `MailError` —
quem chama decide se isso é fatal.
"""
from __future__ import annotations

import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.services import vault
from app.utils import http

log = get_logger(__name__)


class MailError(RuntimeError):
    """O envio falhou de certeza (não saiu nada): quem chama pode repetir."""


class MailUnknown(MailError):
    """SEND_UNKNOWN: não se sabe se o e-mail saiu (timeout depois do pedido, ligação cortada, 5xx).

    Nunca se repete sozinho (repetir pode duplicar): confirma-se na caixa de entrada ou no Brevo.
    """


SMTP_KEY_PREFIX = vault.SMTP_KEY_PREFIX


def _use_api() -> bool:
    key = vault.get("brevo_api_key")
    return bool(key) and not key.startswith(SMTP_KEY_PREFIX)


def _smtp_password() -> str:
    """A senha do relay: `SMTP_PASSWORD` ou, se a chave guardada é uma chave SMTP do Brevo, essa."""
    password = vault.get("smtp_password")
    if password:
        return password
    key = vault.get("brevo_api_key")
    return key if key.startswith(SMTP_KEY_PREFIX) else ""


def _sender_name() -> str:
    return vault.get("mail_from_name") or settings.brand_name


def send_transactional(to_email: str, to_name: str, subject: str, html: str, *,
                       headers: dict[str, str] | None = None, from_email: str = "",
                       from_name: str = "", text_body: str = "") -> str:
    """Envio unitário (Brevo API; SMTP como alternativa). Devolve a referência da mensagem.

    `headers` vão na mensagem (a newsletter manda `List-Unsubscribe`, RFC 8058); `from_*` trocam o
    remetente (a newsletter tem o seu); `text_body` é a versão em texto simples.
    """
    sender_email = from_email or vault.get("mail_from_email")
    sender_name = from_name or _sender_name()
    if not sender_email:
        raise MailError("MAIL_FROM_EMAIL não configurado")

    if _use_api():
        body: dict[str, object] = {
            "sender": {"name": sender_name, "email": sender_email},
            "to": [{"email": to_email, "name": to_name or to_email}],
            "subject": subject,
            "htmlContent": html,
        }
        if text_body:
            body["textContent"] = text_body
        if headers:
            body["headers"] = headers
        try:
            response = http.post_json(
                "https://api.brevo.com/v3/smtp/email",
                json_body=body,
                headers={"api-key": vault.get("brevo_api_key")},
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise MailError(f"Brevo: sem ligação ({type(exc).__name__})") from exc
        except httpx.HTTPError as exc:   # o pedido pode ter chegado (timeout de leitura, corte)
            raise MailUnknown(f"Brevo: resultado incerto ({type(exc).__name__})") from exc
        if response.status_code >= 500:
            raise MailUnknown(f"Brevo envio {response.status_code}: resultado incerto")
        if response.status_code >= 300:
            raise MailError(f"Brevo envio {response.status_code}: {response.text[:200]}")
        try:
            return str(response.json().get("messageId") or "enviado")
        except (ValueError, AttributeError):
            return "enviado"

    if vault.get("smtp_host"):
        return _send_smtp(to_email, to_name, subject, html, headers=headers,
                          sender=f"{sender_name} <{sender_email}>", text_body=text_body)

    raise MailError("Nenhum canal de envio configurado (BREVO_API_KEY ou SMTP_HOST)")


def _send_smtp(to_email: str, to_name: str, subject: str, html: str, *,
               headers: dict[str, str] | None = None, sender: str = "",
               text_body: str = "") -> str:
    import smtplib
    import ssl
    from email.message import EmailMessage

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender or f"{_sender_name()} <{vault.get('mail_from_email')}>"
    message["To"] = f"{to_name} <{to_email}>" if to_name else to_email
    for name, value in (headers or {}).items():
        message[name] = value
    message.set_content(text_body or "O seu cliente de e-mail não suporta HTML.")
    message.add_alternative(html, subtype="html")

    try:
        with smtplib.SMTP(vault.get("smtp_host"), vault.get_int("smtp_port", 587),
                          timeout=30) as smtp:
            # Contexto padrão: confere o certificado e o nome do servidor. Sem ele o Python aceita
            # qualquer certificado e a chave SMTP vazaria num MITM (revisão final da E18, achado 5).
            smtp.starttls(context=ssl.create_default_context())
            user = vault.get("smtp_user")
            if user:
                smtp.login(user, _smtp_password())
            smtp.send_message(message)
    except (smtplib.SMTPServerDisconnected, TimeoutError) as exc:
        raise MailUnknown(f"SMTP: resultado incerto ({type(exc).__name__})") from exc
    except (smtplib.SMTPException, OSError) as exc:
        raise MailError(f"SMTP: {type(exc).__name__}: {str(exc)[:150]}") from exc
    return "enviado"
