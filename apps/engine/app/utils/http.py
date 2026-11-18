"""Cliente HTTP compartilhado, educado e resiliente.

Todas as coletas públicas (Trends, Reddit, autosuggest, SERP) passam por
aqui: User-Agent identificável, rate-limit global e retry com backoff.
"""
from __future__ import annotations

import json
import threading
import time
from urllib.parse import urlparse

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

_lock = threading.Lock()
_last_request_at: dict[str, float] = {}

# Intervalo mínimo (segundos) entre requisições ao MESMO host. Hosts que não
# estão aqui usam o padrão derivado de HTTP_MAX_RPS.
#
# O Reddit devolve 429 já na segunda requisição em menos de um segundo, mesmo
# no feed RSS — daí o intervalo bem mais folgado. Rate-limit global único não
# resolveria: seria lento demais para o autosuggest (centenas de chamadas por
# execução) e ainda assim agressivo demais para o Reddit.
PER_HOST_MIN_INTERVAL = {
    "www.reddit.com": 12.0,
    "old.reddit.com": 12.0,
    "oauth.reddit.com": 1.0,
    "html.duckduckgo.com": 2.0,
    "trends.google.com": 2.0,
}


def _throttle(url: str) -> None:
    """Rate-limit por host: educado com cada fonte, sem penalizar as outras."""
    if settings.http_max_rps <= 0:
        return
    host = urlparse(url).netloc.lower()
    min_interval = PER_HOST_MIN_INTERVAL.get(host, 1.0 / settings.http_max_rps)
    with _lock:
        elapsed = time.monotonic() - _last_request_at.get(host, 0.0)
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        _last_request_at[host] = time.monotonic()


def _headers(extra: dict | None = None) -> dict:
    headers = {
        "User-Agent": settings.http_user_agent,
        "Accept-Language": f"{settings.content_locale},pt;q=0.9,en;q=0.8",
    }
    if extra:
        headers.update(extra)
    return headers


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    reraise=True,
)
def get(url: str, *, params: dict | None = None, headers: dict | None = None) -> httpx.Response:
    _throttle(url)
    with httpx.Client(timeout=settings.http_timeout, follow_redirects=True) as client:
        response = client.get(url, params=params, headers=_headers(headers))
        # 4xx (exceto 429) não vale retry — é erro nosso, não do servidor.
        if response.status_code >= 500 or response.status_code == 429:
            response.raise_for_status()
        return response


def get_json(url: str, *, params: dict | None = None, headers: dict | None = None):
    response = get(url, params=params, headers=headers)
    if response.status_code != 200:
        log.warning("GET %s devolveu %s", url, response.status_code)
        return None
    try:
        # `.text` respeita o charset declarado no Content-Type; `.json()` do
        # httpx assume UTF-8 e quebra em endpoints legados. O autosuggest do
        # Google, por exemplo, responde `text/javascript; charset=ISO-8859-1`.
        return json.loads(response.text)
    except (ValueError, UnicodeDecodeError):
        log.warning("Resposta de %s não é JSON válido (content-type: %s)",
                    url, response.headers.get("content-type", "?"))
        return None


def get_text(url: str, *, params: dict | None = None, headers: dict | None = None) -> str:
    response = get(url, params=params, headers=headers)
    return response.text if response.status_code == 200 else ""


def post_json(
    url: str, *, json_body: dict, headers: dict | None = None, timeout: int | None = None
):
    _throttle(url)
    with httpx.Client(timeout=timeout or settings.http_timeout) as client:
        response = client.post(url, json=json_body, headers=_headers(headers))
        return response
