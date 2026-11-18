"""Autenticação máquina-a-máquina e assinatura de links públicos.

O painel fica atrás da autenticação da borda (D-018: Cloudflare Access + login).
As rotas `/api/**` exigem, além disso, o bearer token — é assim que o n8n
fala com o motor.
"""
from __future__ import annotations

import hashlib

from fastapi import Header, HTTPException, status
from itsdangerous import BadSignature, URLSafeTimedSerializer

from app.core.config import settings

_serializer = URLSafeTimedSerializer(settings.sb_secret_key, salt="sb-links")


async def require_api_token(authorization: str = Header(default="")) -> None:
    """Dependência do FastAPI para as rotas de máquina (token do cofre ou do ambiente; durante a
    carência de uma troca, o anterior também vale — E18.3b)."""
    from app.services import vault

    if not vault.get("sb_api_token"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SB_API_TOKEN não configurado — API de máquina desativada.",
        )
    if not vault.bearer_matches("sb_api_token", authorization):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")


def sign(payload: dict) -> str:
    return _serializer.dumps(payload)


def unsign(token: str, max_age: int = 60 * 60 * 24 * 30) -> dict:
    try:
        return _serializer.loads(token, max_age=max_age)
    except BadSignature as exc:
        raise HTTPException(status_code=400, detail="Link inválido ou expirado") from exc


def hash_ip(ip: str) -> str:
    """Nunca guardamos o IP em claro (LGPD/GDPR) — só um hash com sal."""
    return hashlib.sha256(f"{settings.sb_secret_key}:{ip}".encode()).hexdigest()[:32]
