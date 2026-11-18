"""E19.5: assistente de 1.º arranque — os passos e se cada um está feito, lidos do estado real.

Nada aqui guarda «passo concluído»: cada passo é feito quando o sistema já está como precisa
(a mesma régua do `/health/ready`). Assim o assistente serve ao pacote recém-instalado e não
incomoda uma instalação que já funciona (como a nossa produção: tudo feito, nenhum aviso).

Obrigatórios para ligar o blog: IA (redator e revisor verificados, de famílias diferentes) e a
credencial do WordPress. O e-mail é recomendado (avisos de aprovação), mas não bloqueia.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.services import readiness, vault
from app.services.ai import chain

# Provedores por chave de API (endpoint compatível com a OpenAI). O modelo fica a cargo de quem
# instala: os nomes mudam depressa e a família é conferida pelo nome do modelo
# (`providers._check_family`).
PRESETS: dict[str, dict[str, str]] = {
    "openai": {"label": "OpenAI", "family": "openai", "base_url": "https://api.openai.com/v1",
               "name": "openai-api", "hint": "ex.: o nome de um modelo GPT da sua conta"},
    "anthropic": {"label": "Anthropic (Claude)", "family": "anthropic",
                  "base_url": "https://api.anthropic.com/v1", "name": "anthropic-api",
                  "hint": "ex.: o nome de um modelo Claude da sua conta"},
    "google": {"label": "Google (Gemini)", "family": "google",
               "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "name": "gemini-api", "hint": "ex.: o nome de um modelo Gemini da sua conta"},
    "deepseek": {"label": "DeepSeek", "family": "deepseek", "base_url": "https://api.deepseek.com/v1",
                 "name": "deepseek-api", "hint": "ex.: deepseek-chat"},
    "mistral": {"label": "Mistral", "family": "mistral", "base_url": "https://api.mistral.ai/v1",
                "name": "mistral-api", "hint": "ex.: o nome de um modelo Mistral da sua conta"},
    "custom": {"label": "Outro (compatível com OpenAI)", "family": "", "base_url": "https://",
               "name": "", "hint": "o identificador do modelo no seu provedor"},
}


@dataclass
class Step:
    key: str
    title: str
    done: bool
    required: bool
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)


def ai_state(db: Session) -> dict[str, Any]:
    """Redação e revisão prontas de verdade: um redator verificado e um revisor verificado de
    OUTRA família (o motor nunca deixa a mesma família rever o que escreveu)."""
    writers = [p for p in chain.load_chain(db, "writer") if p.enabled and p.health == "ok"]
    reviewers = [p for p in chain.load_chain(db, "reviewer") if p.enabled and p.health == "ok"]
    pair = next(((w, r) for w in writers for r in reviewers if r.family != w.family), None)
    families = sorted({p.family for p in writers + reviewers})
    if pair:
        writer, reviewer = pair
        summary = (f"redator {writer.name} ({writer.family}) e "
                   f"revisor {reviewer.name} ({reviewer.family})")
    elif writers or reviewers:
        summary = (f"só a família {', '.join(families)} verificada: falta um provedor de OUTRA "
                   "família (quem escreve nunca revê o próprio texto)")
    else:
        summary = "nenhum provedor verificado"
    return {"ok": pair is not None, "summary": summary, "families": families}


def steps(db: Session, user_email: str) -> list[Step]:
    sites = [dict(r) for r in db.execute(text(
        "SELECT site_key, name, base_url, enabled, config->>'description' AS description "
        "FROM sites ORDER BY site_key")).mappings()]
    admins = db.execute(text(
        "SELECT count(*) FROM users WHERE role = 'admin' AND active AND NOT must_change_password"
    )).scalar() or 0
    ai = ai_state(db)
    wp = readiness.wordpress_ready(db)
    mail = vault.mail_ready()
    enabled = [s for s in sites if s["enabled"]]
    return [
        Step("admin", "Administrador", admins > 0, True,
             f"{user_email} — senha própria definida" if admins else "troque a senha inicial"),
        Step("blog", "O blog", bool(sites), True,
             ", ".join(f"{s['name']} ({s['base_url']})" for s in sites) or "nenhum blog no banco",
             {"sites": sites}),
        Step("ai", "Inteligência artificial", ai["ok"], True, ai["summary"], ai),
        Step("email", "E-mail (SMTP)", mail, False,
             "remetente e servidor configurados" if mail else
             "sem e-mail: os avisos de aprovação não saem (o portal funciona sem ele)"),
        Step("wordpress", "Credencial do WordPress", wp["ok"], True,
             f"{wp['have']}/{wp['total']} blog(s) com credencial"
             + (f" (faltam: {', '.join(wp['missing'])})" if wp["missing"] else ""), wp),
        Step("enable", "Ligar o blog", bool(enabled), True,
             ", ".join(s["name"] for s in enabled) + " ligado(s)" if enabled else
             "desligado: liga depois da IA e do WordPress"),
    ]


def pending(db: Session) -> bool:
    """Algum passo obrigatório por fazer? (aviso na sala de controle e destino após a 1.ª senha)"""
    return any(s.required and not s.done for s in steps(db, ""))


def can_enable(found: list[Step]) -> tuple[bool, str]:
    """Ligar o blog só com a IA e o WordPress prontos: sem eles cada slot terminaria BLOCKED."""
    missing = [s.title for s in found if s.key in ("ai", "wordpress") and not s.done]
    if missing:
        return False, "Antes de ligar o blog: " + " e ".join(missing) + "."
    return True, ""
