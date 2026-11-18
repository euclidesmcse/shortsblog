"""Sites que o motor não lê para a IA (D-070, E14.5j).

Ler a página de uma fonte e entregá-la ao modelo (pesquisa, redação, revisão) é o
que muitos termos de uso proíbem: robôs, raspagem, uso por IA ou «grounding (RAG)».
Uma fonte desses sites fica **só como pauta**: o feed continua a dizer que há
notícia (título e ligação), mas a página não é lida, e os factos e as citações vêm
de outras fontes. Duas regras:

- a lista do sem-leitura (no banco desde a E18.3a; semente: `content-config/sem-leitura.yaml`),
  tirada da leitura dos termos
  (`docs/reviews/E14-termos-feeds-*.md`), por domínio (vale para os subdomínios);
- o `robots.txt` de cada site, lido também como os robôs de IA a quem o texto é
  entregue (`research_ai_agents`): quem os bloqueia não quer a página numa IA. Esta
  parte fica no `fetcher` (`ai_agents`).
"""
from __future__ import annotations

import pathlib
import time
from functools import lru_cache
from urllib.parse import urlsplit

import yaml

from app.core.config import settings
from app.services.urls import under


class TermsConfigError(ValueError):
    pass


# E18.3a entrega 3 (D-104): a lista vive no banco (`settings.editorial.no_fetch`), semeada do
# YAML no deploy e editada no portal; o arquivo só vale enquanto o banco ainda não a tem (base
# nova, testes). Cache curto: uma edição no portal chega aos workers em até `_TTL_S` segundos.
SETTING_KEY = "editorial.no_fetch"
_TTL_S = 60.0
_cache: tuple[float, tuple[dict, ...]] | None = None


def validate_entries(raw: list, where: str = "lista", *,
                     allow_duplicates: bool = False) -> tuple[dict, ...]:
    """Normaliza e valida as entradas ({domain, reason, terms?, rss?}); levanta TermsConfigError.

    No arquivo, um domínio repetido é tolerado (vale a 1.ª entrada; `rss: false` de qualquer uma
    prevalece) — o YAML de 10/11 tem `euronews.com` duas vezes. No portal, repetido é recusado."""
    out: list[dict] = []
    seen: dict[str, dict] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            raise TermsConfigError(f"entrada inválida em {where}: {entry!r}")
        domain = str(entry.get("domain", "")).strip().lower().removeprefix("www.")
        if not domain or "/" in domain or "." not in domain or " " in domain:
            raise TermsConfigError(f"domínio inválido em {where}: {entry!r}")
        reason = str(entry.get("reason", "")).strip()
        if not reason:
            raise TermsConfigError(f"{domain}: falta o motivo (reason) em {where}")
        if domain in seen:
            if not allow_duplicates:
                raise TermsConfigError(f"{domain}: domínio repetido em {where}")
            if entry.get("rss") is False:
                seen[domain]["rss"] = False
            continue
        item: dict = {"domain": domain, "reason": reason}
        if entry.get("terms"):
            item["terms"] = str(entry["terms"]).strip()
        if entry.get("rss") is False:
            item["rss"] = False
        seen[domain] = item
        out.append(item)
    return tuple(out)


@lru_cache(maxsize=1)
def _load(path: str) -> tuple[dict, ...]:
    file = pathlib.Path(path)
    if not file.is_file():
        raise TermsConfigError(f"lista de sites sem leitura não encontrada: {path}")
    data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    return validate_entries(data.get("sites", []), file.name, allow_duplicates=True)


def load_file(path: str | None = None) -> tuple[dict, ...]:
    return _load(path or settings.no_fetch_config)


def _from_db() -> tuple[dict, ...] | None:
    from sqlalchemy import text

    from app.core.db import session_scope

    with session_scope() as db:
        value = db.execute(text("SELECT value FROM settings WHERE key = :k"),
                           {"k": SETTING_KEY}).scalar()
    return validate_entries(value, "banco") if isinstance(value, list) else None


def entries() -> tuple[dict, ...]:
    global _cache
    now = time.monotonic()
    if _cache is not None and now - _cache[0] < _TTL_S:
        return _cache[1]
    stored = _from_db()
    value = stored if stored is not None else load_file()
    if stored is not None:
        _cache = (now, value)   # só o banco entra no cache; o arquivo já tem o lru_cache
    return value


def invalidate() -> None:
    """Esquece o cache (depois de uma edição no portal, no mesmo processo)."""
    global _cache
    _cache = None


def no_fetch_domains() -> frozenset[str]:
    return frozenset(e["domain"] for e in entries())


def no_feed_domains() -> frozenset[str]:
    """Sites cujos termos proíbem o uso comercial do RSS (`rss: false`, D-071): nem como pauta."""
    return frozenset(e["domain"] for e in entries() if e.get("rss") is False)


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().rstrip(".").removeprefix("www.")


def readable(url: str) -> bool:
    """A página pode ser lida e entregue ao modelo? (False: o site está na lista.)"""
    return not under(host_of(url), no_fetch_domains())
