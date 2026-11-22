"""E18.4c: Sites — visão operacional dos blogs (operador e admin).

Estado, posts por dia, idiomas, próximo slot, o dia de hoje, último post, feeds ativos, fila de
pautas e se o blog está indexável. Editar (ligar/desligar, slots, idiomas) fica na Configuração
(só admin, E18.3a). O indexável é só LIDO aqui — o `noindex` da home do blog, que o WordPress põe
quando `blog_public = 0`; mudá-lo pelo portal fica para depois (exige endpoint num mu-plugin).
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.routes.dashboard import when_local
from app.core.cache import redis_client
from app.core.config import settings
from app.core.db import get_db
from app.core.logging import get_logger
from app.services import fetcher

log = get_logger(__name__)
router = APIRouter(prefix="/dashboard/sites", tags=["dashboard"])
templates = Jinja2Templates(directory="app/dashboard/templates")
templates.env.filters["when"] = when_local

INDEXABLE_TTL_S = 600
TERMINAL = ("PUBLISHED_CONFIRMED", "REJECTED", "FAILED", "EXPIRED")


_META_ROBOTS = re.compile(r"<meta\b[^>]*\bname\s*=\s*[\"']robots[\"'][^>]*>", re.I)


def robots_noindex(html: str) -> bool:
    """A home traz `<meta name="robots" content="…noindex…">`? (o que o WP põe com blog_public=0)"""
    return any("noindex" in tag.lower() for tag in _META_ROBOTS.findall(html))


def indexable(site_key: str, base_url: str) -> bool | None:
    """True/False pelo `noindex` da home; None se não deu para ler. Cache de 10 min no Redis."""
    key = f"sb:indexable:{site_key}"
    try:
        cached = redis_client.get(key)
        if cached in ("1", "0"):
            return cached == "1"
    except Exception:  # Redis fora: lê direto
        cached = None
    try:
        page = fetcher.fetch(base_url + "/", respect_robots=False, timeout_s=8,
                             accept="text/html")
    except fetcher.FetchError as exc:
        log.info("Sites: home de %s ilegível (%s)", site_key, exc.kind)
        return None
    if page.status != 200:
        return None
    value = not robots_noindex(page.text[:200_000])
    try:
        redis_client.set(key, "1" if value else "0", ex=INDEXABLE_TTL_S)
    except Exception:
        log.warning("Sites: Redis indisponível para o cache do indexável")
    return value


def indexable_all(targets: list[tuple[str, str]]) -> dict[str, bool | None]:
    """`indexable` dos blogs AO MESMO TEMPO: com o cache frio, ler as homes uma a uma somava
    até 8 s por blog (revisão E18); em paralelo, o pior caso é o de uma só."""
    if not targets:
        return {}
    with ThreadPoolExecutor(max_workers=min(len(targets), 8)) as pool:
        values = list(pool.map(lambda target: indexable(*target), targets))
    return {site_key: value for (site_key, _), value in zip(targets, values, strict=True)}


@router.get("", response_class=HTMLResponse, summary="Sites")
def page(request: Request, db: Session = Depends(get_db)):
    now = datetime.now(UTC)
    rows = db.execute(text(
        """SELECT s.site_key, s.name, s.base_url, s.enabled, s.daily_target, s.languages,
                  s.slot_offset_minutes, s.timezone,
                  (SELECT min(j.due_at_utc) FROM publication_jobs j
                    WHERE j.site_key = s.site_key AND j.due_at_utc >= :now
                      AND NOT j.reserve AND NOT (j.status = ANY(:term))) AS next_due,
                  (SELECT count(*) FROM sources f WHERE f.site_key = s.site_key AND f.enabled)
                    AS feeds,
                  (SELECT count(*) FROM ideas i WHERE i.site_key = s.site_key AND i.status = 'new'
                     AND i.duplicate_of IS NULL AND i.expires_at > :now) AS queue,
                  (SELECT count(*) FROM publication_jobs j WHERE j.site_key = s.site_key
                     AND j.status = 'BLOCKED' AND j.local_date >= :week) AS blocked_7d
             FROM sites s ORDER BY s.enabled DESC, s.slot_offset_minutes, s.site_key"""),
        {"now": now, "term": list(TERMINAL), "week": (now - timedelta(days=7)).date()},
    ).mappings().all()
    readable = indexable_all([(r["site_key"], r["base_url"]) for r in rows if r["enabled"]])
    sites = []
    for row in rows:
        site = dict(row)
        site["last"] = db.execute(text(
            """SELECT j.id, j.published_at_utc, j.wordpress_url,
                      (SELECT title FROM job_translations t WHERE t.job_id = j.id
                        AND t.language = 'pt-BR') AS title
                 FROM publication_jobs j WHERE j.site_key = :s AND j.published_at_utc IS NOT NULL
                ORDER BY j.published_at_utc DESC LIMIT 1"""),
            {"s": site["site_key"]}).mappings().first()
        site["recent"] = [dict(r) for r in db.execute(text(
            """SELECT id, local_date, status FROM publication_jobs
                WHERE site_key = :s AND local_date BETWEEN :a AND :b AND NOT reserve
                ORDER BY local_date, slot_number"""),
            {"s": site["site_key"], "a": (now - timedelta(days=2)).date(),
             "b": (now + timedelta(days=1)).date()}).mappings()]
        site["indexable"] = readable.get(site["site_key"])
        sites.append(site)
    return templates.TemplateResponse(
        request=request, name="sites.html",
        context={"brand": settings.brand_name, "primary": settings.brand_primary_color,
                 "accent": settings.brand_accent_color, "user": request.state.panel_user,
                 "sites": sites, "admin": request.state.panel_role == "admin",
                 "status_class": {"PUBLISHED_CONFIRMED": "ok", "AWAITING_APPROVAL": "warn",
                                  "APPROVED": "ok", "BLOCKED": "bad", "FAILED": "bad",
                                  "EXPIRED": "bad", "REJECTED": "bad", "RETRY_WAIT": "warn"}},
    )
