"""Cadeia de prioridade dos provedores de IA, por papel (E3.5).

Cada papel (redator, revisor, tradutor, pesquisador) tem uma lista ordenada de
provedores em `ai_role_chain`. Uma chamada percorre a lista até um responder;
timeout, cota esgotada, resposta vazia ou provedor não autenticado passam ao
seguinte.

Regra do projeto: **quem escreve nunca revê**. Vale pela família do modelo
(anthropic, openai, google…): o papel de revisor exige saber a família de quem
escreveu e salta os provedores dessa família, seja qual for a ordem configurada.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.db import session_scope
from app.core.logging import get_logger
from app.services import vault
from app.services.ai import gateway, health, prompting, quota
from app.services.ai.secrets import SecretUnreadable, decrypt

log = get_logger(__name__)

ROLES = ("writer", "reviewer", "translator", "researcher")

# Falhas em que o pedido chegou ao modelo e gastou cota (contam para o limite
# diário). Ficam de fora: gateway inalcançável, provedor mal configurado, não
# autenticado e cota recusada — nesses o modelo não chegou a trabalhar.
_SPENDS_QUOTA = frozenset({"timeout", "empty", "invalid_output", "failed"})


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    kind: str          # subscription | api
    family: str
    model: str
    base_url: str | None
    api_key_enc: str | None
    enabled: bool
    # Último estado conhecido (E3.6); None = ainda não verificado.
    health: str | None = None
    # Limites e preços (E3.8); None = sem limite / sem preço.
    daily_call_limit: int | None = None
    daily_budget_usd: Decimal | None = None
    monthly_budget_usd: Decimal | None = None
    price_in_usd_mtok: Decimal | None = None
    price_out_usd_mtok: Decimal | None = None

    def __repr__(self) -> str:
        return f"ProviderConfig(name={self.name!r}, family={self.family!r})"


@dataclass(frozen=True)
class ChainResult:
    provider: str
    family: str
    model: str
    text: str
    data: dict[str, Any] | None
    input_tokens: int
    output_tokens: int
    # (provedor, motivo) dos que falharam antes deste, por ordem.
    skipped: tuple[tuple[str, str], ...]


class ChainInvalid(ValueError):
    """A configuração pedida viola uma regra (ver `problems`)."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class ChainExhausted(RuntimeError):
    """Nenhum provedor da cadeia respondeu."""

    def __init__(self, role: str, attempts: tuple[tuple[str, str], ...]) -> None:
        tried = ", ".join(f"{name}: {reason}" for name, reason in attempts) or "cadeia vazia"
        super().__init__(f"Sem provedor de IA disponível para o papel {role} ({tried})")
        self.role = role
        self.attempts = attempts


class JobCallCap(ChainExhausted):
    """O job atingiu o orçamento diário de chamadas de IA (`llm_job_cap`).

    Subclasse de `ChainExhausted` de propósito: a produção já trata a cadeia
    esgotada com RETRY_WAIT e espera a dobrar (R27), que é o comportamento certo
    aqui — o orçamento renova com a cota do dia.
    """

    def __init__(self, role: str, job_id: int) -> None:
        super().__init__(role, ((f"job {job_id}", "limit:llm_job_cap"),))
        args = (f"Job {job_id} no orçamento diário de chamadas de IA "
                f"(llm_job_cap: {vault.limit('llm_job_cap')}); espera a cota do dia renovar",)
        self.args = args
        self.job_id = job_id


# As colunas que montam um ProviderConfig (as mesmas em todas as consultas de provedor).
_PROVIDER_COLUMNS = """p.name, p.kind, p.family, p.model, p.base_url, p.api_key_enc, p.enabled,
                       h.status AS health, p.daily_call_limit, p.daily_budget_usd,
                       p.monthly_budget_usd, p.price_in_usd_mtok, p.price_out_usd_mtok"""


