"""E18.3a (D-104): configuração editorial — o banco é a fonte da verdade, o YAML é semente.

Antes, cada deploy (`sb-engine-migrate`) SOBRESCREVIA no banco os sites (com a política
editorial), os feeds e as newsletters de pauta a partir de `content-config/`. Com o portal
editando o banco, isso apagaria as edições. Agora:

- `seed()` (no deploy) aplica só as ADIÇÕES — blog, feed ou lista de newsletters que ainda não
  existe no banco — e conta o resto como pendente (aviso no log);
- `plan()` compara o YAML com o banco e devolve as mudanças (adicionar, alterar, desligar),
  com o antes e o depois e se aquilo foi editado no portal depois da última importação;
- `apply()` aplica um plano (reimportar: `make reimport-apply` ou a tela Configuração) e grava
  cada mudança no `audit_log` (`config.import`) com antes/depois — histórico e desfazer manual.

O sem-leitura (`no_fetch`) também entrou no banco na entrega 3. A tela edita só as colunas de
`sites` que o papel da app pode escrever; colunas técnicas e blog novo só pela linha de comando
(admin).
"""
from __future__ import annotations

import difflib
import hashlib
import json
import pathlib
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.services import discovery, pauta, sites, terms, users

log = get_logger(__name__)

SITE_COLUMNS = sites._COLUMNS  # noqa: SLF001 — a mesma lista de colunas da sincronização antiga
SOURCE_FIELDS = ("name", "domain", "source_type", "tier", "lang", "usage_notes", "enabled")
IMPORT_ACTION = "config.import"
EDIT_ACTION = "config.edit"      # gravado pelas telas de edição (entregas 2 e 3)
UNDO_ACTION = "config.undo"
# Regras editoriais comuns aos blogs (`policies/_comum.md`). Cada `sites.config.policy` é
# «comum + \n\n + regras do blog» — é o que os workers leem; o comum guardado à parte deixa o
# portal editá-lo uma vez para todos (E18.3a entrega 2).
POLICY_COMMON_KEY = "editorial.policy_common"


@dataclass
class Change:
    entity: str                   # site | source | newsletters
    key: str                      # cloud | cloud https://… | newsletters
    kind: str                     # add | update | disable
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    fields: list[str] = field(default_factory=list)
    edited_in_portal: bool = False
    note: str = ""                # resumo legível (listas: domínios entrando/saindo)

    @property
    def target(self) -> str:
        return f"{self.entity}:{self.key}"

    def text_diffs(self) -> list[tuple[str, str]]:
        """(campo, diff unificado) dos campos de texto longo — a política, sobretudo."""
        out = []
        for name in self.fields:
            old, new = _get(self.before, name), _get(self.after, name)
            if isinstance(old, str) and isinstance(new, str) and ("\n" in old or "\n" in new):
                diff = difflib.unified_diff(old.splitlines(), new.splitlines(),
                                            "banco", "yaml", lineterm="", n=1)
                out.append((name, "\n".join(diff)))
        return out

    def short(self, name: str) -> tuple[str, str]:
        """Antes e depois de um campo, curtos para a tabela."""
        return _brief(_get(self.before, name)), _brief(_get(self.after, name))


def _get(data: dict[str, Any] | None, name: str) -> Any:
    if data is None:
        return None
    if name.startswith("config."):
        return (data.get("config") or {}).get(name[len("config."):])
    return data.get(name)


def _brief(value: Any) -> str:
    if value is None:
        return "—"
    shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    shown = " ".join(shown.split())
    return shown if len(shown) <= 90 else shown[:87] + "…"


# --- leitura -------------------------------------------------------------------------------
def _desired_sites() -> dict[str, dict[str, Any]]:
    out = {}
    for site in sites.load_all():
        values = {name: getattr(site, name) for name in SITE_COLUMNS}
        config = site.extra_config()
        config["policy"] = sites.load_policy(site.site_key)
        values["config"] = config
        out[site.site_key] = values
    return out


def _current_sites(db: Session) -> dict[str, dict[str, Any]]:
    rows = db.execute(text(f"SELECT {', '.join(SITE_COLUMNS)}, config FROM sites")).mappings()
    return {r["site_key"]: {**dict(r), "config": dict(r["config"] or {})} for r in rows}


def _current_sources(db: Session) -> dict[tuple[str, str], dict[str, Any]]:
    rows = db.execute(text(f"SELECT site_key, feed_url, {', '.join(SOURCE_FIELDS)} "
                           "FROM sources")).mappings()
    return {(r["site_key"], r["feed_url"]): dict(r) for r in rows}


