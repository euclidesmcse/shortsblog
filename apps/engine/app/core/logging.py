"""Logging estruturado em JSON — pronto para o Loki/Promtail e para `docker logs`."""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

from app.core.config import settings

_LINK_TOKEN = re.compile(r"(/a/)[^\s\"'?#/]+")


def hide_link_tokens(value: str) -> str:
    """O token de um link de decisão (`/a/<token>`) nunca vai para os logs (E10, revisão)."""
    return _LINK_TOKEN.sub(r"\1<oculto>", value)


class HideLinkTokens(logging.Filter):
    """Mascara `/a/<token>` na mensagem e nos argumentos (inclui o log de acesso do uvicorn)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = hide_link_tokens(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(
                hide_link_tokens(a) if isinstance(a, str) else a for a in record.args
            )
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Campos extras passados via logger.info("...", extra={"ctx": {...}})
        ctx = getattr(record, "ctx", None)
        if isinstance(ctx, dict):
            payload.update(ctx)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(HideLinkTokens())
    # O uvicorn tem o seu próprio handler no log de acesso: o filtro vai no logger.
    logging.getLogger("uvicorn.access").addFilter(HideLinkTokens())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())
    # Bibliotecas muito verbosas
    for noisy in ("httpx", "httpcore", "urllib3", "openai", "anthropic", "apscheduler"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