def load_chain(db: Session, role: str) -> list[ProviderConfig]:
    """Provedores do papel, por ordem de prioridade (inclui os desligados)."""
    _check_role(role)
    rows = db.execute(
        text(f"""SELECT {_PROVIDER_COLUMNS}
                  FROM ai_role_chain c JOIN ai_providers p ON p.id = c.provider_id
                  LEFT JOIN ai_provider_health h ON h.provider_id = p.id
                 WHERE c.role = :role ORDER BY c.position"""),  # noqa: S608 — colunas fixas
        {"role": role},
    ).mappings().all()
    return [ProviderConfig(**row) for row in rows]


def problems(chains: dict[str, list[ProviderConfig]]) -> list[str]:
    """Regras da configuração; lista vazia = válida."""
    found: list[str] = []
    active = {role: [p for p in chains.get(role, []) if p.enabled] for role in ROLES}
    for role in ROLES:
        if not active[role]:
            found.append(f"papel {role} sem nenhum provedor ativo")
    reviewer_families = {p.family for p in active["reviewer"]}
    for writer in active["writer"]:
        if reviewer_families and not reviewer_families - {writer.family}:
            found.append(
                f"redator {writer.name} ({writer.family}) não tem revisor de outra família"
            )
    return found


def validate(db: Session) -> list[str]:
    """Problemas da configuração guardada (usado no arranque e no painel)."""
    return problems({role: load_chain(db, role) for role in ROLES})


def _unusable(db: Session, provider: ProviderConfig, site: str | None) -> str | None:
    """Por que o provedor não serve agora; None = utilizável. Mesmos critérios da `run`."""
    if not provider.enabled:
        return "disabled"
    if provider.health in health.BLOCKING:
        return f"unhealthy:{provider.health}"
    over = quota.blocked(db, provider, site)
    if over:
        return f"limit:{over}"
    return None


def production_block(db: Session, site: str | None = None) -> str | None:
    """Motivo para adiar a produção agora; None = há provedores para produzir.

    Produzir exige pesquisador, redator e um revisor de família diferente da do
    redator, todos utilizáveis (ligados, saudáveis e com cota). Verificar antes de
    a tentativa começar evita gastar pauta, buscas e chamadas numa tentativa que
    morreria na revisão (R27, 2/11: as voltas queimaram as cotas do dia).
    """
    usable: dict[str, list[ProviderConfig]] = {}
    reasons: dict[str, list[str]] = {}
    for role in ("researcher", "writer", "reviewer"):
        usable[role], reasons[role] = [], []
        for provider in load_chain(db, role):
            why = _unusable(db, provider, site)
            if why is None:
                usable[role].append(provider)
            else:
                reasons[role].append(f"{provider.name}: {why}")
        if role != "reviewer" and not usable[role]:
            tried = ", ".join(reasons[role]) or "cadeia vazia"
            return f"Sem provedor de IA disponível para o papel {role} ({tried})"
    paired = any(r.family != w.family for w in usable["writer"] for r in usable["reviewer"])
    if not paired:
        # Sem par: todo revisor utilizável é da família do(s) redator(es) que restou(aram).
        tried = ", ".join(reasons["reviewer"]
                          + [f"{p.name}: same_family_as_writer" for p in usable["reviewer"]])
        writers = ", ".join(f"{p.name} ({p.family})" for p in usable["writer"])
        return (f"Sem provedor de IA disponível para o papel reviewer ({tried}) "
                f"para os redatores utilizáveis ({writers})")
    return None