def _desired_policy_common() -> str:
    from app.core.config import settings

    path = pathlib.Path(settings.policies_config_dir) / "_comum.md"
    if not path.is_file():
        raise sites.SiteConfigError("política editorial em falta: _comum.md")
    return path.read_text(encoding="utf-8").strip()


def _current_policy_common(db: Session) -> str | None:
    value = db.execute(text("SELECT value FROM settings WHERE key = :k"),
                       {"k": POLICY_COMMON_KEY}).scalar()
    return value if isinstance(value, str) else None


def _current_no_fetch(db: Session) -> list[dict[str, Any]] | None:
    value = db.execute(text("SELECT value FROM settings WHERE key = :k"),
                       {"k": terms.SETTING_KEY}).scalar()
    return value if isinstance(value, list) else None


def _list_note(before: list[dict[str, Any]], after: list[dict[str, Any]], key: str) -> str:
    """«+2: a, b · −1: c · 3 alterados» — o que muda numa lista identificada por `key`."""
    old = {str(e[key]): e for e in before}
    new = {str(e[key]): e for e in after}
    added = [k for k in new if k not in old]
    removed = [k for k in old if k not in new]
    changed = [k for k in new if k in old and new[k] != old[k]]
    parts = []
    if added:
        parts.append(f"+{len(added)}: " + ", ".join(added[:8]) + ("…" if len(added) > 8 else ""))
    if removed:
        parts.append(f"−{len(removed)}: " + ", ".join(removed[:8])
                     + ("…" if len(removed) > 8 else ""))
    if changed:
        parts.append(f"{len(changed)} alterado(s): " + ", ".join(changed[:8]))
    if not parts and before != after:
        parts.append("ordem alterada")
    return " · ".join(parts)


def _current_newsletters(db: Session) -> list[dict[str, Any]] | None:
    value = db.execute(text("SELECT value FROM settings WHERE key = :k"),
                       {"k": pauta.SETTING_KEY}).scalar()
    return value if isinstance(value, list) else None


def _portal_edited(db: Session, targets: list[str]) -> set[str]:
    """Alvos editados no portal depois da última importação de cada um."""
    if not targets:
        return set()
    rows = db.execute(text(
        """SELECT target,
                  max(at) FILTER (WHERE action IN (:edit, :undo)) AS edited,
                  max(at) FILTER (WHERE action = :imp) AS imported
             FROM audit_log WHERE target = ANY(:t) GROUP BY target"""),
        {"edit": EDIT_ACTION, "undo": UNDO_ACTION, "imp": IMPORT_ACTION, "t": targets}).all()
    return {t for t, edited, imported in rows
            if edited is not None and (imported is None or edited > imported)}


# --- plano -----------------------------------------------------------------------------------
def plan(db: Session) -> list[Change]:
    """Diferença YAML → banco. Valida o YAML (os mesmos erros que travariam o deploy)."""
    changes: list[Change] = []
    want_sites, have_sites = _desired_sites(), _current_sites(db)
    for key, want in want_sites.items():
        have = have_sites.get(key)
        if have is None:
            changes.append(Change("site", key, "add", None, want, ["*"]))
            continue
        diff = [c for c in SITE_COLUMNS[1:] if have.get(c) != want.get(c)]
        config_keys = sorted(set(have["config"]) | set(want["config"]))
        diff += [f"config.{k}" for k in config_keys
                 if have["config"].get(k) != want["config"].get(k)]
        if diff:
            changes.append(Change("site", key, "update", have, want, diff))
    for key, have in have_sites.items():
        if key not in want_sites and have.get("enabled"):
            after = {**have, "enabled": False}
            changes.append(Change("site", key, "disable", have, after, ["enabled"]))

    known = set(have_sites) | set(want_sites)
    want_feeds = discovery.load_feeds(known)
    have_feeds = _current_sources(db)
    for site_key, feeds in want_feeds.items():
        urls = set()
        for feed in feeds:
            url = feed["feed_url"]
            urls.add(url)
            want = {**feed, "enabled": True}
            want.pop("feed_url")
            have = have_feeds.get((site_key, url))
            key = f"{site_key} {url}"
            if have is None:
                changes.append(Change("source", key, "add", None, want, ["*"]))
                continue
            diff = [f for f in SOURCE_FIELDS if have.get(f) != want.get(f)]
            if diff:
                before = {f: have.get(f) for f in SOURCE_FIELDS}
                changes.append(Change("source", key, "update", before, want, diff))
        for (s, url), have in have_feeds.items():
            if s == site_key and url not in urls and have.get("enabled"):
                before = {f: have.get(f) for f in SOURCE_FIELDS}
                changes.append(Change("source", f"{s} {url}", "disable", before,
                                      {**before, "enabled": False}, ["enabled"]))

    want_news = pauta.load()
    have_news = _current_newsletters(db)
    if have_news is None:
        changes.append(Change("newsletters", "newsletters", "add", None,
                              {"newsletters": want_news}, ["*"]))
    elif have_news != want_news:
        changes.append(Change("newsletters", "newsletters", "update", {"newsletters": have_news},
                              {"newsletters": want_news}, ["newsletters"],
                              note=_list_note(have_news, want_news, "sender_domain")))

    want_common = _desired_policy_common()
    have_common = _current_policy_common(db)
    if have_common is None:
        changes.append(Change("policy_common", "comum", "add", None, {"text": want_common}, ["*"]))
    elif have_common != want_common:
        changes.append(Change("policy_common", "comum", "update", {"text": have_common},
                              {"text": want_common}, ["text"]))

    want_nf = [dict(e) for e in terms.load_file()]
    have_nf = _current_no_fetch(db)
    if have_nf is None:
        changes.append(Change("no_fetch", "lista", "add", None, {"sites": want_nf}, ["*"],
                              note=f"{len(want_nf)} domínios"))
    elif have_nf != want_nf:
        changes.append(Change("no_fetch", "lista", "update", {"sites": have_nf},
                              {"sites": want_nf}, ["sites"],
                              note=_list_note(have_nf, want_nf, "domain")))

    edited = _portal_edited(db, [c.target for c in changes if c.kind != "add"])
    for change in changes:
        change.edited_in_portal = change.target in edited
    return changes


