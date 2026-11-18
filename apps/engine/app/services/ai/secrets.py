"""Cifra das API keys dos provedores de IA guardadas na base (E3.5/E3.7).

Fernet (AES-128-CBC + HMAC) com chave derivada de `SB_SECRET_KEY`. Rodar o
`SB_SECRET_KEY` invalida as chaves guardadas: é preciso voltar a introduzi-las.
"""
from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


class SecretUnreadable(RuntimeError):
    """O valor cifrado não abre com o `SB_SECRET_KEY` atual."""


PURPOSE = "sb-ai-provider-keys"


def fernet_for(purpose: str, secret_key: str | None = None) -> Fernet:
    """Fernet de um propósito, derivado do `SB_SECRET_KEY` (ou de outra chave, na rotação)."""
    key = settings.sb_secret_key if secret_key is None else secret_key
    digest = hashlib.sha256(f"{purpose}:{key}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(plaintext: str, secret_key: str | None = None) -> str:
    return fernet_for(PURPOSE, secret_key).encrypt(plaintext.encode()).decode()


def decrypt(token: str, secret_key: str | None = None) -> str:
    try:
        return fernet_for(PURPOSE, secret_key).decrypt(token.encode()).decode()
    except InvalidToken:
        raise SecretUnreadable("API key cifrada ilegível (SB_SECRET_KEY mudou?)") from None
