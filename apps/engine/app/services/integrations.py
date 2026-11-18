"""E18.3b: «testar conexão» das integrações da engrenagem — sem enviar nada, sem gravar nada.

- E-mail: com chave de API do Brevo, `GET /v3/account`; senão SMTP (conecta, STARTTLS, login, sai).
- WordPress: `GET /wp/v2/users/me?context=edit` com a credencial, pelo mesmo cliente da publicação
  (mesmo destino validado da tabela `sites`); confere que o usuário pode publicar.

As mensagens nunca trazem o valor de um segredo.
"""
from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from typing import Any

import httpx

from app.services import sitepub
from app.services.notify.mail import SMTP_KEY_PREFIX

BREVO_ACCOUNT = "https://api.brevo.com/v3/account"
_transport: httpx.BaseTransport | None = None   # os testes trocam por um Brevo de mentira
_smtp_factory: Any = smtplib.SMTP              # idem, por um SMTP de mentira
PUBLISHING_ROLES = {"author", "editor", "administrator"}


@dataclass
class Check:
    ok: bool
    message: str


def check_email(values: dict[str, str]) -> Check:
    """`values`: os itens de e-mail como ficariam (o que está no formulário sobre o atual)."""
    key = values.get("brevo_api_key", "")
    if key and not key.startswith(SMTP_KEY_PREFIX):
        try:
            with httpx.Client(transport=_transport, timeout=15) as client:
                response = client.get(BREVO_ACCOUNT, headers={"api-key": key,
                                                              "accept": "application/json"})
        except httpx.HTTPError as exc:
            return Check(False, f"Brevo: sem ligação ({type(exc).__name__})")
        if response.status_code == 200:
            return Check(True, "Brevo: chave da API aceita.")
        return Check(False, f"Brevo recusou a chave (HTTP {response.status_code}).")
    host = values.get("smtp_host", "")
    if not host:
        return Check(False, "Nenhum canal: falta a chave da API do Brevo ou o servidor SMTP.")
    try:
        port = int(values.get("smtp_port") or 587)
    except ValueError:
        return Check(False, "Porta SMTP inválida.")
    user = values.get("smtp_user", "")
    password = values.get("smtp_password", "") or (key if key.startswith(SMTP_KEY_PREFIX) else "")
    try:
        with _smtp_factory(host, port, timeout=15) as smtp:
            smtp.starttls(context=ssl.create_default_context())   # confere o certificado
            if user:
                smtp.login(user, password)
    except ssl.SSLError as exc:
        return Check(False, f"SMTP {host}:{port}: certificado TLS recusado ({type(exc).__name__})")
    except smtplib.SMTPAuthenticationError:
        return Check(False, f"SMTP {host}:{port}: usuário ou senha recusados.")
    except (smtplib.SMTPException, OSError) as exc:
        return Check(False, f"SMTP {host}:{port}: {type(exc).__name__}")
    who = f" como {user}" if user else ""
    return Check(True, f"SMTP {host}:{port}: conectou com STARTTLS{who}.")


def check_wordpress(site: dict[str, Any], user: str, password: str) -> Check:
    if not user or not password:
        return Check(False, "Falta o usuário ou a Application Password.")
    try:
        wp = sitepub.WordPress(site, credentials=(user, password))
        me = wp.call("GET", "/wp/v2/users/me", params={"context": "edit"})
    except sitepub.SiteSuspended:
        return Check(False, "O WordPress recusou a credencial (401/403).")
    except sitepub.PublishError as exc:
        return Check(False, f"WordPress: {exc}")
    roles = set(me.get("roles") or []) if isinstance(me, dict) else set()
    name = me.get("slug") or me.get("name") or user if isinstance(me, dict) else user
    if not roles & PUBLISHING_ROLES:
        return Check(False, f"Credencial aceita ({name}), mas o papel {sorted(roles)} não publica.")
    return Check(True, f"Credencial aceita: {name} ({', '.join(sorted(roles))}).")