def fingerprint(changes: list[Change]) -> str:
    """Identifica um plano: a tela aplica exatamente o que mostrou (se o YAML ou o banco
    mudaram entretanto, o plano é outro e pede nova revisão)."""
    payload = json.dumps([asdict(c) for c in changes], sort_keys=True, ensure_ascii=False,
                         default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:20]


# --- aplicação -----------------------------------------------------------------------------
def _write_site(db: Session, values: dict[str, Any]) -> None:
    params = {c: values[c] for c in SITE_COLUMNS}
    params["config"] = json.dumps(values["config"], ensure_ascii=False)
    assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in (*SITE_COLUMNS[1:], "config"))
    db.execute(
        text(f"""INSERT INTO sites ({', '.join(SITE_COLUMNS)}, config)
                 VALUES ({', '.join(':' + c for c in SITE_COLUMNS)}, CAST(:config AS JSONB))
                 ON CONFLICT (site_key) DO UPDATE
                    SET {assignments}, updated_at = now()"""),  # noqa: S608 — só nomes fixos
        params,
    )


def _write_source(db: Session, site_key: str, url: str, values: dict[str, Any]) -> None:
    db.execute(
        text("""INSERT INTO sources (site_key, feed_url, name, domain, source_type, tier, lang,
                                     usage_notes, enabled)
                VALUES (:s, :u, :name, :domain, :source_type, :tier, :lang, :usage_notes,
                        :enabled)
                ON CONFLICT (site_key, feed_url) DO UPDATE
                   SET name = EXCLUDED.name, domain = EXCLUDED.domain,
                       source_type = EXCLUDED.source_type, tier = EXCLUDED.tier,
                       lang = EXCLUDED.lang, usage_notes = EXCLUDED.usage_notes,
                       enabled = EXCLUDED.enabled"""),
        {"s": site_key, "u": url, **{f: values[f] for f in SOURCE_FIELDS}},
    )


def _write_newsletters(db: Session, entries: list[dict[str, Any]]) -> None:
    db.execute(
        text("""INSERT INTO settings (key, value, description) VALUES (:k, CAST(:v AS JSONB), :d)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"""),
        {"k": pauta.SETTING_KEY, "v": json.dumps(entries, ensure_ascii=False),
         "d": "Remetentes de pauta por e-mail (D-094), de content-config/newsletters.yaml"},
    )


def _write_no_fetch(db: Session, entries: list[dict[str, Any]]) -> None:
    db.execute(
        text("""INSERT INTO settings (key, value, description) VALUES (:k, CAST(:v AS JSONB), :d)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"""),
        {"k": terms.SETTING_KEY, "v": json.dumps(entries, ensure_ascii=False),
         "d": "Sites que o motor não lê para a IA (D-070), de content-config/sem-leitura.yaml"},
    )
    terms.invalidate()


def _write_policy_common(db: Session, value: str) -> None:
    db.execute(
        text("""INSERT INTO settings (key, value, description)
                VALUES (:k, to_jsonb(CAST(:v AS text)), :d)
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"""),
        {"k": POLICY_COMMON_KEY, "v": value,
         "d": "Regras editoriais comuns aos blogs (E18.3a), de content-config/policies/_comum.md"},
    )


