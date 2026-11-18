"""Newsletters de fabricantes como pauta (D-094, opção A da D-OPEN-15).

O Brevo recebe o e-mail (MX de um subdomínio próprio), converte-o em JSON e chama a
rota `/wp-json/sb/v1/pauta/brevo` de um blog; o mu-plugin `sb-pauta.php` repassa ao
motor (`/api/pauta/brevo-inbound`) pela rede interna. Aqui confere-se o remetente
(lista de `content-config/newsletters.yaml`, sincronizada na tabela `settings` no
arranque, como os sites) e SPF/DKIM; extrai-se título + ligação de cada item do
HTML; desembrulha-se o rastreio (a URL final é a que vale, senão a deduplicação por
`canonical_url` quebra); e cada item vira uma ideia `origin='newsletter'`.

A newsletter nunca é fonte (D-070): só título e ligação entram; os fatos vêm das
páginas de destino, da corroboração e do reforço, com `min_sources` intacto.
E-mail é entrada não confiável (R7): tudo que vem dele é dado, nunca instrução —
ao LLM só chega o título, na triagem que já existe.
"""
from __future__ import annotations

import json
import pathlib
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

import yaml
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.cache import QUEUES
from app.core.config import settings
from app.core.db import get_setting, session_scope
from app.core.logging import get_logger
from app.services import discovery, fetcher
from app.services.urls import under

log = get_logger(__name__)

SETTING_KEY = "newsletters"
# Ligações que não são notícia: descadastro, preferências, redes sociais, loja.
# `manage`/`preferenc` com borda de palavra: «cost management» e «alert-management»
# são pauta legítima; «manage your preferences» não é (revisão E14).
_SKIP_PATH = re.compile(
    r"unsubscribe|\bpreferenc|\bmanage\b|opt[-_]?out|descadastr|privacy|privacidade|"
    r"profile|account|login|signin|mailto:", re.IGNORECASE)
_SKIP_TEXT = re.compile(
    r"unsubscribe|descadastr|\bprefer[eê]nc|\bmanage\b|view (in|this).*browser|"
    r"privacy|privacidade|facebook|twitter|linkedin|instagram|youtube", re.IGNORECASE)
_MIN_TITLE = 8          # o mesmo mínimo da ideia manual
_UNWRAP_MAX_BYTES = 65536   # só queremos a URL final, não a página


class NewsletterConfigError(ValueError):
    pass


def load(path: str | pathlib.Path | None = None) -> list[dict]:
    """Lê e valida `newsletters.yaml`. Arquivo ausente = lista vazia (recurso desligado)."""
    file = pathlib.Path(path or settings.newsletters_config_file)
    if not file.is_file():
        return []
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise NewsletterConfigError(f"{file.name}: {exc}") from exc
    entries = data.get("newsletters") or []
    if not isinstance(entries, list):
        raise NewsletterConfigError(f"{file.name}: 'newsletters' tem de ser uma lista")
    return validate_entries(entries, file.name)


def validate_entries(entries: list, where: str = "lista") -> list[dict]:
    """Normaliza e valida as entradas (também usado pela tela do portal, E18.3a)."""
    out: list[dict] = []
    for raw in entries:
        if not isinstance(raw, dict):
            raise NewsletterConfigError(f"{where}: cada entrada tem de ser um mapa")
        entry = {"name": str(raw.get("name", "")).strip(),
                 "sender_domain": str(raw.get("sender_domain", "")).strip().lower().lstrip("@"),
                 "site_key": str(raw.get("site_key", "")).strip(),
                 "lang": str(raw.get("lang", "")).strip()}
        if not entry["sender_domain"] or "." not in entry["sender_domain"] \
                or " " in entry["sender_domain"]:
            raise NewsletterConfigError(f"{where}: sender_domain inválido em {raw!r}")
        if not entry["site_key"]:
            raise NewsletterConfigError(f"{where}: site_key em falta em {raw!r}")
        out.append(entry)
    return out


