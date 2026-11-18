"""D-097 — purge do cache do Cloudflare ao publicar (automatiza a D-048)."""
from __future__ import annotations

import httpx
import pytest

from app.core.config import settings
from app.services import cloudflare

TOKEN = "cf-token-de-teste"
ZONE = "7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a7a"


class Api:
    """API do Cloudflare simulada: devolve a zona e guarda os purges."""

    def __init__(self) -> None:
        self.purges: list[list[str]] = []
        self.zone_calls = 0
        self.fail = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == f"Bearer {TOKEN}"
        if self.fail:
            return httpx.Response(500, json={"success": False})
        if request.url.path == "/client/v4/zones":
            self.zone_calls += 1
            assert dict(request.url.params) == {"name": "shortsblog.com"}
            return httpx.Response(200, json={"result": [{"id": ZONE}]})
        assert request.url.path == f"/client/v4/zones/{ZONE}/purge_cache"
        import json
        self.purges.append(json.loads(request.content)["files"])
        return httpx.Response(200, json={"success": True})


@pytest.fixture
def cf(monkeypatch) -> Api:
    api = Api()
    monkeypatch.setattr(settings, "cloudflare_purge_token", TOKEN)
    monkeypatch.setattr(cloudflare, "_transport", httpx.MockTransport(api))
    return api


def test_desligado_por_omissao_nao_faz_nada(monkeypatch):
    monkeypatch.setattr(settings, "cloudflare_purge_token", "")
    assert cloudflare.enabled() is False
    assert cloudflare.purge_urls(["https://cloud.shortsblog.com/"]) is False


def test_resolve_a_zona_uma_vez_e_purga_em_lotes(cf):
    urls = [f"https://cloud.shortsblog.com/p{i}/" for i in range(65)]
    assert cloudflare.purge_urls(urls) is True
    assert [len(batch) for batch in cf.purges] == [30, 30, 5]
    assert cf.purges[0][0] == "https://cloud.shortsblog.com/p0/"
    # A zona ficou no Redis: a chamada seguinte não consulta /zones de novo.
    cf.purges.clear()
    assert cloudflare.purge_urls(["https://cloud.shortsblog.com/"]) is True
    assert cf.purges == [["https://cloud.shortsblog.com/"]] and cf.zone_calls == 1


def test_zone_id_configurado_dispensa_a_consulta(cf, monkeypatch):
    monkeypatch.setattr(settings, "cloudflare_zone_id", ZONE)
    assert cloudflare.purge_urls(["https://ia.shortsblog.com/"]) is True
    assert cf.purges == [["https://ia.shortsblog.com/"]]


def test_falha_da_api_nunca_levanta(cf):
    cf.fail = True
    assert cloudflare.purge_urls(["https://cloud.shortsblog.com/"]) is False