def portal_blockers(changes: list[Change]) -> list[str]:
    """Mudanças que o papel da app não pode aplicar (blog novo, colunas técnicas): só pela linha
    de comando (`make reimport-apply`, admin) — revisão intermediária da E18, #1."""
    return [c.target for c in changes if c.entity == "site" and (
        c.kind == "add" or any(f in TECHNICAL_SITE_COLUMNS for f in c.fields))]


def apply(db: Session, changes: list[Change], actor: str, *, portal: bool = False) -> int:
    """Aplica as mudanças (sites antes dos feeds) e grava cada uma na auditoria.

    `portal=True` (tela, papel da app): só UPDATE das colunas editáveis de `sites`; blog novo ou
    coluna técnica são recusados antes de gravar qualquer coisa."""
    if portal and (blocked := portal_blockers(changes)):
        raise EditError("estas mudanças mexem em colunas técnicas ou criam blog e só se aplicam "
                        "pela linha de comando (make reimport-apply): " + ", ".join(blocked))
    order = {"policy_common": 0, "site": 1, "source": 2, "newsletters": 3, "no_fetch": 4}
    for change in sorted(changes, key=lambda c: order[c.entity]):
        after = change.after or {}
        if change.entity == "site":
            if portal:
                _update_site_portal(db, after)
            else:
                _write_site(db, after)
        elif change.entity == "source":
            site_key, url = change.key.split(" ", 1)
            _write_source(db, site_key, url, after)
        elif change.entity == "policy_common":
            _write_policy_common(db, after["text"])
        elif change.entity == "no_fetch":
            _write_no_fetch(db, after["sites"])
        else:
            _write_newsletters(db, after["newsletters"])
        users.audit(db, actor, IMPORT_ACTION, change.target,
                    {"kind": change.kind, "fields": change.fields,
                     "edited_in_portal": change.edited_in_portal,
                     "before": change.before, "after": change.after})
    return len(changes)


def seed(db: Session, actor: str = "deploy") -> tuple[list[Change], list[Change]]:
    """No deploy: aplica só as adições; devolve (aplicadas, pendentes de reimportação)."""
    changes = plan(db)
    adds = [c for c in changes if c.kind == "add"]
    apply(db, adds, actor)
    return adds, [c for c in changes if c.kind != "add"]


def summary(changes: list[Change]) -> str:
    parts = [f"{c.target} ({c.kind}: {', '.join(c.fields)})" for c in changes[:12]]
    more = f" e mais {len(changes) - 12}" if len(changes) > 12 else ""
    return "; ".join(parts) + more


def config_root_ok() -> bool:
    """A API só faz a reimportação se o `content-config` estiver montado."""
    from app.core.config import settings

    return pathlib.Path(settings.sites_config_dir).is_dir()


# --- edição pelo portal (E18.3a entrega 2) --------------------------------------------------
class EditError(ValueError):
    """Edição recusada; a mensagem é segura para mostrar na tela."""


# Campos que o portal edita no formulário do site. Os técnicos (URLs do WordPress, fuso, idioma
# principal, grupo de hospedagem, versões) ficam só de leitura: mexer neles quebra a publicação;
# mudam pelo YAML + reimportação.
SITE_EDITABLE = ("name", "enabled", "daily_target", "slot_offset_minutes", "languages")
SITE_EDITABLE_CONFIG = ("description", "min_sources", "categories", "recipe_mode",
                        "search_policy")
SITE_READONLY = ("base_url", "wordpress_api_url", "hosting_group", "timezone", "language",
                 "seo_plugin", "taxonomy_version", "editorial_policy_version")
POLICY_MAX = 20_000
# Colunas de `sites` que o papel da app pode atualizar (as de `migrate.SITES_APP_COLUMNS`, além de
# `config`); as técnicas só o admin escreve (migrate/reimport pela linha de comando).
PORTAL_SITE_COLUMNS = ("name", "enabled", "daily_target", "slot_offset_minutes", "languages")
TECHNICAL_SITE_COLUMNS = tuple(c for c in SITE_COLUMNS[1:] if c not in PORTAL_SITE_COLUMNS)


def version(value: Any) -> str:
    """Impressão do valor que a tela mostrou: gravar só se ele não mudou entretanto."""
    payload = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _check_version(current: Any, expected: str, what: str) -> None:
    if expected and version(current) != expected:
        raise EditError(f"{what} mudou desde que a tela foi aberta (outra pessoa ou outra aba): "
                        "recarregue a página e refaça a mudança")


def _lock_site(db: Session, site_key: str) -> None:
    db.execute(text("SELECT 1 FROM sites WHERE site_key = :k FOR UPDATE"), {"k": site_key})


