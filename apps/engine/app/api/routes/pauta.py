"""Pauta por e-mail (D-094): webhook de entrada do Brevo, via WordPress.

Quem chama é o mu-plugin `sb-pauta.php`, pela rede interna, com o mesmo token
WordPress↔motor da newsletter (`SB_NEWSLETTER_API_TOKEN`). Além do Bearer, cada
pedido traz a chave que o Brevo manda no cabeçalho `X-SB-Pauta-Key` do webhook
(`pauta_webhook_token`, segredo próprio; R61 — antes ia no endereço, `?k=`); sem ela
configurada, a rota responde 503 e nada entra.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.api.routes.newsletter import require_newsletter_token
from app.core.cache import incr_daily
from app.services import pauta, vault

router = APIRouter(prefix="/api/pauta", tags=["pauta"],
                   dependencies=[Depends(require_newsletter_token)])


class InboundEmail(BaseModel):
    key: str = Field(max_length=200)
    # Por onde a chave chegou ao relay (R61): cabeçalho `X-SB-Pauta-Key` ou `?k=` no endereço.
    key_via: Literal["header", "query", ""] = ""
    sender: str = Field(default="", max_length=320)      # endereço do From
    subject: str = Field(default="", max_length=500)
    html: str = Field(default="", max_length=1_000_000)  # corpo HTML do e-mail
    # Valores de `Authentication-Results` do e-mail; o motor só confia no do Brevo (R62).
    auth: list[str] = Field(default_factory=list)


@router.post("/brevo-inbound", summary="E-mail de newsletter vira ideias «só pauta»")
def brevo_inbound(body: InboundEmail) -> dict:
    if not vault.get("pauta_webhook_token"):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Pauta por e-mail desligada")
    match = vault.token_match("pauta_webhook_token", body.key)
    if not match:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Chave do webhook inválida")
    # R61: conta por onde a chave chegou (só com «header» ela pode sair do endereço) e qual foi
    # (só com «current» a carência de uma troca pode acabar: scripts/pauta-webhook-rotate.sh).
    incr_daily(f"pauta_key_via:{body.key_via or 'unknown'}")
    incr_daily(f"pauta_key_match:{match}")
    # O processamento (remetente, SPF/DKIM, desembrulho dos links) corre no worker `fast`:
    # o relay do WordPress tem 15 s e o desembrulho faz HTTP externo (revisão E14).
    pauta.enqueue_ingest(sender=body.sender, subject=body.subject, html=body.html,
                         auth=[value[:2000] for value in body.auth[:20]])
    return {"accepted": True, "queued": True}
