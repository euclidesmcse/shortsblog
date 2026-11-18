"""Prontidão real das fases do motor, para o painel e o `/health/ready`.

A redação e a revisão passam pelo AI Gateway (assinaturas, sem chave de API) e o WordPress usa uma
credencial por site (`SB_WP_<SITE>_APP_USER/_APP_PASSWORD`). Os `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY` e `WORDPRESS_*` herdados do Growth Engine ficam vazios e não dizem nada.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services import vault
from app.services.ai import chain


def role_ready(db: Session, role: str) -> dict[str, Any]:
    """Pronto se algum provedor ligado da cadeia do papel passou na última sonda."""
    usable = [p.name for p in chain.load_chain(db, role) if p.enabled and p.health == "ok"]
    return {"ok": bool(usable), "providers": usable}


def wordpress_ready(db: Session) -> dict[str, Any]:
    """Quantos sites têm credencial do WordPress (cofre ou ambiente) (a publicação precisa dela)."""
    keys = [r[0] for r in db.execute(text("SELECT site_key FROM sites ORDER BY site_key"))]
    have = [k for k in keys if all(vault.wp_credentials(k))]
    return {"ok": bool(keys) and len(have) == len(keys), "have": len(have), "total": len(keys),
            "missing": sorted(set(keys) - set(have))}


def summary(db: Session) -> dict[str, Any]:
    return {"writer": role_ready(db, "writer"), "reviewer": role_ready(db, "reviewer"),
            "wordpress": wordpress_ready(db)}