def _lock_setting(db: Session, key: str) -> None:
    db.execute(text("SELECT 1 FROM settings WHERE key = :k FOR UPDATE"), {"k": key})


def _update_site_portal(db: Session, values: dict[str, Any]) -> None:
    """UPDATE só das colunas que o papel da app pode escrever (nunca o destino de publicação)."""
    sets = ", ".join(f"{c} = :{c}" for c in PORTAL_SITE_COLUMNS)
    params = {c: values[c] for c in PORTAL_SITE_COLUMNS}
    params.update(site_key=values["site_key"],
                  config=json.dumps(values["config"], ensure_ascii=False))
    db.execute(text(f"UPDATE sites SET {sets}, config = CAST(:config AS JSONB), "  # noqa: S608
                    "updated_at = now() WHERE site_key = :site_key"), params)


def site_values(db: Session, site_key: str) -> dict[str, Any]:
    site = _current_sites(db).get(site_key)
    if site is None:
        raise EditError(f"blog desconhecido: {site_key}")
    return site


def own_policy(combined: str, common: str) -> str | None:
    """A parte do blog na política montada; None se ela não começa pelo comum atual."""
    prefix = common + "\n\n"
    return combined[len(prefix):] if combined.startswith(prefix) else None


def policies(db: Session) -> tuple[str, dict[str, str | None]]:
    """(comum, {blog: regras do blog ou None se a política montada estiver incoerente})."""
    common = _current_policy_common(db)
    if common is None:
        raise EditError("as regras comuns ainda não estão no banco: faça o deploy (ele as semeia) "
                        "ou reimporte do YAML")
    return common, {k: own_policy(str(v["config"].get("policy", "")), common)
                    for k, v in sorted(_current_sites(db).items())}


def _validate_site(values: dict[str, Any]) -> None:
    config = {k: v for k, v in values["config"].items() if k != "policy"}
    try:
        sites.SiteConfig.model_validate({**{c: values[c] for c in SITE_COLUMNS}, **config})
    except Exception as exc:  # pydantic.ValidationError: mostra só as mensagens
        errors = getattr(exc, "errors", None)
        if callable(errors):
            raise EditError("; ".join(
                f"{'.'.join(str(p) for p in e.get('loc', ())) or 'site'}: {e.get('msg')}"
                for e in errors())) from None
        raise EditError(str(exc)) from None