def set_chain(db: Session, role: str, provider_names: list[str]) -> None:
    """Substitui a cadeia de um papel. Rejeita (sem gravar) o que violar as regras."""
    _check_role(role)
    if len(set(provider_names)) != len(provider_names):
        raise ChainInvalid([f"provedor repetido na cadeia de {role}"])
    rows = db.execute(
        text("""SELECT id, name, kind, family, model, base_url, api_key_enc, enabled
                  FROM ai_providers WHERE name = ANY(:names)"""),
        {"names": provider_names},
    ).mappings().all()
    by_name = {row["name"]: row for row in rows}
    unknown = [name for name in provider_names if name not in by_name]
    if unknown:
        raise ChainInvalid([f"provedor desconhecido: {', '.join(unknown)}"])

    chains = {r: load_chain(db, r) for r in ROLES}
    chains[role] = [
        ProviderConfig(**{k: v for k, v in by_name[name].items() if k != "id"})
        for name in provider_names
    ]
    found = problems(chains)
    if found:
        raise ChainInvalid(found)

    db.execute(text("DELETE FROM ai_role_chain WHERE role = :role"), {"role": role})
    for position, name in enumerate(provider_names, start=1):
        db.execute(
            text("INSERT INTO ai_role_chain (role, position, provider_id) VALUES (:r, :p, :id)"),
            {"r": role, "p": position, "id": by_name[name]["id"]},
        )


def run(
    role: str,
    prompt: str,
    *,
    system: str = "",
    schema: dict[str, Any] | None = None,
    purpose: str = "generic",
    article_id: int | None = None,
    writer_family: str | None = None,
    site: str | None = None,
    external: tuple[prompting.External, ...] | list[prompting.External] = (),
    timeout_s: float | None = None,
    avoid_family: str | None = None,
) -> ChainResult:
    """Chama o primeiro provedor disponível do papel; em falha, o seguinte.

    Para `reviewer`, `writer_family` é obrigatório: é a família de quem escreveu
    o texto a rever, e os provedores dessa família são saltados.
    `avoid_family` é preferência, não exclusão (B da D-101): depois de um tropeço
    técnico dessa família, as outras vêm primeiro; sem outra, repete-se a mesma.
    `site` é o blog a que a chamada pertence (custos e orçamento por blog, E3.8).
    `prompt` e `system` são texto do motor; tudo o que vem de fora (RSS, páginas,
    ideias) entra por `external` e é delimitado como dados (E3.9). As regras de
    segurança são sempre acrescentadas ao prompt de sistema.
    """
    _check_role(role)
    if role == "reviewer" and not writer_family:
        raise ValueError("o papel reviewer exige writer_family (quem escreve nunca revê)")

    # Orçamento diário por job (llm_job_cap): verificado por chamada, porque o gasto
    # cresce durante a produção; a pré-checagem do `produce` cobre só o arranque.
    job_id = quota.current_job()
    if job_id is not None and vault.limit("llm_job_cap") > 0:
        with session_scope() as db:
            over = quota.job_blocked(db, job_id)
        if over:
            log.warning("IA: job %d no orçamento diário de chamadas (%s) no papel %s",
                        job_id, over, role)
            raise JobCallCap(role, job_id)

    system = prompting.system_prompt(system)
    prompt = prompting.user_prompt(prompt, external)

    with session_scope() as db:
        chain = load_chain(db, role)
    if avoid_family:
        chain = [*(p for p in chain if p.family != avoid_family),
                 *(p for p in chain if p.family == avoid_family)]

    attempts: list[tuple[str, str]] = []
    for provider in chain:
        if not provider.enabled:
            attempts.append((provider.name, "disabled"))
            continue
        if role == "reviewer" and provider.family == writer_family:
            attempts.append((provider.name, "same_family_as_writer"))
            continue
        if provider.health in health.BLOCKING:
            # Fora da cadeia até o probe o repor (E3.6).
            attempts.append((provider.name, f"unhealthy:{provider.health}"))
            continue
        with session_scope() as db:
            over = quota.blocked(db, provider, site)
        if over:
            # Limite diário de chamadas ou orçamento atingido (E3.8).
            log.warning("IA: %s fora da cadeia no papel %s (%s)", provider.name, role, over)
            attempts.append((provider.name, f"limit:{over}"))
            continue
        try:
            endpoint = _endpoint(provider)
            result = gateway.call(
                provider.name, prompt, system=system, schema=schema,
                model=provider.model, endpoint=endpoint, timeout_s=timeout_s,
            )
        except gateway.GatewayError as exc:
            log.warning("IA: %s falhou no papel %s (%s); a tentar o seguinte",
                        provider.name, role, exc.kind)
            attempts.append((provider.name, exc.kind))
            health.record_call(provider.name, exc.kind)
            if exc.kind in _SPENDS_QUOTA:
                quota.record(provider, role=role, purpose=purpose, model=provider.model,
                             site=site, article_id=article_id, outcome=exc.kind)
            continue
        except SecretUnreadable:
            log.error("IA: API key de %s ilegível; a tentar o seguinte", provider.name)
            attempts.append((provider.name, "secret_unreadable"))
            continue

        health.record_call(provider.name, None)
        model = result.model or provider.model or provider.name
        quota.record(
            provider, role=role, purpose=purpose, model=model, site=site,
            article_id=article_id,
            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
        )
        return ChainResult(
            provider=provider.name, family=provider.family, model=model,
            text=result.text, data=result.data,
            input_tokens=result.input_tokens, output_tokens=result.output_tokens,
            skipped=tuple(attempts),
        )
    raise ChainExhausted(role, tuple(attempts))


