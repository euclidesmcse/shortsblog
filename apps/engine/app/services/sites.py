"""Sites do ShortsBlog (E4.2).

A fonte de verdade é `content-config/sites/<site_key>.yaml` (versionado, sem
segredos). `sync()` copia para a tabela `sites`, que é o que o resto do motor
lê. Um site que deixe de ter ficheiro não é apagado (há jobs e histórico
ligados a ele): fica desligado.
"""
from __future__ import annotations

import json
import pathlib
from collections.abc import Iterator
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

LANGUAGES = ("pt-BR", "en", "es", "zh-CN")
# Campos que vão para colunas próprias; o resto do ficheiro fica em `config` (JSONB).
_COLUMNS = (
    "site_key", "name", "base_url", "wordpress_api_url", "hosting_group", "timezone",
    "language", "languages", "enabled", "daily_target", "slot_offset_minutes",
    "seo_plugin", "taxonomy_version", "editorial_policy_version",
)
# Nomes que denunciam um segredo metido no ficheiro por engano.
_SECRET_HINTS = ("password", "passwd", "secret", "token", "api_key", "apikey", "credential")


def _keys(value: Any) -> Iterator[str]:
    """Todas as chaves de um valor YAML, a qualquer profundidade."""
    if isinstance(value, dict):
        for key, inner in value.items():
            yield str(key)
            yield from _keys(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _keys(inner)


class SiteConfigError(ValueError):
    """Ficheiro de site inválido (mensagem com o nome do ficheiro)."""


class SiteConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    site_key: str = Field(pattern=r"^[a-z][a-z0-9-]{1,30}$")
    name: str = Field(min_length=1)
    base_url: str
    wordpress_api_url: str
    hosting_group: str = "default"
    timezone: str = "America/Sao_Paulo"
    language: str = "pt-BR"
    languages: list[str] = list(LANGUAGES)
    enabled: bool = False
    daily_target: int = Field(default=5, ge=0, le=5)
    slot_offset_minutes: int = Field(default=0, ge=0, le=59)
    seo_plugin: str = ""
    taxonomy_version: str = ""
    editorial_policy_version: str = ""

    @field_validator("base_url", "wordpress_api_url")
    @classmethod
    def _https(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("tem de começar por https://")
        return value.rstrip("/")

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"fuso horário desconhecido: {value}") from None
        return value

    @model_validator(mode="after")
    def _languages(self) -> SiteConfig:
        unknown = [lang for lang in self.languages if lang not in LANGUAGES]
        if unknown:
            raise ValueError(f"idiomas não suportados: {', '.join(unknown)}")
        if len(set(self.languages)) != len(self.languages):
            raise ValueError("idiomas repetidos")
        if self.language not in self.languages:
            raise ValueError("o idioma principal tem de estar em languages")
        return self

    @model_validator(mode="after")
    def _no_secrets(self) -> SiteConfig:
        for key in _keys(self.model_extra or {}):
            if any(hint in key.lower() for hint in _SECRET_HINTS):
                raise ValueError(f"'{key}' parece um segredo: não pode estar no ficheiro do site")
        return self

    def extra_config(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


def load_all(directory: str | pathlib.Path | None = None) -> list[SiteConfig]:
    """Lê e valida todos os ficheiros de site. Falha no primeiro inválido."""
    root = pathlib.Path(directory or settings.sites_config_dir)
    if not root.is_dir():
        raise SiteConfigError(f"diretório de sites não encontrado: {root}")
    sites: list[SiteConfig] = []
    for path in sorted(root.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("o ficheiro tem de ser um mapa YAML")
            site = SiteConfig(**data)
        except (yaml.YAMLError, ValueError) as exc:
            raise SiteConfigError(f"{path.name}: {exc}") from exc
        if site.site_key != path.stem:
            raise SiteConfigError(
                f"{path.name}: site_key '{site.site_key}' diferente do nome do ficheiro")
        sites.append(site)

    offsets = [s.slot_offset_minutes for s in sites]
    if len(set(offsets)) != len(offsets):
        raise SiteConfigError("dois sites têm o mesmo slot_offset_minutes")
    return sites


def load_policy(site_key: str, directory: str | pathlib.Path | None = None) -> str:
    """Política editorial do blog: as regras comuns seguidas das do próprio blog (E6.4)."""
    root = pathlib.Path(directory or settings.policies_config_dir)
    parts = []
    for name in ("_comum.md", f"{site_key}.md"):
        path = root / name
        if not path.is_file():
            raise SiteConfigError(f"política editorial em falta: {path.name}")
        parts.append(path.read_text(encoding="utf-8").strip())
    return "\n\n".join(parts)


def sync(db: Session, directory: str | pathlib.Path | None = None,
         policies: str | pathlib.Path | None = None) -> list[str]:
    """Copia os ficheiros para a tabela `sites`. Devolve as chaves sincronizadas.

    A política editorial de cada blog vai em `config.policy`: os workers leem-na da
    base e não precisam de ter os ficheiros montados.
    """
    sites = load_all(directory)
    for site in sites:
        values = {name: getattr(site, name) for name in _COLUMNS}
        extra = site.extra_config()
        if policies is not None or directory is None:
            extra["policy"] = load_policy(site.site_key, policies)
        values["config"] = json.dumps(extra, ensure_ascii=False)
        assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in (*_COLUMNS[1:], "config"))
        db.execute(
            text(f"""INSERT INTO sites ({', '.join(_COLUMNS)}, config)
                     VALUES ({', '.join(':' + c for c in _COLUMNS)}, CAST(:config AS JSONB))
                     ON CONFLICT (site_key) DO UPDATE
                        SET {assignments}, updated_at = now()"""),  # noqa: S608 — só nomes fixos
            values,
        )
    keys = [s.site_key for s in sites]
    orphaned = db.execute(
        text("""UPDATE sites SET enabled = false, updated_at = now()
                 WHERE enabled AND NOT (site_key = ANY(:keys)) RETURNING site_key"""),
        {"keys": keys},
    ).scalars().all()
    if orphaned:
        log.warning("Sites sem ficheiro de configuração desligados: %s", ", ".join(orphaned))
    return keys


def get(db: Session, site_key: str) -> dict[str, Any] | None:
    row = db.execute(text("SELECT * FROM sites WHERE site_key = :k"),
                     {"k": site_key}).mappings().first()
    return dict(row) if row else None


def enabled(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(
        text("SELECT * FROM sites WHERE enabled ORDER BY slot_offset_minutes, site_key")
    ).mappings().all()
    return [dict(row) for row in rows]
