"""Painel "Pauta" (E10.2, spec WF-01; E18.2): o tema, o ângulo e, se quiser, as fontes.

Uma ideia manual tem prioridade sobre as do RSS, mas passa pelos mesmos gates editoriais (as fontes
são lidas, corroboradas e revistas como qualquer outra). A primeira ligação é a ideia; as seguintes
entram como repetições dela, que o motor lê como fontes extra. Protegido como o resto do painel
(`core/panel_auth.py`).
"""

from __future__ import annotations

import re
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import get_db, session_scope
from app.services import discovery, users, websearch

router = APIRouter(prefix="/dashboard/ideas", tags=["dashboard"])
templates = Jinja2Templates(directory="app/dashboard/templates")

PRIORITIES = {
    "normal": discovery.MANUAL_PRIORITY,
    "alta": discovery.MANUAL_PRIORITY + 50,
    "maxima": discovery.MANUAL_PRIORITY + 100,
}
PRIORITY_LABELS = {"normal": "Normal", "alta": "Alta", "maxima": "Máxima (antes de todas)"}
TTL_CHOICES = (1, 2, 4, 7, 14)
MAX_SOURCES = 5
STATUS_LABELS = {
    "new": "na fila",
    "taken": "em produção",
    "used": "usada",
    "discarded": "descartada",
    "expired": "expirou",
}


def _user(request: Request) -> str:
    return str(getattr(request.state, "panel_user", None) or "painel")[:120]


def _back(*, msg: str = "", err: str = "") -> RedirectResponse:
    query = urlencode({k: v for k, v in (("msg", msg), ("err", err)) if v})
    return RedirectResponse("/dashboard/ideas" + (f"?{query}" if query else ""), status_code=303)


def parse_sources(raw: str) -> list[str]:
    """Uma ligação por linha (ou separadas por espaços); sem repetidas; no máximo `MAX_SOURCES`."""
    urls: list[str] = []
    for item in re.split(r"[\s,;]+", raw.strip()):
        if item and item not in urls:
            urls.append(item)
    return urls


@router.get("", response_class=HTMLResponse, summary="Nova ideia")
def page(request: Request, db: Session = Depends(get_db)):
    rows = (
        db.execute(text("SELECT site_key, name, config FROM sites ORDER BY site_key"))
        .mappings()
        .all()
    )
    blogs = [
        {
            "key": r["site_key"],
            "name": r["name"],
            "min_sources": int((r["config"] or {}).get("min_sources", 2)),
        }
        for r in rows
    ]
    recent = (
        db.execute(
            text(
                """SELECT i.id, i.site_key, i.topic, i.status, i.priority, i.expires_at,
                          i.created_at, i.created_by, i.canonical_url, i.timeless,
                          (SELECT count(*) FROM ideas d
                            WHERE d.duplicate_of = i.id) AS extra_sources,
                          (SELECT j.id FROM publication_jobs j WHERE j.idea_id = i.id
                            ORDER BY j.id DESC LIMIT 1) AS job_id
                     FROM ideas i WHERE i.origin = 'manual' AND i.duplicate_of IS NULL
                    ORDER BY i.id DESC LIMIT 25"""
            )
        )
        .mappings()
        .all()
    )
    queue = {
        r["site_key"]: r["n"]
        for r in db.execute(
            text(
                "SELECT site_key, count(*) AS n FROM ideas WHERE status = 'new' "
                "AND duplicate_of IS NULL AND expires_at > now() GROUP BY site_key"
            )
        ).mappings()
    }
    return templates.TemplateResponse(
        request=request,
        name="ideas.html",
        context={
            "brand": settings.brand_name,
            "primary": settings.brand_primary_color,
            "accent": settings.brand_accent_color,
            "user": _user(request),
            "msg": request.query_params.get("msg", ""),
            "err": request.query_params.get("err", ""),
            "blogs": blogs,
            "recent": [dict(r) for r in recent],
            "queue": queue,
            "priorities": PRIORITY_LABELS,
            "ttl_choices": TTL_CHOICES,
            "max_sources": MAX_SOURCES,
            "status_labels": STATUS_LABELS,
            "default_ttl": 7,
            "search_enabled": websearch.enabled(),
        },
    )