def sync(db: Session, path: str | pathlib.Path | None = None) -> int:
    """Copia o YAML para `settings.newsletters` (os workers leem da base, como os sites)."""
    entries = load(path)
    db.execute(
        text("""INSERT INTO settings (key, value, description) VALUES (:k, CAST(:v AS JSONB), :d)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"""),
        {"k": SETTING_KEY, "v": json.dumps(entries, ensure_ascii=False),
         "d": "Remetentes de pauta por e-mail (D-094), de content-config/newsletters.yaml"},
    )
    return len(entries)


def _from_domain(sender: str) -> str:
    """O domínio do `From` («Nome <a@b.com>» → `b.com`)."""
    return sender.rsplit("@", 1)[-1].strip().strip(">").lower() if sender else ""


def authorized(db: Session, sender: str) -> dict | None:
    """A entrada do remetente, se o domínio do `From` for um autorizado (ou subdomínio)."""
    domain = _from_domain(sender)
    if not domain:
        return None
    for entry in get_setting(db, SETTING_KEY, []) or []:
        allowed = entry.get("sender_domain", "")
        if allowed and under(domain, (allowed,)):
            return entry
    return None


# Um resultado (`spf=pass`/`dkim=pass`) e o domínio autenticado do mesmo trecho (RFC 8601).
_AUTH_METHOD = re.compile(r"\b(spf|dkim)\s*=\s*([a-z0-9]+)", re.IGNORECASE)
_DKIM_DOMAIN = re.compile(r"\bheader\.[di]\s*=\s*@?([a-z0-9.-]+)", re.IGNORECASE)
_SPF_DOMAIN = re.compile(r"\bsmtp\.mailfrom\s*=\s*(?:[^@\s;]*@)?([a-z0-9.-]+)", re.IGNORECASE)


def _aligned(auth_domain: str, from_domain: str) -> bool:
    """Alinhamento relaxado: um é o outro ou subdomínio dele (`bounce.x.com` ↔ `x.com`)."""
    return bool(auth_domain) and bool(from_domain) and (
        under(auth_domain, (from_domain,)) or under(from_domain, (auth_domain,)))


def auth_passed(auth: list[str], from_domain: str) -> bool:
    """SPF ou DKIM com `pass` E domínio autenticado alinhado ao domínio do `From`.

    Só vale o `Authentication-Results` cujo authserv-id é o do nosso recebedor
    (`pauta_authserv_id`): qualquer remetente pode escrever esse cabeçalho no e-mail
    (RFC 8601), e um `pass` do domínio do atacante não autentica o `From` que ele
    forjou — daí o alinhamento. Sem o id configurado, nada passa (R62)."""
    trusted = settings.pauta_authserv_id.strip().lower()
    if not trusted:
        log.warning("Pauta por e-mail: pauta_authserv_id vazio — recusando tudo (R62); "
                    "conferir o authserv-id no 1.º e-mail real e configurar")
        return False
    for value in auth:
        head, _, results = value.partition(";")
        authserv = head.strip().split()[0].lower() if head.strip() else ""
        if authserv != trusted:
            continue
        for part in results.split(";"):
            found = _AUTH_METHOD.search(part)
            if not found or found.group(2).lower() != "pass":
                continue
            kind = found.group(1).lower()
            domain = (_DKIM_DOMAIN if kind == "dkim" else _SPF_DOMAIN).search(part)
            if domain and _aligned(domain.group(1).lower().rstrip("."), from_domain):
                return True
    return False


