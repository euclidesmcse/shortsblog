"""API da newsletter para o WordPress (E17.2–E17.4, E17.7).

Quem chama é o mu-plugin `sb-newsletter.php` de cada blog, pela rede interna, com o token próprio
da newsletter (`NEWSLETTER_API_TOKEN`; o `SB_API_TOKEN` do n8n não serve aqui). O leitor nunca fala
com o motor: o formulário, a página de confirmação e a de saída ficam no domínio do blog.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db
from app.services import newsletter, vault


async def require_newsletter_token(authorization: str = Header(default="")) -> None:
    if not vault.get("newsletter_api_token"):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "NEWSLETTER_API_TOKEN não configurado")
    if not vault.bearer_matches("newsletter_api_token", authorization):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido")


router = APIRouter(prefix="/api/newsletter", tags=["newsletter"],
                   dependencies=[Depends(require_newsletter_token)])


class SubscribeRequest(BaseModel):
    email: str = Field(max_length=320)
    language: str = Field(max_length=8)
    site_key: str = Field(max_length=32)
    ip: str = Field(default="", max_length=64)
    honeypot: str = Field(default="", max_length=200)


class TokenRequest(BaseModel):
    token: str = Field(max_length=200)


class BrevoEvent(BaseModel):
    key: str = Field(max_length=200)
    event: str = Field(default="", max_length=64)
    email: str = Field(default="", max_length=320)


@router.get("/status", summary="O cadastro está aberto?")
def status_() -> dict:
    return {"open": settings.newsletter_enabled}


@router.post("/subscribe", summary="Pedido de cadastro (resposta igual exista o e-mail ou não)")
def subscribe(body: SubscribeRequest, db: Session = Depends(get_db)) -> dict:
    try:
        result = newsletter.subscribe(db, email=body.email, language=body.language,
                                      site_key=body.site_key, ip=body.ip,
                                      honeypot=body.honeypot)
    except newsletter.Closed:
        return {"ok": False, "error": "fechado"}
    db.commit()
    return result


@router.post("/check/{action}", summary="Estado de um link (não muda nada)")
def check(action: str, body: TokenRequest, db: Session = Depends(get_db)) -> dict:
    if action not in ("confirm", "unsubscribe"):
        raise HTTPException(404, "Ação desconhecida")
    return newsletter.check(db, body.token, action)


@router.post("/confirm", summary="Confirma a inscrição (POST do botão)")
def confirm(body: TokenRequest, db: Session = Depends(get_db)) -> dict:
    result = newsletter.confirm(db, body.token)
    db.commit()
    return result


@router.post("/unsubscribe", summary="Saída de 1 clique")
def unsubscribe(body: TokenRequest, db: Session = Depends(get_db)) -> dict:
    result = newsletter.unsubscribe(db, body.token)
    db.commit()
    return result


@router.post("/brevo-event", summary="Webhook do Brevo (bounce, reclamação), via WordPress")
def brevo_event(body: BrevoEvent, db: Session = Depends(get_db)) -> dict:
    if not vault.token_matches("newsletter_webhook_token", body.key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Chave do webhook inválida")
    changed = newsletter.brevo_event(db, body.event, body.email)
    db.commit()
    return {"suppressed": changed}
