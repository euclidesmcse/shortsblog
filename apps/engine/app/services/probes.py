"""Sondas dos blogs e do painel (E12.2): o motor testa-os de minuto a minuto, de fora para dentro.

O resultado fica no Redis (`sb:probe:<nome>`) para o painel de saúde, as métricas e os alertas.
Um blog
em baixo (por exemplo, a base de dados parada) responde 5xx ou não responde; o Cloudflare à frente
devolve 52x. Também se vigia a validade do certificado que os visitantes recebem.
"""

from __future__ import annotations

import contextlib
import json
import socket
import ssl
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.cache import redis_client
from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

TIMEOUT_S = 10.0
CERT_TTL_S = 6 * 3600  # o certificado só se confere de 6 em 6 horas
Fetcher = Callable[[str], dict[str, Any]]
_transport: httpx.BaseTransport | None = None  # os testes trocam por um servidor de mentira


def targets(db: Session) -> dict[str, str]:
    """Nome → endereço a sondar: a página inicial de cada blog e o `/health/live` do painel."""
    rows = db.execute(text("SELECT site_key, base_url FROM sites ORDER BY site_key")).all()
    stamp = int(time.time())
    found = {key: f"{str(url).rstrip('/')}/?sbprobe={stamp}" for key, url in rows}
    found["painel"] = settings.public_base_url.rstrip("/") + "/health/live"
    return found


PLACEHOLDER = (
    "em construção"  # a página que o Traefik serve quando o WordPress de um blog está em baixo
)


def fetch(url: str) -> dict[str, Any]:
    """Um GET sem seguir redirecionamentos. `ok`: 2xx ou 3xx e que não seja o placeholder.

    O Cloudflare serve a página em cache e, com o WordPress parado, o Traefik cai no placeholder
    ("em construção", 200): por isso o endereço leva um parâmetro novo (fura a cache) e o
    corpo conta.
    """
    started = time.monotonic()
    try:
        with httpx.Client(
            timeout=TIMEOUT_S,
            follow_redirects=False,
            transport=_transport,
            headers={"User-Agent": "ShortsBlogProbe/1.0"},
        ) as client:
            response = client.get(url)
        placeholder = PLACEHOLDER in response.text[:4000].lower()
        return {
            "ok": 200 <= response.status_code < 400 and not placeholder,
            "status": response.status_code,
            "ms": int((time.monotonic() - started) * 1000),
            "error": "placeholder (WordPress em baixo)" if placeholder else "",
        }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "status": 0,
            "ms": int((time.monotonic() - started) * 1000),
            "error": type(exc).__name__,
        }


def run(db: Session, now: float | None = None, probe: Fetcher | None = None) -> dict[str, dict]:
    """Sonda tudo em paralelo e guarda. Mantém `fail_since` (desde quando falha)."""
    moment = now or time.time()
    urls = targets(db)
    probe = probe or fetch
    with ThreadPoolExecutor(max_workers=max(len(urls), 1)) as pool:
        raw = dict(zip(urls, pool.map(probe, urls.values()), strict=True))
    out: dict[str, dict] = {}
    for name, result in raw.items():
        previous = load(name)
        record = {
            **result,
            "url": urls[name],
            "checked_at": moment,
            "fail_since": None if result["ok"] else (previous or {}).get("fail_since") or moment,
        }
        try:
            redis_client.set(f"sb:probe:{name}", json.dumps(record), ex=3600)
        except Exception:  # noqa: BLE001 — sem Redis ainda se devolve o resultado
            log.warning("Sonda %s: não foi possível guardar o resultado", name)
        out[name] = record
    return out


def load(name: str) -> dict | None:
    try:
        raw = redis_client.get(f"sb:probe:{name}")
    except Exception:  # noqa: BLE001
        return None
    return json.loads(raw) if raw else None


def results(db: Session) -> dict[str, dict | None]:
    return {name: load(name) for name in targets(db)}


# --- Certificado ------------------------------------------------------------------------------


def fetch_cert_days(host: str) -> float:
    """Dias até o certificado que o `host` apresenta expirar."""
    context = ssl.create_default_context()
    with (
        socket.create_connection((host, 443), timeout=TIMEOUT_S) as sock,
        context.wrap_socket(sock, server_hostname=host) as tls,
    ):
        cert = tls.getpeercert() or {}
    not_after = str(cert.get("notAfter", ""))
    if not not_after:
        raise ssl.SSLError("o servidor não apresentou certificado")
    expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)
    return (expires - datetime.now(UTC)).total_seconds() / 86400


def cert_days(
    hosts: list[str], now: float | None = None, fetcher: Callable[[str], float] | None = None
) -> dict[str, float]:
    """Dias de validade por host, com cache de 6 horas no Redis."""
    moment = now or time.time()
    fetcher = fetcher or fetch_cert_days
    out: dict[str, float] = {}
    for host in hosts:
        key = f"sb:cert:{host}"
        try:
            raw = redis_client.get(key)
        except Exception:  # noqa: BLE001
            raw = None
        if raw:
            cached = json.loads(raw)
            if moment - cached["at"] < CERT_TTL_S:
                out[host] = cached["days"]
                continue
        try:
            days = fetcher(host)
        except Exception as exc:  # noqa: BLE001 — sem ligação não se sabe: não se avisa por isto
            log.warning("Certificado de %s: %s", host, type(exc).__name__)
            continue
        out[host] = days
        with contextlib.suppress(Exception):
            redis_client.set(key, json.dumps({"days": days, "at": moment}), ex=CERT_TTL_S * 2)
    return out


def hostnames(db: Session) -> list[str]:
    return sorted({urlsplit(url).hostname or "" for url in targets(db).values()} - {""})