class _Anchors(HTMLParser):
    """Só `<a href>` https com texto: o par (título, ligação) de cada item."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.items: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag != "a":
            return
        self._flush()
        href = next((v for k, v in attrs if k == "href" and v), "")
        self._href = href if href.startswith("https://") else None
        self._text = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def _flush(self) -> None:
        if self._href is not None:
            title = re.sub(r"\s+", " ", " ".join(self._text)).strip()
            if title:
                self.items.append((title, self._href))
        self._href, self._text = None, []


def extract_items(html: str) -> list[tuple[str, str]]:
    """Itens (título, ligação) do HTML do e-mail, sem descadastro/preferências/social."""
    parser = _Anchors()
    try:
        parser.feed(html or "")
        parser.close()
    except Exception:                                   # noqa: BLE001 — HTML de e-mail é selvagem
        log.warning("HTML da newsletter ilegível; itens lidos até o erro: %d", len(parser.items))
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for title, href in parser.items:
        parts = urlsplit(href)
        target = f"{parts.netloc}{parts.path}?{parts.query}"
        if (len(title) < _MIN_TITLE or title.startswith(("http://", "https://"))
                or _SKIP_TEXT.search(title) or _SKIP_PATH.search(target)):
            continue
        if href in seen:
            continue
        seen.add(href)
        out.append((title[:500], href))
        if len(out) >= settings.pauta_max_items:
            break
    return out


def unwrap(url: str) -> str | None:
    """Resolve o rastreio até a URL final (redirects seguros do fetcher; sem robots:
    só queremos o destino, a página não é lida nem guardada). Falha = item fora."""
    try:
        page = fetcher.fetch(url, max_bytes=_UNWRAP_MAX_BYTES, respect_robots=False)
    except fetcher.FetchError as exc:
        log.info("Item de newsletter descartado (%s): %s", exc, url[:120])
        return None
    return page.url


def enqueue_ingest(*, sender: str, subject: str, html: str, auth: list[str]) -> None:
    """Põe o e-mail na fila `fast` e o webhook responde já: quem desembrulha o rastreio
    (até `pauta_max_items` pedidos HTTP em série) é o worker, fora do timeout do relay —
    uma cadeia de redirects lenta estourava os 15 s do WordPress e o Brevo repetia tudo
    (revisão E14). Se o Redis falhar, o erro sobe, o relay devolve 503 e o Brevo retenta."""
    QUEUES["fast"].enqueue(ingest_email, sender, subject, html, auth, job_timeout=300)


def ingest_email(sender: str, subject: str, html: str, auth: list[str]) -> dict:
    """Processa um e-mail enfileirado pelo webhook (corre no worker `fast`)."""
    with session_scope() as db:
        return ingest(db, sender=sender, subject=subject, html=html, auth=auth)


def ingest(db: Session, *, sender: str, subject: str, html: str,
           auth: list[str] | None = None) -> dict:
    """Processa um e-mail do webhook. Devolve contadores (nada do e-mail vai no erro)."""
    entry = authorized(db, sender)
    if entry is None:
        log.warning("Pauta por e-mail recusada: remetente não autorizado (%s)", sender[:120])
        return {"accepted": False, "reason": "sender"}
    if not auth_passed(auth or [], _from_domain(sender)):
        log.warning("Pauta por e-mail recusada: sem SPF/DKIM `pass` alinhado ao From (%s)",
                    sender[:120])
        return {"accepted": False, "reason": "auth"}
    stats = {"accepted": True, "newsletter": entry.get("name", ""), "items": 0,
             "new": 0, "duplicate": 0, "known": 0, "discarded": 0}
    for title, href in extract_items(html):
        stats["items"] += 1
        final = unwrap(href)
        if final is None or discovery.canonical_url(final) is None:
            stats["discarded"] += 1
            continue
        result, _ = discovery.store_idea(
            db, site_key=entry["site_key"], origin="newsletter", topic=title, url=final,
            created_by=f"newsletter:{entry.get('sender_domain', '')}")
        stats[result] += 1
    log.info("Pauta por e-mail de %s («%s»): %s", entry.get("name", "?"), subject[:80],
             {k: v for k, v in stats.items() if k not in ("accepted", "newsletter")})
    return stats