@router.post("", summary="Acrescenta uma ideia manual")
def create(
    request: Request,
    site_key: str = Form(...),
    topic: str = Form(...),
    angle: str = Form(""),
    sources: str = Form(""),
    priority: str = Form("normal"),
    ttl_days: int = Form(7),
    timeless: bool = Form(False),
):
    urls = parse_sources(sources)
    if priority not in PRIORITIES or ttl_days not in TTL_CHOICES:
        return _back(err="Prioridade ou validade inválida.")
    if not urls and not websearch.enabled():
        return _back(
            err="Indique pelo menos uma fonte (ligação https): o buscador do motor está "
            "desligado, então ele só lê as fontes que recebe."
        )
    if len(urls) > MAX_SOURCES:
        return _back(err=f"No máximo {MAX_SOURCES} fontes por ideia.")
    actor = _user(request)
    results: list[tuple[str, int | None]] = []
    try:
        with session_scope() as db:
            # E18.2: sem fontes, a pauta entra só com o tema; a pesquisa do job busca as fontes
            # (como no resgate do slot, D-101) e todos os gates seguem iguais.
            targets: list[str | None] = [*urls] or [None]
            for url in targets:
                results.append(
                    discovery.add_manual(
                        db,
                        site_key,
                        topic,
                        angle=angle.strip()[:2000],
                        url=url,
                        created_by=actor,
                        priority=PRIORITIES[priority],
                        ttl_days=ttl_days,
                        timeless=timeless,
                    )
                )
            outcome, idea_id = results[0]
            if not urls and outcome == "duplicate" and idea_id:
                similar = db.execute(text("SELECT duplicate_of FROM ideas WHERE id = :i"),
                                     {"i": idea_id}).scalar_one()
                raise ValueError(
                    f"Já existe uma pauta parecida neste blog (#{similar}); sem fontes novas, "
                    "esta não acrescentaria nada.")
            first = next((i for _, i in results if i), None)
            if first is not None:
                known = sum(1 for o, _ in results if o == "known")
                users.audit(db, actor, "idea.create", f"idea:{first}",
                            {"site": site_key, "topic": topic.strip()[:200], "priority": priority,
                             "ttl_days": ttl_days, "sources": len(urls) - known,
                             "timeless": timeless})
    except ValueError as exc:
        return _back(err=str(exc))
    known = sum(1 for outcome, _ in results if outcome == "known")
    first = next((idea_id for _, idea_id in results if idea_id), None)
    if first is None:
        return _back(err="Todas as ligações já existiam neste blog: nada foi acrescentado.")
    if not urls:
        return _back(msg=f"Pauta #{first} acrescentada ao blog {site_key} sem fontes: o motor "
                         "vai buscá-las na pesquisa.")
    text_msg = f"Pauta #{first} acrescentada ao blog {site_key} com {len(urls) - known} fonte(s)."
    if known:
        text_msg += f" {known} ligação(ões) já existia(m) e foi(ram) ignorada(s)."
    return _back(msg=text_msg)


@router.post("/{idea_id}/discard", summary="Descarta uma pauta manual ainda na fila")
def discard(idea_id: int, request: Request):
    actor = _user(request)
    with session_scope() as db:
        row = db.execute(
            text("""UPDATE ideas SET status = 'discarded'
                     WHERE id = :i AND origin = 'manual' AND status = 'new'
                       AND duplicate_of IS NULL
                 RETURNING site_key, topic"""), {"i": idea_id}).mappings().first()
        if row is None:
            return _back(err=f"A pauta #{idea_id} não pode ser descartada: já entrou em produção, "
                             "já foi usada ou não é uma pauta manual.")
        users.audit(db, actor, "idea.discard", f"idea:{idea_id}",
                    {"site": row["site_key"], "topic": row["topic"][:200]})
    return _back(msg=f"Pauta #{idea_id} descartada; não entra mais em produção.")
