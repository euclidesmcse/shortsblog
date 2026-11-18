"""Utilitários de texto: normalização, slug, contagem, markdown → HTML."""
from __future__ import annotations

import hashlib
import re
import unicodedata

import markdown as md
from slugify import slugify as _slugify

_WS = re.compile(r"\s+")
_HTML_TAG = re.compile(r"<[^>]+>")


def normalize(term: str) -> str:
    """Forma canônica para deduplicar keywords: minúsculas, sem acento, sem pontuação."""
    text = unicodedata.normalize("NFKD", term.lower().strip())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return _WS.sub(" ", text).strip()


def slugify(text: str, max_length: int = 80) -> str:
    return _slugify(text, max_length=max_length, word_boundary=True)


def word_count(text: str) -> int:
    return len(_WS.split(_HTML_TAG.sub(" ", text).strip())) if text.strip() else 0


def reading_minutes(words: int, wpm: int = 220) -> int:
    return max(1, round(words / wpm))


def to_html(markdown_text: str) -> str:
    return md.markdown(
        markdown_text,
        extensions=["extra", "tables", "toc", "sane_lists", "attr_list"],
        output_format="html5",
    )


def strip_html(html: str) -> str:
    return _WS.sub(" ", _HTML_TAG.sub(" ", html)).strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize(text).encode()).hexdigest()


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[: limit - len(suffix)]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + suffix


def extract_headings(markdown_text: str) -> list[dict]:
    """Extrai a estrutura H1..H4 de um markdown — usado no sumário e na auditoria."""
    headings = []
    for line in markdown_text.splitlines():
        match = re.match(r"^(#{1,4})\s+(.+?)\s*$", line)
        if match:
            headings.append({"level": len(match.group(1)), "text": match.group(2)})
    return headings