class ImageUnavailable(RuntimeError):
    """O provedor de imagem não está disponível (desligado, doente, sem cota ou falhou)."""


def run_image(prompt: str, *, purpose: str = "illustration_image", site: str | None = None,
              article_id: int | None = None, timeout_s: float | None = None) -> bytes:
    """Pede uma imagem ao provedor de imagem (D-056), com a mesma contabilidade da `run`.

    Não há cadeia: só o Codex gera imagens. Os mesmos travões valem (desligado, estado do
    probe, limite diário de chamadas) e a tentativa fica em `provider_usage`.
    """
    name = settings.ai_image_provider
    with session_scope() as db:
        row = db.execute(
            text(f"""SELECT {_PROVIDER_COLUMNS}
                      FROM ai_providers p LEFT JOIN ai_provider_health h ON h.provider_id = p.id
                     WHERE p.name = :n"""),  # noqa: S608 — colunas fixas
            {"n": name}).mappings().first()
        if row is None:
            raise ImageUnavailable(f"provedor de imagem {name!r} desconhecido")
        provider = ProviderConfig(**row)
        over = quota.blocked(db, provider, site) if provider.enabled else None
    if not provider.enabled:
        raise ImageUnavailable(f"{name} desligado")
    if provider.health in health.BLOCKING:
        raise ImageUnavailable(f"{name} fora do ar ({provider.health})")
    if over:
        raise ImageUnavailable(f"{name} no limite ({over})")
    try:
        result = gateway.image(prompt, provider=name, timeout_s=timeout_s)
    except gateway.GatewayError as exc:
        health.record_call(name, exc.kind)
        if exc.kind in _SPENDS_QUOTA:
            quota.record(provider, role="illustrator", purpose=purpose, model=provider.model,
                         site=site, article_id=article_id, outcome=exc.kind)
        raise ImageUnavailable(f"{name}: {exc.kind}") from None
    health.record_call(name, None)
    quota.record(provider, role="illustrator", purpose=purpose,
                 model=result.model or provider.model or name, site=site, article_id=article_id,
                 input_tokens=result.input_tokens, output_tokens=result.output_tokens)
    return result.data


def _endpoint(provider: ProviderConfig) -> gateway.Endpoint | None:
    if provider.kind != "api":
        return None
    if not provider.api_key_enc or not provider.base_url:
        raise gateway.GatewayError(provider.name, "not_configured", "sem API key ou base_url")
    return gateway.Endpoint(provider.base_url, decrypt(provider.api_key_enc), provider.model)


def _check_role(role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"papel desconhecido: {role!r} (válidos: {', '.join(ROLES)})")
