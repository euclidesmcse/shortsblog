"""Utilidades de URL partilhadas: domínio de uma ligação e teste de sufixo de domínio.

Antes cada módulo (websearch, terms, pauta, article) trazia a sua cópia; aqui fica a única.
"""
from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit


def domain_of(url: str) -> str:
    """O anfitrião da ligação, minúsculo e sem `www.`."""
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def under(domain: str, parents: Iterable[str]) -> bool:
    """O domínio é um dos `parents`, ou subdomínio de um deles?"""
    return any(domain == d or domain.endswith("." + d) for d in parents)
