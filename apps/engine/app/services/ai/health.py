"""Saúde dos provedores de IA (E3.6).

O probe corre no scheduler a cada 5 minutos e pergunta ao gateway, por
provedor, se há credencial válida — sem chamar nenhum modelo, para não gastar
a cota das assinaturas (R19). Cota esgotada só se vê numa chamada real: quem a
regista é a cadeia (`chain.run`), e o probe respeita esse estado durante um
período de espera antes de dar o provedor outra vez como bom.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import session_scope
from app.core.logging import get_logger
from app.services.ai import gateway
from app.services.ai.secrets import SecretUnreadable, decrypt

log = get_logger(__name__)

OK = "ok"
NOT_AUTHENTICATED = "not_authenticated"
QUOTA_EXHAUSTED = "quota_exhausted"
ERROR = "error"

# Estados que tiram o provedor da cadeia.
BLOCKING = (NOT_AUTHENTICATED, QUOTA_EXHAUSTED)

# O que o gateway responde em /v1/status → estado guardado.
_FROM_GATEWAY = {
    "ok": OK,
    "not_authenticated": NOT_AUTHENTICATED,
    "rate_limited": QUOTA_EXHAUSTED,
    "error": ERROR,
}
# Falha de uma chamada real → estado guardado (as restantes não mudam a saúde).
_FROM_CALL = {"not_authenticated": NOT_AUTHENTICATED, "rate_limited": QUOTA_EXHAUSTED}


def record(db: Session, name: str, status: str, source: str, detail: str = "") -> None:
    db.execute(
        text("""INSERT INTO ai_provider_health (provider_id, status, source, detail, checked_at)
                SELECT id, :s, :src, :d, now() FROM ai_providers WHERE name = :n
                ON CONFLICT (provider_id) DO UPDATE
                   SET status = EXCLUDED.status, source = EXCLUDED.source,
                       detail = EXCLUDED.detail, checked_at = EXCLUDED.checked_at"""),
        {"n": name, "s": status, "src": source, "d": detail[:500]},
    )


def record_call(name: str, error_kind: str | None) -> None:
    """Resultado de uma chamada real: sucesso (`None`) ou o tipo de falha."""
    status = OK if error_kind is None else _FROM_CALL.get(error_kind)
    if status is None:
        return
    try:
        with session_scope() as db:
            current = db.execute(
                text("""SELECT h.status FROM ai_provider_health h
                          JOIN ai_providers p ON p.id = h.provider_id WHERE p.name = :n"""),
                {"n": name},
            ).scalar()
            if current != status or status != OK:
                record(db, name, status, "call")
    except Exception:  # noqa: BLE001 — a saúde nunca pode derrubar a geração
        log.exception("Falha ao registar a saúde do provedor %s", name)


def probe_all(only: str | None = None, *, force: bool = False) -> dict[str, str]:
    """Verifica os provedores ativos (ou só `only`). Devolve {nome: estado guardado}.

    `force` ignora o período de espera: é o botão "Verificar" do painel, usado
    logo depois de o operador autenticar.
    """
    with session_scope() as db:
        rows = db.execute(
            text("""SELECT p.name, p.kind, p.model, p.base_url, p.api_key_enc,
                           h.status, h.source, h.checked_at
                      FROM ai_providers p
                      LEFT JOIN ai_provider_health h ON h.provider_id = p.id
                     WHERE p.enabled AND (CAST(:only AS TEXT) IS NULL OR p.name = :only)
                     ORDER BY p.id"""),
            {"only": only},
        ).mappings().all()

    cooldown = timedelta(minutes=settings.ai_cooldown_min)
    now = datetime.now(UTC)
    results: dict[str, str] = {}
    for row in rows:
        name = row["name"]
        status, detail = _probe(dict(row))
        held = (
            not force
            and status == OK
            and row["source"] == "call"
            and row["status"] in BLOCKING
            and now - row["checked_at"] < cooldown
        )
        if held:
            # A verificação barata diz "há credencial", mas uma chamada real falhou
            # há pouco: mantém o provedor fora da cadeia até ao fim da espera.
            results[name] = row["status"]
            continue
        if status != row["status"]:
            log.warning("IA: provedor %s passou de %s para %s %s",
                        name, row["status"] or "desconhecido", status, detail)
        with session_scope() as db:
            record(db, name, status, "probe", detail)
        results[name] = status
    return results


def _probe(row: Mapping[str, Any]) -> tuple[str, str]:
    endpoint = None
    if row["kind"] == "api":
        if not row["api_key_enc"] or not row["base_url"]:
            return NOT_AUTHENTICATED, "sem API key"
        try:
            endpoint = gateway.Endpoint(row["base_url"], decrypt(row["api_key_enc"]), row["model"])
        except SecretUnreadable:
            return NOT_AUTHENTICATED, "API key ilegível"
    try:
        status, detail = gateway.status(row["name"], endpoint)
    except gateway.GatewayError as exc:
        return ERROR, f"gateway: {exc.kind}"
    return _FROM_GATEWAY.get(status, ERROR), detail