def _changed_fields(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    fields = [c for c in SITE_COLUMNS[1:] if before.get(c) != after.get(c)]
    keys = sorted(set(before["config"]) | set(after["config"]))
    return fields + [f"config.{k}" for k in keys
                     if before["config"].get(k) != after["config"].get(k)]


def _save_site(db: Session, before: dict[str, Any], after: dict[str, Any], actor: str,
               action: str = EDIT_ACTION, extra: dict[str, Any] | None = None) -> list[str]:
    fields = _changed_fields(before, after)
    if not fields:
        return []
    technical = [f for f in fields if f in TECHNICAL_SITE_COLUMNS]
    if technical:
        raise EditError("colunas técnicas (" + ", ".join(technical) + ") só mudam pelo YAML e "
                        "pela linha de comando (make reimport-apply)")
    _update_site_portal(db, after)
    users.audit(db, actor, action, f"site:{before['site_key']}",
                {"kind": "update", "fields": fields, "before": before, "after": after,
                 **(extra or {})})
    return fields


def edit_site(db: Session, site_key: str, form: dict[str, Any], actor: str,
              expected: str = "") -> list[str]:
    """Aplica os campos editáveis do formulário; valida como o deploy. Devolve os campos mudados.

    `expected`: a `version` do blog que a tela mostrou (edição simultânea é recusada)."""
    _lock_site(db, site_key)
    before = site_values(db, site_key)
    _check_version(before, expected, "o blog")
    after = {**before, "config": dict(before["config"])}
    for name in SITE_EDITABLE:
        if name in form:
            after[name] = form[name]
    for name in SITE_EDITABLE_CONFIG:
        if name not in form:
            continue
        value = form[name]
        if value in (None, "", [], {}):
            after["config"].pop(name, None)
        else:
            after["config"][name] = value
    if after["language"] not in after["languages"]:
        raise EditError(f"o idioma principal ({after['language']}) não pode sair da lista")
    _validate_site(after)
    return _save_site(db, before, after, actor)


def edit_policy_own(db: Session, site_key: str, own: str, actor: str,
                    expected: str = "") -> list[str]:
    own = own.replace("\r\n", "\n").strip()
    if not own:
        raise EditError("as regras do blog não podem ficar vazias")
    if len(own) > POLICY_MAX:
        raise EditError(f"as regras do blog passam de {POLICY_MAX} caracteres")
    _lock_setting(db, POLICY_COMMON_KEY)
    _lock_site(db, site_key)
    common, owns = policies(db)
    _check_version(owns.get(site_key), expected, "as regras do blog")
    before = site_values(db, site_key)
    after = {**before, "config": {**before["config"], "policy": f"{common}\n\n{own}"}}
    return _save_site(db, before, after, actor)


def edit_policy_common(db: Session, common: str, actor: str, expected: str = "") -> list[str]:
    """Troca as regras comuns e remonta a política de cada blog (um registro por blog)."""
    common = common.replace("\r\n", "\n").strip()
    if not common:
        raise EditError("as regras comuns não podem ficar vazias")
    if len(common) > POLICY_MAX:
        raise EditError(f"as regras comuns passam de {POLICY_MAX} caracteres")
    _lock_setting(db, POLICY_COMMON_KEY)
    old, owns = policies(db)
    _check_version(old, expected, "as regras comuns")
    broken = [k for k, v in owns.items() if v is None]
    if broken:
        raise EditError("a política montada de " + ", ".join(broken) + " não começa pelas regras "
                        "comuns atuais; reimporte do YAML antes de editar o comum")
    if common == old:
        return []
    _write_policy_common(db, common)
    users.audit(db, actor, EDIT_ACTION, "policy_common:comum",
                {"kind": "update", "fields": ["text"], "before": {"text": old},
                 "after": {"text": common}})
    touched = []
    for key, own in owns.items():
        before = site_values(db, key)
        after = {**before, "config": {**before["config"], "policy": f"{common}\n\n{own}"}}
        if _save_site(db, before, after, actor, extra={"via": "policy_common"}):
            touched.append(key)
    return touched


# --- histórico e desfazer -------------------------------------------------------------------
def history(db: Session, limit: int = 40) -> list[dict[str, Any]]:
    rows = db.execute(text(
        """SELECT id, at, actor, action, target, details->>'kind' AS kind,
                  details->'fields' AS fields, details->>'via' AS via,
                  details->>'undo_of' AS undo_of
             FROM audit_log WHERE action IN (:i, :e, :u)
            ORDER BY id DESC LIMIT :n"""),
        {"i": IMPORT_ACTION, "e": EDIT_ACTION, "u": UNDO_ACTION, "n": limit}).mappings()
    return [dict(r) for r in rows]


def _current_value(db: Session, entity: str, key: str) -> dict[str, Any] | None:
    if entity == "site":
        return _current_sites(db).get(key)
    if entity == "source":
        site_key, url = key.split(" ", 1)
        have = _current_sources(db).get((site_key, url))
        return {f: have.get(f) for f in SOURCE_FIELDS} if have else None
    if entity == "policy_common":
        common = _current_policy_common(db)
        return None if common is None else {"text": common}
    if entity == "newsletters":
        news = _current_newsletters(db)
        return None if news is None else {"newsletters": news}
    if entity == "no_fetch":
        listed = _current_no_fetch(db)
        return None if listed is None else {"sites": listed}
    raise EditError(f"tipo desconhecido: {entity}")


def undo(db: Session, audit_id: int, actor: str) -> str:
    """Volta um registro de importação/edição ao «antes», se nada mudou depois dele."""
    row = db.execute(text("SELECT action, target, details FROM audit_log WHERE id = :i"),
                     {"i": audit_id}).mappings().first()
    if row is None or row["action"] not in (IMPORT_ACTION, EDIT_ACTION, UNDO_ACTION):
        raise EditError("registro de configuração não encontrado")
    details = dict(row["details"] or {})
    before, after = details.get("before"), details.get("after")
    if before is None:
        raise EditError("é uma adição: para tirar, desligue o blog ou o feed")
    if details.get("via") == "policy_common":
        raise EditError("esta mudança veio da edição das regras comuns: desfaça aquele registro")
    entity, key = str(row["target"]).split(":", 1)
    if _current_value(db, entity, key) != after:
        raise EditError("o valor mudou depois deste registro; desfazer agora sobrescreveria a "
                        "mudança mais recente — desfaça primeiro a mais recente")
    if entity == "policy_common":
        _, owns = policies(db)
        _write_policy_common(db, before["text"])
        for site_key, own in owns.items():
            if own is None:
                continue
            site_before = site_values(db, site_key)
            site_after = {**site_before,
                          "config": {**site_before["config"],
                                     "policy": f"{before['text']}\n\n{own}"}}
            _save_site(db, site_before, site_after, actor, UNDO_ACTION,
                       {"via": "policy_common", "undo_of": audit_id})
    elif entity == "site":
        technical = [f for f in _changed_fields(dict(after or {}), before)
                     if f in TECHNICAL_SITE_COLUMNS]
        if technical:
            raise EditError("este registro mexe em colunas técnicas (" + ", ".join(technical)
                            + "): desfazer só pela linha de comando (YAML + make reimport-apply)")
        _update_site_portal(db, before)
    elif entity == "source":
        site_key, url = key.split(" ", 1)
        _write_source(db, site_key, url, before)
    elif entity == "no_fetch":
        _write_no_fetch(db, before["sites"])
    else:
        _write_newsletters(db, before["newsletters"])
    users.audit(db, actor, UNDO_ACTION, row["target"],
                {"kind": "update", "fields": details.get("fields", []), "before": after,
                 "after": before, "undo_of": audit_id})
    return str(row["target"])


# --- exportar para o git --------------------------------------------------------------------
def export_site_yaml(db: Session, site_key: str) -> str:
    import yaml

    values = site_values(db, site_key)
    data = {c: values[c] for c in SITE_COLUMNS}
    data.update({k: v for k, v in values["config"].items() if k != "policy"})
    header = (f"# ShortsBlog — site \"{site_key}\". Exportado do banco pelo portal (E18.3a);\n"
              "# sem segredos. Os comentários do arquivo original não vêm na exportação.\n")
    return header + yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=100)


