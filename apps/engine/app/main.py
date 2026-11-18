"""ShortsBlog — motor de conteúdo (fork do Growth Engine, ver UPSTREAM.md).

Superfícies:
  /                → painel (Cloudflare Access + login próprio, D-018 — core/panel_auth.py)
  /api/**          → API de máquina (bearer token) — é o que o n8n chama
  /media           → imagens geradas (pública)
  /health, /metrics → monitoria
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.db import run_migrations
from app.core.logging import get_logger, setup_logging
from app.services import vault

setup_logging()
log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info("Motor ShortsBlog iniciando", extra={"ctx": {
        "environment": settings.environment, "dry_run": settings.dry_run,
        "human_gate": settings.require_human_approval,
    }})

    if settings.run_migrations_on_startup:
        try:
            applied = run_migrations()
            if applied:
                log.info("Migrações aplicadas: %s", ", ".join(applied))
        except Exception:  # noqa: BLE001
            log.exception("Falha ao aplicar migrações — a API sobe degradada")

    # E3.5: a cadeia de provedores de IA é validada no arranque (redator e
    # revisor de famílias diferentes; nenhum papel sem provedor ativo).
    try:
        from app.core.db import session_scope
        from app.services.ai import chain

        with session_scope() as db:
            for problem in chain.validate(db):
                log.error("CADEIA DE IA INVÁLIDA: %s", problem)
    except Exception:  # noqa: BLE001
        log.exception("Não foi possível validar a cadeia de provedores de IA")

    # Avisos altos e claros sobre o que está faltando configurar.
    try:
        from app.core.db import session_scope
        from app.services import readiness

        with session_scope() as db:
            wp = readiness.wordpress_ready(db)
        if not wp["ok"]:
            log.warning("NÃO CONFIGURADO: WordPress sem credencial em %s — a publicação desses "
                        "sites ficará parada", ", ".join(wp["missing"]) or "nenhum site")
    except Exception:  # noqa: BLE001
        log.exception("Não foi possível verificar as credenciais do WordPress")
    if not vault.mail_ready():
        log.warning("NÃO CONFIGURADO: E-mail ao operador (Brevo/SMTP) — a fase correspondente "
                    "ficará parada")

    yield
    log.info("Motor ShortsBlog encerrando")


app = FastAPI(
    title="ShortsBlog — motor de conteúdo",
    description=(
        "Pesquisa, redige, revê e publica posts nos blogs ShortsBlog, "
        "sempre com aprovação humana. Agendado pelo n8n."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
    openapi_url="/openapi.json",
)


@app.exception_handler(Exception)
async def unhandled(request, exc: Exception):
    log.exception("Erro não tratado em %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Erro interno", "path": str(request.url.path)},
    )


from app.api.routes import (  # noqa: E402
    ai,
    ai_panel,
    auth,
    config_panel,
    dashboard,
    health,
    health_panel,
    ideas,
    jobs_panel,
    links,
    newsletter,
    pauta,
    public,
    review,
    sched,
    setup_panel,
    sites_panel,
    system_panel,
    users_panel,
)
from app.core.panel_auth import panel_guard  # noqa: E402

app.middleware("http")(panel_guard)
app.include_router(auth.router)

app.include_router(health.router)
app.include_router(public.router)
app.include_router(sched.router)
app.include_router(ai.router)
app.include_router(dashboard.router)
app.include_router(ai_panel.router)
app.include_router(review.router)
app.include_router(ideas.router)
app.include_router(links.router)
app.include_router(newsletter.router)
app.include_router(pauta.router)
app.include_router(health_panel.router)
app.include_router(users_panel.router)
app.include_router(config_panel.router)
app.include_router(jobs_panel.router)
app.include_router(sites_panel.router)
app.include_router(system_panel.router)
app.include_router(setup_panel.router)
