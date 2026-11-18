"""Provedores de IA — leitura da cadeia por papel e do seu estado (E3.5).

A edição (ordem, ligar/desligar, API keys) entra com o painel da E3.7.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.core.security import require_api_token
from app.services.ai import chain, quota

router = APIRouter(prefix="/api/ai", tags=["ai"], dependencies=[Depends(require_api_token)])


@router.get("/chain", summary="Cadeia de provedores por papel e problemas de configuração")
def get_chain(db: Session = Depends(get_db)) -> dict:
    chains = {role: chain.load_chain(db, role) for role in chain.ROLES}
    return {
        "roles": {
            role: [
                # Nunca devolve api_key_enc; só se há chave guardada.
                {"name": p.name, "kind": p.kind, "family": p.family, "model": p.model,
                 "enabled": p.enabled, "has_api_key": bool(p.api_key_enc),
                 "health": p.health or "unknown",
                 "limit_reached": quota.blocked(db, p)}
                for p in providers
            ]
            for role, providers in chains.items()
        },
        "problems": chain.problems(chains),
    }