def export_policy(db: Session, name: str) -> str:
    common, owns = policies(db)
    if name == "_comum":
        return common + "\n"
    own = owns.get(name)
    if own is None:
        raise EditError(f"política de {name} indisponível (blog desconhecido ou incoerente)")
    return own + "\n"


# --- feeds, newsletters de pauta e sem-leitura (E18.3a entrega 3) ----------------------------
def site_sources(db: Session, site_key: str) -> list[dict[str, Any]]:
    rows = db.execute(text(
        """SELECT feed_url, name, domain, source_type, tier, lang, usage_notes, enabled,
                  last_checked_at, last_status, last_items, fail_count
             FROM sources WHERE site_key = :s
            ORDER BY enabled DESC, tier, lower(name)"""), {"s": site_key}).mappings()
    return [dict(r) for r in rows]


def _audit_source(db: Session, actor: str, site_key: str, url: str, kind: str,
                  before: dict[str, Any] | None, after: dict[str, Any],
                  extra: dict[str, Any] | None = None) -> None:
    fields = ["*"] if before is None else [f for f in SOURCE_FIELDS
                                           if before.get(f) != after.get(f)]
    users.audit(db, actor, EDIT_ACTION, f"source:{site_key} {url}",
                {"kind": kind, "fields": fields, "before": before, "after": after,
                 **(extra or {})})


def add_source(db: Session, site_key: str, url: str, values: dict[str, Any], actor: str,
               check: dict[str, Any]) -> None:
    """Feed novo (já validado por `feedcheck`). Um feed desligado com o mesmo endereço volta."""
    site_values(db, site_key)
    have = _current_sources(db).get((site_key, url))
    if have is not None and have.get("enabled"):
        raise EditError("este feed já está ativo neste blog")
    after = {"name": values["name"], "domain": values.get("domain") or urlsplit(url).hostname,
             "source_type": values["source_type"], "tier": values["tier"],
             "lang": values["lang"], "usage_notes": values.get("usage_notes", ""),
             "enabled": True}
    before = None if have is None else {f: have.get(f) for f in SOURCE_FIELDS}
    _write_source(db, site_key, url, after)
    _audit_source(db, actor, site_key, url, "add" if before is None else "update", before,
                  after, {"check": check})


def edit_source(db: Session, site_key: str, url: str, values: dict[str, Any],
                actor: str, expected: str = "") -> list[str]:
    from app.services import feedcheck

    db.execute(text("SELECT 1 FROM sources WHERE site_key = :s AND feed_url = :u FOR UPDATE"),
               {"s": site_key, "u": url})
    have = _current_sources(db).get((site_key, url))
    if have is None:
        raise EditError("feed não encontrado neste blog")
    before = {f: have.get(f) for f in SOURCE_FIELDS}
    _check_version(before, expected, "o feed")
    after = {**before, **{k: v for k, v in values.items() if k in SOURCE_FIELDS}}
    if not str(after["name"]).strip():
        raise EditError("o nome do feed não pode ficar vazio")
    if after["tier"] not in feedcheck.TIERS:
        raise EditError(f"tier inválido: {after['tier']}")
    if after["lang"] not in feedcheck.LANGS:
        raise EditError(f"idioma inválido: {after['lang']}")
    if after["enabled"] and not before["enabled"] and under_no_feed(url):
        raise EditError("os termos deste site proíbem o uso comercial do RSS (rss: false)")
    fields = [f for f in SOURCE_FIELDS if before.get(f) != after.get(f)]
    if fields:
        _write_source(db, site_key, url, after)
        _audit_source(db, actor, site_key, url, "update", before, after)
    return fields


