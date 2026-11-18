"""Pauta por e-mail (D-094): webhook de entrada do Brevo, via WordPress.

Quem chama é o mu-plugin `sb-pauta.php`, pela rede interna, com o mesmo token
WordPress↔motor da newsletter (`SB_NEWSLETTER_API_TOKEN`). Além do Bearer, cada
pedido traz a chave que o Brevo põe no endereço do webhook (`pauta_webhook_token`,
segredo próprio); sem ela configurada, a rota responde 503 e nada entra.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.routes.newsletter import require_newsletter_token
from app.services import pauta, vault

router = APIRouter(prefix="/api/pauta", tags=["pauta"],
                   dependencies=[Depends(require_newsletter_token)])


class InboundEmail(BaseModel):
    key: str = Field(max_length=200)
    sender: str = Field(default="", max_length=320)      # endereço do From
    subject: str = Field(default="", max_length=500)
    html: str = Field(default="", max_length=1_000_000)  # corpo HTML do e-mail
    # Valores de `Authentication-Results` do e-mail; o motor só confia no do Brevo (R62).
    auth: list[str] = Field(default_factory=list)


@router.post("/brevo-inbound", summary="E-mail de newsletter vira ideias «só pauta»")
def brevo_inbound(body: InboundEmail) -> dict:
    if not vault.get("pauta_webhook_token"):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Pauta por e-mail desligada")
    if not vault.token_matches("pauta_webhook_token", body.key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Chave do webhook inválida")
    # O processamento (remetente, SPF/DKIM, desembrulho dos links) corre no worker `fast`:
    # o relay do WordPress tem 15 s e o desembrulho faz HTTP externo (revisão E14).
    pauta.enqueue_ingest(sender=body.sender, subject=body.subject, html=body.html,
                         auth=[value[:2000] for value in body.auth[:20]])
    return {"accepted": True, "queued": True}
