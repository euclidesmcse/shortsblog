"""Purge do cache do Cloudflare quando um post sai (D-097; automatiza o manual da D-048).

Desligado por omissão: só age com `cloudflare_purge_token` configurado — um token PRÓPRIO,
separado do de DNS (D-048), com as permissões `Zone → Zone → Read` e
`Zone → Cache Purge → Purge`, restrito à zona `shortsblog.com`.

O purge é cosmético: sem ele a página nova aparece em até 1 h, quando o cache expira.
Por isso NADA aqui bloqueia nem atrasa a publicação — qualquer falha vira um log e segue.
"""
from __future__ import annotations

import httpx

from app.core import cache
from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_API = "https://api.cloudflare.com/client/v4"
_BATCH = 30                      # URLs por chamada (limite da API)
_transport: httpx.BaseTransport | None = None


def enabled() -> bool:
    return bool(settings.cloudflare_purge_token.strip())


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.cloudflare_purge_token.strip()}",
            "Content-Type": "application/json"}


def _zone_id(client: httpx.Client) -> str:
    """O id da zona: o configurado, o guardado no Redis, ou o resolvido pelo nome."""
    configured = settings.cloudflare_zone_id.strip()
    if configured:
        return configured
    cached = cache.cache_get("cloudflare:zone_id")
    if isinstance(cached, str) and cached:
        return cached
    reply = client.get(f"{_API}/zones", params={"name": settings.cloudflare_zone},
                       headers=_headers())
    reply.raise_for_status()
    zones = reply.json().get("result") or []
    zone = str(zones[0].get("id", "")) if zones else ""
    if zone:
        cache.cache_set("cloudflare:zone_id", zone, ttl=7 * 86400)
    return zone


def purge_urls(urls: list[str]) -> bool:
    """Tira as URLs do cache da borda. Nunca levanta; falha = log + False."""
    if not enabled() or not urls:
        return False
    try:
        with httpx.Client(timeout=10, transport=_transport) as client:
            zone = _zone_id(client)
            if not zone:
                log.warning("Purge do Cloudflare: zona %r não encontrada",
                            settings.cloudflare_zone)
                return False
            for start in range(0, len(urls), _BATCH):
                reply = client.post(f"{_API}/zones/{zone}/purge_cache",
                                    json={"files": urls[start:start + _BATCH]},
                                    headers=_headers())
                reply.raise_for_status()
    except Exception as exc:  # noqa: BLE001 — o purge nunca bloqueia a publicação
        log.warning("Purge do Cloudflare falhou (%s): %s", type(exc).__name__, str(exc)[:200])
        return False
    log.info("Purge do Cloudflare: %d URLs", len(urls))
    return True