def under_no_feed(url: str) -> bool:
    from app.services.urls import under

    return under(terms.host_of(url), terms.no_feed_domains())


def newsletters(db: Session) -> list[dict[str, Any]]:
    return _current_newsletters(db) or []


def save_newsletters(db: Session, entries: list[dict[str, Any]], actor: str,
                     expected: str = "") -> bool:
    try:
        clean = pauta.validate_entries(entries, "tela")
    except pauta.NewsletterConfigError as exc:
        raise EditError(str(exc)) from None
    known = set(db.execute(text("SELECT site_key FROM sites")).scalars())
    unknown = sorted({e["site_key"] for e in clean} - known)
    if unknown:
        raise EditError(f"blog desconhecido: {', '.join(unknown)}")
    domains = [e["sender_domain"] for e in clean]
    if len(set(domains)) != len(domains):
        raise EditError("remetente (sender_domain) repetido")
    _lock_setting(db, pauta.SETTING_KEY)
    before = _current_newsletters(db)
    _check_version(before or [], expected, "a lista de newsletters")
    if before == clean:
        return False
    _write_newsletters(db, clean)
    users.audit(db, actor, EDIT_ACTION, "newsletters:newsletters",
                {"kind": "update", "fields": ["newsletters"],
                 "before": None if before is None else {"newsletters": before},
                 "after": {"newsletters": clean},
                 "note": _list_note(before or [], clean, "sender_domain")})
    return True


def no_fetch(db: Session) -> list[dict[str, Any]]:
    stored = _current_no_fetch(db)
    return stored if stored is not None else [dict(e) for e in terms.load_file()]


def save_no_fetch(db: Session, entries: list[dict[str, Any]], actor: str,
                  justification: str = "", expected: str = "") -> bool:
    try:
        clean = [dict(e) for e in terms.validate_entries(entries, "tela")]
    except terms.TermsConfigError as exc:
        raise EditError(str(exc)) from None
    _lock_setting(db, terms.SETTING_KEY)
    _check_version(no_fetch(db), expected, "a lista de sites sem leitura")
    before = _current_no_fetch(db)
    if before == clean:
        return False
    removed = sorted({e["domain"] for e in before or []} - {e["domain"] for e in clean})
    if removed and len(justification.strip()) < 10:
        raise EditError("tirar um site da lista pede a justificativa (licença ou leitura nova dos "
                        "termos), com pelo menos 10 caracteres")
    _write_no_fetch(db, clean)
    users.audit(db, actor, EDIT_ACTION, "no_fetch:lista",
                {"kind": "update", "fields": ["sites"],
                 "before": None if before is None else {"sites": before},
                 "after": {"sites": clean},
                 "note": _list_note(before or [], clean, "domain"),
                 "justification": justification.strip()[:500]})
    return True


def export_feeds_yaml(db: Session, site_key: str) -> str:
    import yaml

    site_values(db, site_key)
    feeds = []
    for row in site_sources(db, site_key):
        if not row["enabled"]:
            continue
        item = {"name": row["name"], "url": row["feed_url"], "tier": row["tier"],
                "lang": row["lang"]}
        if row["source_type"] != "rss":
            item["kind"] = row["source_type"]
        if row["domain"] and row["domain"] != urlsplit(row["feed_url"]).hostname:
            item["domain"] = row["domain"]
        if row["usage_notes"]:
            item["notes"] = row["usage_notes"]
        feeds.append(item)
    header = (f"# Feeds do blog «{site_key}» — exportados do banco pelo portal (E18.3a).\n"
              "# Só os ativos; sem os comentários do arquivo original.\n")
    return header + yaml.safe_dump({"site_key": site_key, "feeds": feeds}, allow_unicode=True,
                                   sort_keys=False, width=100)


def export_newsletters_yaml(db: Session) -> str:
    import yaml

    return ("# Newsletters de pauta (D-094) — exportadas do banco pelo portal (E18.3a).\n"
            + yaml.safe_dump({"newsletters": newsletters(db)}, allow_unicode=True,
                             sort_keys=False, width=100))


def export_no_fetch_yaml(db: Session) -> str:
    import yaml

    return ("# Sites sem leitura (D-070/D-071) — exportados do banco pelo portal (E18.3a).\n"
            "# Ver o cabeçalho do arquivo original para as regras.\n"
            + yaml.safe_dump({"sites": no_fetch(db)}, allow_unicode=True, sort_keys=False,
                             width=120))
