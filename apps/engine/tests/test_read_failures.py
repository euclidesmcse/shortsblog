"""Falhas de leitura das páginas-fonte por sítio, no resumo diário (22/11)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx

from app.core.config import settings
from app.services import article, fetcher, read_failures
from app.services.notify import digests

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
LONG = "<p>" + "Texto longo da notícia com dados e números verificáveis. " * 20 + "</p>"


def test_conta_por_sitio_e_motivo_e_so_mostra_o_que_passa_do_minimo(monkeypatch):
    monkeypatch.setattr(settings, "read_fail_alert_min", 3)
    for _ in range(3):
        read_failures.failed("jama.test", "http_403", NOW)
    read_failures.failed("jama.test", "no_text", NOW - timedelta(days=2))
    read_failures.read_ok("jama.test", NOW)
    read_failures.failed("raro.test", "timeout", NOW)                   # abaixo do mínimo
    read_failures.failed("velho.test", "http_403", NOW - timedelta(days=9))
    read_failures.failed("velho.test", "http_403", NOW - timedelta(days=9))
    read_failures.failed("velho.test", "http_403", NOW - timedelta(days=9))  # fora dos 7 dias
    rows = read_failures.summary(NOW)
    assert rows == [{"site": "jama.test", "fails": 4, "ok": 1,
                     "kinds": {"http_403": 3, "no_text": 1}}]
    html = "".join(digests._read_failures_html(rows))
    assert "Sítios que não se deixam ler" in html and "jama.test" in html
    assert "403 (bloqueio) ×3" in html and "paywall" in html
    assert digests._read_failures_html([]) == []


def test_a_pesquisa_regista_falhas_e_leituras_certas(monkeypatch):
    pages = {"https://bloqueado.test/a": (403, "text/html", "<p>proibido</p>"),
             "https://vazio.test/b": (200, "text/html", "<p>curto</p>"),
             "https://bom.test/c": (200, "text/html", f"<html><body><article>{LONG}"
                                                       "</article></body></html>")}

    def handler(request: httpx.Request) -> httpx.Response:
        url = f"https://{request.headers['host']}{request.url.path}"
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        status, ctype, body = pages[url]
        return httpx.Response(status, headers={"content-type": ctype}, text=body)

    monkeypatch.setattr(fetcher, "_transport", httpx.MockTransport(handler))
    monkeypatch.setattr(fetcher, "check_url", lambda url: (httpx.URL(url).host, "203.0.113.9"))
    monkeypatch.setattr(settings, "read_fail_alert_min", 1)
    got = article.read_sources(list(pages), NOW)
    assert [s.url for s in got] == ["https://bom.test/c"]
    rows = {r["site"]: r for r in read_failures.summary(NOW)}
    assert rows["bloqueado.test"]["kinds"] == {"http_403": 1}
    assert rows["vazio.test"]["kinds"] == {"no_text": 1}
    assert "bom.test" not in rows


def test_recusa_pelo_robots_de_ia_nao_entra_no_alerta_e_vira_linha_informativa(monkeypatch):
    """23/11: a dw.com aparecia toda semana no alerta, mas o robots.txt dela proíbe robôs de IA
    e o feed é só pauta de propósito (D-070) — não há nada a corrigir."""
    monkeypatch.setattr(settings, "read_fail_alert_min", 3)
    for _ in range(7):
        read_failures.failed("dw.com", "ai_disallowed", NOW)
    for _ in range(3):
        read_failures.failed("un.org", "no_text", NOW)
    read_failures.failed("un.org", "ai_disallowed", NOW)
    rows = read_failures.summary(NOW)
    assert rows == [{"site": "un.org", "fails": 3, "ok": 0, "kinds": {"no_text": 3}}]
    designed = read_failures.by_design(NOW)
    assert designed == [("dw.com", 7), ("un.org", 1)]
    html = "".join(digests._read_failures_html(rows, designed))
    assert "Sítios que não se deixam ler" in html and "un.org" in html
    assert "Só pauta, como previsto" in html and "dw.com ×7" in html
    only = "".join(digests._read_failures_html([], designed))
    assert "Sítios que não se deixam ler" not in only and "dw.com ×7" in only
