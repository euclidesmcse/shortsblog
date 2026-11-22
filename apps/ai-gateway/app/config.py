"""Configuração do gateway (variáveis de ambiente, prefixo `AIGW_`)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AIGW_")

    # Diretório de trabalho das CLIs: vazio e descartável (tmpfs no compose).
    workdir: str = "/work"

    default_timeout_s: float = 300
    max_timeout_s: float = 900
    # Um argumento de linha de comando no Linux não pode passar de 128 KiB.
    max_prompt_bytes: int = 400_000

    # Onde o Codex grava as imagens que gera (uma pasta por pedido); o gateway lê e apaga.
    codex_image_dir: str = "/state/codex/generated_images"
    max_image_prompt_bytes: int = 4_000

    # Chamadas simultâneas por provedor (R19: a cota é a do Euclides).
    claude_concurrency: int = 2
    codex_concurrency: int = 2
    agy_concurrency: int = 1

    # Provedores por API key: chamadas simultâneas por nome de provedor.
    api_concurrency: int = 4
    # Só HTTPS: a API key nunca viaja em claro para fora. Os testes desligam isto.
    api_require_https: bool = True

    # Modelo por omissão; vazio = o que a CLI escolher.
    claude_model: str = ""
    codex_model: str = ""
    # O agy também serve modelos Claude; fixar Gemini mantém a regra
    # "redator e revisor de famílias diferentes" (ADR 0003).
    agy_model: str = "gemini-3.1-pro-high"

    # R31: pastas de estado das CLIs (os volumes) e a limpeza periódica delas.
    claude_state_dir: str = "/state/claude"
    codex_state_dir: str = "/state/codex"
    agy_state_dir: str = "/home/sb/.gemini"
    housekeeping_enabled: bool = True
    housekeeping_interval_h: float = 24
    housekeeping_first_delay_s: float = 600


settings = Settings()
