"""O token dos links de decisão não aparece nos logs (revisão da E10)."""

from __future__ import annotations

import io
import logging

from app.core import logging as app_logging


def capture(logger_name: str) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(app_logging.JsonFormatter())
    handler.addFilter(app_logging.HideLinkTokens())
    logger = logging.getLogger(logger_name)
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    return logger, stream


def test_mascara_o_token_na_mensagem_e_nos_argumentos():
    logger, stream = capture("teste.painel")
    logger.warning("Painel: acesso negado em %s (%s)", "/a/12.1791263002.AbCd-_xyz", "sem token")
    logger.info("pedido /a/99.1.zzz?x=1 terminou")
    out = stream.getvalue()
    assert "AbCd-_xyz" not in out and "99.1.zzz" not in out
    assert "/a/<oculto>" in out and "sem token" in out and "?x=1" in out


def test_log_de_acesso_do_uvicorn_tambem():
    app_logging.setup_logging()  # põe o filtro no logger `uvicorn.access`
    access = logging.getLogger("uvicorn.access")
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    access.addHandler(handler)
    access.setLevel(logging.INFO)
    access.info('%s - "%s %s HTTP/%s" %d', "10.0.0.1:5", "GET", "/a/7.2.segredo", "1.1", 200)
    access.removeHandler(handler)
    assert "segredo" not in stream.getvalue() and "/a/<oculto>" in stream.getvalue()


def test_nao_mexe_no_que_nao_e_link():
    assert app_logging.hide_link_tokens(
        "/api/sched/dispatch /dashboard/review/3 /media/a.webp"
    ) == ("/api/sched/dispatch /dashboard/review/3 /media/a.webp")
