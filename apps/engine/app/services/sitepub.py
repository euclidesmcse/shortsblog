"""Publicador por site (E9): leva um job APPROVED para o WordPress do seu site, nos 4 idiomas.

- O destino vem sempre da tabela `sites` e a credencial do cofre (E18.3b) ou do ambiente
  (`SB_WP_<SITE>_APP_USER` / `_APP_PASSWORD`); nada disto vem do pedido nem do conteúdo do
  job (E9.1).
- Idempotente (E9.4): cada post leva `sb_job_id`; antes de criar procura-se por ele + idioma.
  Um timeout no POST nunca repete às cegas: consulta-se; se o post não aparece, o job fica
  `VERIFY_REQUIRED` e a próxima execução reconcilia (a procura é o primeiro passo).
- Modo `draft` (por omissão, até ao piloto da E14): cria só rascunhos e não muda o estado do job.
  Modo `live`: APPROVED → PUBLISHING → verificação → PUBLISHED_CONFIRMED.
- Erros (E9.5): 401/403 suspende só o site; 429 espera `Retry-After`; um site em baixo
  não afeta os outros.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import pathlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.cache import acquire_lock, release_lock
from app.core.config import settings
from app.core.db import session_scope
from app.core.logging import get_logger
from app.services import cloudflare, jobs, sites, vault

log = get_logger(__name__)

ACTOR = "publisher"
LANGS = ("pt-BR", "en", "es", "zh-CN")
POLYLANG = {
    "pt-BR": "pt",
    "en": "en",
    "es": "es",
    "zh-CN": "zh",
}  # idioma do motor → slug do Polylang
_TIMEOUT_S = 30.0
_LOCK_TTL_S = 600  # mais que o pior caso (4 idiomas com mídia) e menos que um lease
_transport: httpx.BaseTransport | None = None  # os testes trocam por um WordPress de mentira


class PublishError(RuntimeError):
    """Falha que não deixa o job num estado incerto."""


class SiteSuspended(PublishError):
    """401/403: a credencial deixou de servir. Só este site pára."""


class RateLimited(PublishError):
    def __init__(self, wait_s: int) -> None:
        super().__init__(f"429: repetir daqui a {wait_s}s")
        self.wait_s = wait_s


class Unverified(PublishError):
    """Não se sabe se o WordPress criou o post (timeout): reconciliar, nunca repetir às cegas."""


# Slugs das páginas do blog (`wordpress/pages/`): um artigo com um destes ficaria à sombra dela.
_PAGE_SLUGS = {
    "sobre",
    "about",
    "acerca-de",
    "sobre-zh",
    "politica-editorial",
    "editorial-policy",
    "politica-editorial-y-fuentes",
    "politica-editorial-zh",
    "privacidade",
    "privacy",
    "privacidad",
    "privacidade-zh",
    "contato",
    "contact",
    "contacto",
    "contato-zh",
}


def safe_slug(slug: str) -> str:
    """O WordPress aceita um artigo com o slug de uma página; aqui evita-se (spike E8.4, D-050)."""
    return f"{slug}-artigo" if slug in _PAGE_SLUGS else slug


@dataclass
class Result:
    job_id: int
    mode: str
    posts: dict[str, int] = field(default_factory=dict)  # idioma do motor → id do post
    urls: dict[str, str] = field(default_factory=dict)
    slugs: dict[str, str] = field(default_factory=dict)  # o slug que o WordPress atribuiu
    media_urls: dict[str, str] = field(default_factory=dict)  # URL pública da capa (D-099)
    created: list[str] = field(default_factory=list)  # idiomas criados nesta execução
    outcome: str = "DRAFTS"  # DRAFTS | PUBLISHED_CONFIRMED | …


# --- Cliente REST ---------------------------------------------------------------------------


def _retry_after(value: str | None) -> int:
    """Espera de um `Retry-After`: só segundos (outro valor → 60), com teto de 1 h."""
    try:
        return max(1, min(int(str(value).strip()), 3600))
    except ValueError:
        return 60


class WordPress:
    """Um site: URL fixa da tabela `sites`, credencial do ambiente."""

    def __init__(self, site: dict[str, Any],
                 credentials: tuple[str, str] | None = None) -> None:
        self.site_key = str(site["site_key"])
        api = str(site.get("wordpress_api_url") or "").rstrip("/")
        self.host = (urlsplit(str(site.get("base_url") or "")).hostname or "").lower()
        if (
            not api
            or not self.host
            or urlsplit(api).scheme != "https"
            or urlsplit(api).hostname != self.host
        ):
            raise PublishError(
                f"{self.site_key}: wordpress_api_url ausente ou fora do hostname do site"
            )
        # E18.3b: o cofre (banco) tem precedência; sem item no banco vale o
        # SB_WP_<SITE>_APP_USER/_APP_PASSWORD do ambiente. `credentials` serve ao «testar conexão».
        user, password = credentials or vault.wp_credentials(self.site_key)
        if not user or not password:
            raise PublishError(f"{self.site_key}: sem credencial do WordPress (cofre ou ambiente)")
        self.api = api
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self._auth = {"Authorization": f"Basic {token}"}

    def call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        try:
            with httpx.Client(transport=_transport, timeout=_TIMEOUT_S) as client:
                response = client.request(
                    method,
                    f"{self.api}{path}",
                    params=params,
                    json=json,
                    content=content,
                    headers={**self._auth, **(headers or {})},
                )
        except httpx.TimeoutException as exc:
            raise Unverified(f"{method} {path}: timeout") from exc
        except httpx.HTTPError as exc:
            raise PublishError(f"{method} {path}: {type(exc).__name__}") from exc
        if response.status_code in (401, 403):
            raise SiteSuspended(f"{self.site_key}: {response.status_code} em {path}")
        if response.status_code == 429:
            raise RateLimited(_retry_after(response.headers.get("Retry-After")))
        if response.status_code >= 400:
            raise PublishError(f"{method} {path}: HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            # 200 que não é JSON (página de erro do Cloudflare, por exemplo): não se sabe o que
            # o WordPress fez; para um POST, reconcilia-se em vez de assumir.
            error = Unverified if method != "GET" else PublishError
            raise error(f"{method} {path}: resposta que não é JSON") from None

    def find_post(self, job_id: int, lang: str) -> dict[str, Any] | None:
        found = self.call(
            "GET",
            "/wp/v2/posts",
            params={
                "sb_job_id": job_id,
                "status": "any",
                "lang": POLYLANG[lang],
                "per_page": 20,
                "context": "edit",
            },
        )
        # O Polylang gratuito ignora `lang` na procura (visto no WordPress real): confere-se
        # o idioma de cada resposta, senão todos os idiomas "encontravam" o post em português.
        return next((p for p in found if p.get("lang") == POLYLANG[lang]), None)

    def find_media(self, name: str) -> int | None:
        """Mídia já enviada com este nome (R46a): o WordPress guarda o nome como `slug`."""
        stem = pathlib.PurePath(name).stem
        found = self.call(
            "GET",
            "/wp/v2/media",
            # Sem `status=any`: o WordPress devolve 400 nas mídias (visto em 2026-10-07).
            params={"slug": stem, "per_page": 5, "context": "edit"},
        )
        match = next((m for m in found if m.get("slug") == stem and m.get("id")), None)
        return int(match["id"]) if match else None

    def in_site(self, url: str) -> bool:
        return (urlsplit(url).hostname or "").lower() == self.host


# --- Passos ---------------------------------------------------------------------------------


def _load(
    db: Session, job_id: int
) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]], dict[str, Any] | None]:
    job = (
        db.execute(text("SELECT * FROM publication_jobs WHERE id = :i"), {"i": job_id})
        .mappings()
        .first()
    )
    if job is None:
        raise jobs.JobNotFound(f"job {job_id} não existe")
    site = sites.get(db, str(job["site_key"]))
    if site is None:
        raise PublishError(f"job {job_id}: site {job['site_key']!r} desconhecido")
    if _suspended(db, str(site["site_key"])):
        raise PublishError(f"site {site['site_key']} suspenso nas últimas 24 h (401/403)")
    texts = {
        r["language"]: dict(r)
        for r in db.execute(
            text("SELECT * FROM job_translations WHERE job_id = :i"), {"i": job_id}
        ).mappings()
    }
    missing = [
        lang for lang in LANGS if texts.get(lang, {}).get("status") not in ("ready", "published")
    ]
    if missing:
        raise PublishError(f"job {job_id}: texto não pronto em {', '.join(missing)}")
    image = (
        db.execute(
            text("SELECT * FROM image_assets WHERE job_id = :i ORDER BY id LIMIT 1"), {"i": job_id}
        )
        .mappings()
        .first()
    )
    return dict(job), dict(site), texts, dict(image) if image else None


def _category_term(wp: WordPress, name: str, default: str) -> dict[str, int]:
    """Termo da categoria por slug de idioma (Polylang: o do pt-BR traz as traduções ligadas)."""
    for wanted in (name, default):
        if not wanted:
            continue
        found = wp.call(
            "GET",
            "/wp/v2/categories",
            params={
                "search": wanted,
                "lang": "pt",
                "per_page": 20,
            },  # `context=edit` é vedado ao author
        )
        for term in found:
            if str(term.get("name", "")).casefold() == wanted.casefold():
                links = dict(term.get("translations") or {})
                links.setdefault("pt", term["id"])
                return {slug: int(links[slug]) for slug in POLYLANG.values() if slug in links}
    return {}


def _upload_media(wp: WordPress, job_id: int, image: dict[str, Any], lang: str) -> tuple[int, str]:
    """Uma imagem por idioma (o Polylang liga a mídia ao idioma); só com licença provada (D-012)."""
    if not image.get("license") or not (
        image.get("license_proof_url") or image.get("ai_generated")
    ):
        raise PublishError(f"job {job_id}: imagem sem prova de licença; não se publica")
    path = pathlib.Path(str(image["local_path"] or ""))
    if not path.is_file():
        raise PublishError(f"job {job_id}: ficheiro da imagem em falta")
    name = f"sb-{job_id}-{POLYLANG[lang]}{path.suffix or '.webp'}"
    kind = mimetypes.guess_type(name)[0] or "image/webp"
    existing = wp.find_media(name)  # uma repetição depois de uma falha não reenvia a imagem
    media = (
        {"id": existing}
        if existing
        else wp.call(
            "POST",
            "/wp/v2/media",
            content=path.read_bytes(),
            headers={
                "Content-Disposition": f'attachment; filename="{name}"',
                "Content-Type": kind,
            },
        )
    )
    alt = (image.get("alt_texts") or {}).get(lang) or image.get("alt_text") or ""
    caption = (image.get("captions") or {}).get(lang) or ""
    if not isinstance(media, dict) or "id" not in media:
        raise Unverified(f"job {job_id}: o upload da imagem ({lang}) não devolveu um id")
    updated = wp.call(
        "POST",
        f"/wp/v2/media/{media['id']}",
        json={"alt_text": alt, "caption": caption, "title": name, "lang": POLYLANG[lang]},
    )
    # URL pública da mídia (D-099, capa na newsletter): o POST de metadados devolve o objeto
    # completo, também no caminho de reaproveitamento (`existing`), em que o upload não correu.
    url = ""
    for source in (updated, media):
        if isinstance(source, dict) and source.get("source_url"):
            url = str(source["source_url"])
            break
    return int(media["id"]), url


def _create(
    wp: WordPress,
    job: dict[str, Any],
    lang: str,
    row: dict[str, Any],
    *,
    status: str,
    category: int | None,
    media: int | None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "title": row["title"],
        "slug": safe_slug(str(row["slug"])),
        "excerpt": row["excerpt"],
        "content": row["html"],
        "status": status,
        "lang": POLYLANG[lang],
        "sb_job_id": job["id"],
    }
    if category:
        body["categories"] = [category]
    if media:
        body["featured_media"] = media
    try:
        return wp.call("POST", "/wp/v2/posts", json=body)
    except Unverified:
        # Pode ter sido criado: consulta antes de dar o job por perdido (nunca POST às cegas).
        existing = wp.find_post(int(job["id"]), lang)
        if existing is None:
            raise
        return existing


def _ensure_posts(
    wp: WordPress,
    job: dict[str, Any],
    site: dict[str, Any],
    texts: dict[str, dict[str, Any]],
    image: dict[str, Any] | None,
    status: str,
) -> Result:
    result = Result(job_id=int(job["id"]), mode="live" if status == "publish" else "draft")
    config = site.get("config") or {}
    default = (config.get("categories") or [""])[0]
    terms = _category_term(
        wp, str((job.get("review_report") or {}).get("category", "")), str(default)
    )
    for lang in LANGS:
        post = wp.find_post(int(job["id"]), lang)
        if post is None:
            media = None
            if image:
                media, media_url = _upload_media(wp, int(job["id"]), image, lang)
                if media_url:
                    result.media_urls[lang] = media_url
            post = _create(
                wp,
                job,
                lang,
                texts[lang],
                status=status,
                category=terms.get(POLYLANG[lang]),
                media=media,
            )
            result.created.append(lang)
        elif status == "publish" and post.get("status") != "publish":
            post = wp.call("POST", f"/wp/v2/posts/{post['id']}", json={"status": "publish"})
        result.posts[lang] = int(post["id"])
        result.urls[lang] = str(post.get("link") or "")
        result.slugs[lang] = str(post.get("slug") or "")
    # Liga as traduções (idempotente) a partir do post em português.
    wp.call(
        "POST",
        f"/wp/v2/posts/{result.posts['pt-BR']}",
        json={"translations": {POLYLANG[lang]: pid for lang, pid in result.posts.items()}},
    )
    return result


def _verify(wp: WordPress, result: Result) -> None:
    """Relê cada post: publicado, no idioma certo e com o link no hostname do site (E9.2)."""
    for lang, post_id in result.posts.items():
        post = wp.call("GET", f"/wp/v2/posts/{post_id}", params={"context": "edit"})
        if post.get("status") != "publish" or post.get("lang") != POLYLANG[lang]:
            raise Unverified(f"post {post_id} ({lang}): estado ou idioma inesperado após publicar")
        if not wp.in_site(str(post.get("link") or "")):
            raise PublishError(f"post {post_id} ({lang}): link fora do hostname do site")
        result.urls[lang] = str(post["link"])


def _record(db: Session, job_id: int, result: Result) -> None:
    for lang, post_id in result.posts.items():
        db.execute(
            text(
                "UPDATE job_translations SET wordpress_post_id = :p, wordpress_url = :u, "
                "slug = COALESCE(NULLIF(:sl, ''), slug), "
                "status = :s, updated_at = now() WHERE job_id = :j AND language = :l"
            ),
            {
                "p": post_id,
                "sl": result.slugs.get(lang, ""),
                "u": result.urls.get(lang),
                "j": job_id,
                "l": lang,
                "s": "published" if result.mode == "live" else "ready",
            },
        )
    if result.media_urls:
        # Junta ao que já houver: uma reexecução que só criou idiomas em falta não apaga os outros.
        db.execute(
            text(
                "UPDATE image_assets SET wordpress_media_urls = "
                "COALESCE(wordpress_media_urls, '{}'::jsonb) || CAST(:u AS JSONB) "
                "WHERE job_id = :j"
            ),
            {"u": json.dumps(result.media_urls), "j": job_id},
        )


def _suspended(db: Session, site_key: str) -> bool:
    return (
        db.execute(
            text("""SELECT 1 FROM publication_events e JOIN publication_jobs j ON j.id = e.job_id
                 WHERE j.site_key = :k AND e.event_type = 'site_suspended'
                   AND e.timestamp_utc > now() - interval '24 hours' LIMIT 1"""),
            {"k": site_key},
        ).first()
        is not None
    )


def publish_job(job_id: int, *, mode: str | None = None, owner: str = "publisher") -> Result:
    """Publica (ou cria rascunhos de) um job aprovado. `mode`: `draft` (padrão) ou `live`.

    Um bloqueio por job (R46b) impede dois workers de criarem os mesmos posts ao mesmo tempo."""
    name = f"publish:{job_id}"
    if not acquire_lock(name, ttl=_LOCK_TTL_S):
        raise PublishError(f"job {job_id}: já há uma publicação em curso")
    try:
        return _publish_locked(job_id, mode=mode, owner=owner)
    finally:
        release_lock(name)


def _publish_locked(job_id: int, *, mode: str | None, owner: str) -> Result:
    mode = mode or settings.wp_publish_mode
    if mode not in ("draft", "live"):
        raise ValueError(f"modo desconhecido: {mode!r}")
    with session_scope() as db:
        # Nada é publicado sem a decisão humana registada (E6.9/E10): RETRY_WAIT também é a
        # espera da produção, e um job nunca aprovado não pode virar APPROVED aqui (21/11).
        if not db.execute(text("SELECT 1 FROM approvals WHERE job_id = :j "
                               "AND decision = 'approved' LIMIT 1"), {"j": job_id}).first():
            raise PublishError(f"job {job_id}: sem aprovação registada; não se publica")
        job, site, texts, image = _load(db, job_id)
        status = str(job["status"])
        allowed = ("APPROVED", "VERIFY_REQUIRED", "RETRY_WAIT") if mode == "live" else ("APPROVED",)
        if status not in allowed:
            raise PublishError(f"job {job_id}: estado {status} não se publica em modo {mode}")
        if mode == "live":
            if status == "RETRY_WAIT":
                jobs.transition(db, job_id, "APPROVED", actor=ACTOR, expected="RETRY_WAIT")
            jobs.transition(
                db,
                job_id,
                "PUBLISHING",
                actor=ACTOR,
                expected=("APPROVED", "VERIFY_REQUIRED"),
                lease_owner=owner,
                lease_until_utc=datetime.now(UTC) + timedelta(minutes=settings.job_lease_minutes),
            )
    try:
        wp = WordPress(site)
        result = _ensure_posts(
            wp, job, site, texts, image, "publish" if mode == "live" else "draft"
        )
        if mode == "live":
            _verify(wp, result)
    except Unverified as exc:
        return _fail(job_id, mode, "VERIFY_REQUIRED", str(exc), owner=owner)
    except RateLimited as exc:
        return _fail(
            job_id,
            mode,
            "RETRY_WAIT",
            str(exc),
            owner=owner,
            retry_after=datetime.now(UTC) + timedelta(seconds=exc.wait_s),
        )
    except SiteSuspended as exc:
        # O papel da base não escreve em `sites`: a suspensão fica num evento, que `_suspended`
        # consulta; só este site pára (E9.5).
        with session_scope() as db:
            jobs.record_event(
                db,
                job_id,
                "site_suspended",
                actor=ACTOR,
                details={"site": site["site_key"], "reason": str(exc)[:300]},
            )
        log.error("Site %s suspenso: %s", site["site_key"], exc)
        return _fail(
            job_id,
            mode,
            "RETRY_WAIT",
            f"site suspenso: {exc}",
            owner=owner,
            retry_after=datetime.now(UTC) + timedelta(hours=24),
        )
    except PublishError as exc:
        return _fail(
            job_id,
            mode,
            "RETRY_WAIT",
            str(exc),
            owner=owner,
            retry_after=datetime.now(UTC) + timedelta(minutes=settings.job_retry_minutes),
        )
    with session_scope() as db:
        _record(db, job_id, result)
        if mode == "live":
            jobs.transition(
                db,
                job_id,
                "PUBLISHED_CONFIRMED",
                actor=ACTOR,
                owner=owner,
                wordpress_post_id=result.posts["pt-BR"],
                wordpress_url=result.urls["pt-BR"],
                published_at_utc=datetime.now(UTC),
                details={"posts": result.posts},
            )
            result.outcome = "PUBLISHED_CONFIRMED"
        else:
            jobs.record_event(
                db,
                job_id,
                "drafts_created",
                actor=ACTOR,
                details={"posts": result.posts, "created": result.created},
            )
    if mode == "live" and cloudflare.enabled():
        # Tira do cache da borda o que o leitor vê: os idiomas do post e as páginas iniciais
        # (D-097; a categoria fica para o cache de 1 h expirar). Falha nunca bloqueia.
        base = str(site.get("base_url") or "").rstrip("/")
        homes = [f"{base}/"] + [f"{base}/{POLYLANG[lang]}/" for lang in LANGS if lang != "pt-BR"]
        cloudflare.purge_urls([*result.urls.values(), *homes])
    return result


def _fail(
    job_id: int,
    mode: str,
    outcome: str,
    reason: str,
    *,
    owner: str,
    retry_after: datetime | None = None,
) -> Result:
    log.warning("Job %d: %s (%s)", job_id, outcome, reason)
    with session_scope() as db:
        if mode == "live":
            fields: dict[str, Any] = {"blocked_reason": reason[:500]}
            if retry_after and outcome == "RETRY_WAIT":
                fields["retry_after_utc"] = retry_after
            jobs.transition(
                db,
                job_id,
                outcome,
                actor=ACTOR,
                owner=owner,
                details={"reason": reason[:500]},
                **fields,
            )
        else:
            jobs.record_event(
                db, job_id, "draft_failed", actor=ACTOR, details={"reason": reason[:500]}
            )
    return Result(job_id=job_id, mode=mode, outcome=outcome)
