"""E6.2 — fetcher seguro (anti-SSRF): só https, só IPs públicos, também depois de redirecionar."""
from __future__ import annotations

import httpx
import pytest

from app.core.config import settings
from app.services import fetcher
from app.services.fetcher import FetchError

PUBLIC = "93.184.216.34"


class Net:
    """Internet simulada: nomes → IPs, e respostas por (host, caminho)."""

    def __init__(self) -> None:
        self.dns: dict[str, list[str]] = {}
        self.pages: dict[tuple[str, str], httpx.Response] = {}
        self.requests: list[tuple[str, str, str, str]] = []   # (ip, host, sni, caminho)

    def add(self, host: str, path: str = "/", *, ip: str | list[str] = PUBLIC,
            status: int = 200, body: bytes = b"ok", headers: dict | None = None) -> None:
        self.dns[host] = [ip] if isinstance(ip, str) else ip
        self.pages[(host, path)] = httpx.Response(status, content=body, headers=headers or {})

    def handle(self, request: httpx.Request) -> httpx.Response:
        host = request.headers["host"]
        self.requests.append((request.url.host, host,
                              request.extensions.get("sni_hostname", ""), request.url.path))
        if request.url.path == "/robots.txt" and (host, "/robots.txt") not in self.pages:
            return httpx.Response(404)
        return self.pages.get((host, request.url.raw_path.decode()), httpx.Response(404))


@pytest.fixture
def net(monkeypatch) -> Net:
    world = Net()

    def resolve(host: str) -> list[str]:
        if host not in world.dns:
            raise OSError("nome desconhecido")
        return world.dns[host]

    monkeypatch.setattr(fetcher, "resolve", resolve)
    monkeypatch.setattr(fetcher, "_transport", httpx.MockTransport(world.handle))
    monkeypatch.setattr(fetcher, "_robots", {})
    return world


def kind_of(url: str, **kwargs) -> str:
    with pytest.raises(FetchError) as info:
        fetcher.fetch(url, **kwargs)
    return info.value.kind


# --- Caminho normal --------------------------------------------------------------------------


def test_liga_ao_ip_verificado_com_o_nome_no_host_e_no_sni(net):
    net.add("fonte.test", "/noticia?id=1", body=b"<html>ol\xc3\xa1</html>",
            headers={"content-type": "text/html; charset=utf-8"})
    page = fetcher.fetch("https://fonte.test/noticia?id=1")
    assert (page.status, page.text) == (200, "<html>olá</html>")
    # O pedido vai para o IP que foi verificado, não para o nome (anti DNS rebinding).
    assert net.requests[-1] == (PUBLIC, "fonte.test", "fonte.test", "/noticia")
    assert page.url == "https://fonte.test/noticia?id=1"


def test_identifica_se_como_shortsblogbot(net, monkeypatch):
    seen = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen[request.url.path] = request.headers["user-agent"]
        return httpx.Response(200, content=b"x")

    net.dns["fonte.test"] = [PUBLIC]
    monkeypatch.setattr(fetcher, "_transport", httpx.MockTransport(handle))
    fetcher.fetch("https://fonte.test/")
    assert all(ua.startswith("ShortsBlogBot/") for ua in seen.values())
    assert settings.http_user_agent.startswith("ShortsBlogBot/")


# --- Endereços recusados ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("http://169.254.169.254/", "not_https"),                # metadados, sem https
        ("http://fonte.test/", "not_https"),
        ("ftp://fonte.test/x", "not_https"),
        ("file:///etc/passwd", "not_https"),
        ("https://169.254.169.254/latest/meta-data/", "blocked_address"),
        ("https://10.0.0.1/", "blocked_address"),
        ("https://127.0.0.1/", "blocked_address"),
        ("https://192.168.1.10/", "blocked_address"),
        ("https://172.16.5.4/", "blocked_address"),
        ("https://100.64.0.1/", "blocked_address"),              # CGNAT
        ("https://0.0.0.0/", "blocked_address"),
        ("https://[::1]/", "blocked_address"),
        ("https://[fd00::1]/", "blocked_address"),               # IPv6 privado
        ("https://[fe80::1]/", "blocked_address"),               # IPv6 link-local
        ("https://[::ffff:10.0.0.1]/", "blocked_address"),       # IPv4 mapeado em IPv6
        ("https://224.0.0.1/", "blocked_address"),               # multicast
        ("https://fonte.test:8443/", "bad_port"),
        ("https://utilizador:senha@fonte.test/", "credentials_in_url"),
        ("https:///sem-host", "no_host"),
        ("https://nao-existe.test/", "dns_error"),
    ],
)
def test_enderecos_recusados_sem_fazer_nenhum_pedido(net, url, kind):
    net.add("fonte.test")
    assert kind_of(url) == kind
    assert net.requests == []


@pytest.mark.parametrize("ip", ["10.0.0.5", "127.0.0.1", "169.254.169.254", "192.168.0.1",
                                "172.20.0.2", "fd12::1"])
def test_nome_publico_que_resolve_para_ip_interno(net, ip):
    net.add("interno.test", ip=ip)
    assert kind_of("https://interno.test/") == "blocked_address"
    assert net.requests == []


def test_basta_um_ip_interno_entre_as_respostas_do_dns(net):
    net.add("misto.test", ip=[PUBLIC, "10.0.0.5"])
    assert kind_of("https://misto.test/") == "blocked_address"
    assert net.requests == []


def test_nomes_dos_containers_da_rede_interna_sao_recusados(net):
    # É o que um feed malicioso tentaria: os serviços do próprio motor.
    for name, ip in (("sb-engine-postgres", "172.18.0.3"), ("sb-ai-gateway", "172.18.0.9"),
                     ("sb-engine-redis", "172.18.0.4")):
        net.add(name, ip=ip)
        assert kind_of(f"https://{name}/") == "blocked_address"
    assert net.requests == []


# --- Redirecionamentos -----------------------------------------------------------------------


def test_segue_redirecionamento_para_destino_publico(net):
    net.add("fonte.test", "/a", status=301, headers={"location": "https://outra.test/b"})
    net.add("outra.test", "/b", body=b"destino")
    page = fetcher.fetch("https://fonte.test/a")
    assert (page.url, page.body) == ("https://outra.test/b", b"destino")


@pytest.mark.parametrize(
    ("location", "kind"),
    [("https://10.0.0.1/admin", "blocked_address"),
     ("https://169.254.169.254/latest/meta-data/", "blocked_address"),
     ("http://outra.test/b", "not_https"),
     ("https://interno.test/", "blocked_address")],
)
def test_redirecionamento_para_destino_proibido(net, location, kind):
    net.add("fonte.test", "/a", status=302, headers={"location": location})
    net.add("interno.test", ip="10.1.2.3")
    assert kind_of("https://fonte.test/a") == kind
    # Só o primeiro pedido (e o robots.txt) saiu; ao destino proibido nunca se ligou.
    assert {r[1] for r in net.requests} == {"fonte.test"}


def test_demasiados_redirecionamentos(net):
    net.add("fonte.test", "/a", status=302, headers={"location": "https://fonte.test/a"})
    assert kind_of("https://fonte.test/a") == "too_many_redirects"


def test_redirecionamento_relativo_e_sem_destino(net):
    net.add("fonte.test", "/a", status=302, headers={"location": "/b"})
    net.add("fonte.test", "/b", body=b"relativo")
    assert fetcher.fetch("https://fonte.test/a").body == b"relativo"
    net.add("fonte.test", "/c", status=302)
    assert kind_of("https://fonte.test/c") == "bad_redirect"


# --- Limites ------------------------------------------------------------------------------------


def test_limite_de_tamanho(net):
    net.add("fonte.test", "/grande", body=b"x" * 5000)
    assert kind_of("https://fonte.test/grande", max_bytes=1000) == "too_large"
    assert len(fetcher.fetch("https://fonte.test/grande", max_bytes=5000).body) == 5000


def test_teto_proprio_para_pdf(net):
    # Relatórios oficiais em PDF são maiores que páginas HTML (5/11): teto próprio,
    # que só vale quando a resposta é mesmo application/pdf.
    net.add("fonte.test", "/doc.pdf", body=b"%" * 5000,
            headers={"content-type": "application/pdf"})
    assert kind_of("https://fonte.test/doc.pdf", max_bytes=1000) == "too_large"
    page = fetcher.fetch("https://fonte.test/doc.pdf", max_bytes=1000, max_bytes_pdf=8000)
    assert len(page.body) == 5000
    net.add("fonte.test", "/pagina", body=b"x" * 5000,
            headers={"content-type": "text/html"})
    assert kind_of("https://fonte.test/pagina", max_bytes=1000,
                   max_bytes_pdf=8000) == "too_large"


def test_tempo_esgotado_e_erro_de_rede(net, monkeypatch):
    net.dns["fonte.test"] = [PUBLIC]

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lento", request=request)

    monkeypatch.setattr(fetcher, "_transport", httpx.MockTransport(slow))
    assert kind_of("https://fonte.test/", respect_robots=False) == "timeout"

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("recusado", request=request)

    monkeypatch.setattr(fetcher, "_transport", httpx.MockTransport(broken))
    assert kind_of("https://fonte.test/", respect_robots=False) == "network_error"
    assert kind_of("https://fonte.test/", respect_robots=False, timeout_s=0) == "timeout"


# --- robots.txt -----------------------------------------------------------------------------------


def test_respeita_o_robots_txt(net):
    net.add("fonte.test", "/robots.txt", body=b"User-agent: *\nDisallow: /privado/\n")
    net.add("fonte.test", "/publico", body=b"ok")
    net.add("fonte.test", "/privado/x", body=b"segredo")
    assert fetcher.fetch("https://fonte.test/publico").body == b"ok"
    assert kind_of("https://fonte.test/privado/x") == "robots_disallowed"
    # O robots.txt é pedido uma vez por sítio e fica em cache.
    assert sum(r[3] == "/robots.txt" for r in net.requests) == 1


def test_regra_dirigida_ao_nosso_robo(net):
    net.add("fonte.test", "/robots.txt",
            body=b"User-agent: ShortsBlogBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
    net.add("fonte.test", "/a")
    assert kind_of("https://fonte.test/a") == "robots_disallowed"


def test_pagina_para_a_ia_respeita_tambem_as_regras_dos_robos_de_ia(net):
    # D-070: o sítio deixa o nosso robô, mas não quer a página numa IA.
    net.add("fonte.test", "/robots.txt",
            body=b"User-agent: ClaudeBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
    net.add("fonte.test", "/a", body=b"ok")
    assert fetcher.fetch("https://fonte.test/a").body == b"ok"            # leitura comum
    assert kind_of("https://fonte.test/a", ai_agents=("ClaudeBot", "GPTBot")) == "ai_disallowed"
    net.add("livre.test", "/robots.txt", body=b"User-agent: GPTBot\nDisallow: /privado\n")
    net.add("livre.test", "/a", body=b"ok")
    assert fetcher.fetch("https://livre.test/a", ai_agents=("ClaudeBot", "GPTBot")).body == b"ok"


def test_robots_ausente_permite_e_proibido_bloqueia(net):
    net.add("livre.test", "/a")                                  # robots.txt → 404
    assert fetcher.fetch("https://livre.test/a").status == 200
    net.add("fechado.test", "/robots.txt", status=403)
    net.add("fechado.test", "/a")
    assert kind_of("https://fechado.test/a") == "robots_disallowed"


def test_destino_do_redirecionamento_tambem_passa_pelo_robots(net):
    net.add("fonte.test", "/a", status=302, headers={"location": "https://outra.test/privado"})
    net.add("outra.test", "/robots.txt", body=b"User-agent: *\nDisallow: /privado\n")
    net.add("outra.test", "/privado", body=b"x")
    assert kind_of("https://fonte.test/a") == "robots_disallowed"


def test_destino_do_redirecionamento_bloqueado_so_para_ia_e_ai_disallowed(net):
    """Regressão (revisão E14): o destino bloqueado apenas para os robôs de IA contava como
    `robots_disallowed` e sumia do contador «só de pauta pelos termos de uso» (D-070)."""
    net.add("fonte.test", "/a", status=302, headers={"location": "https://outra.test/x"})
    net.add("outra.test", "/robots.txt",
            body=b"User-agent: GPTBot\nDisallow: /\n\nUser-agent: *\nAllow: /\n")
    net.add("outra.test", "/x", body=b"ok")
    assert kind_of("https://fonte.test/a", ai_agents=("ClaudeBot", "GPTBot")) == "ai_disallowed"
    # Sem a página ir para uma IA, o mesmo destino é permitido.
    assert fetcher.fetch("https://fonte.test/a").body == b"ok"


def test_is_public_ip():
    assert fetcher.is_public_ip("8.8.8.8") and fetcher.is_public_ip("2606:4700::1111")
    for raw in ("10.0.0.1", "127.0.0.1", "169.254.169.254", "::1", "fc00::1", "192.0.2.1",
                "não-é-ip", ""):
        assert not fetcher.is_public_ip(raw), raw


# --- Achados da revisão da E6 (ronda 1) --------------------------------------------------------


@pytest.mark.parametrize("raw", ["64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe", "::7f00:1",
                                 "64:ff9b:1::a00:1"])
def test_ipv4_embutido_em_ipv6_nunca_e_publico(raw):
    assert not fetcher.is_public_ip(raw)


def test_redirecionamento_malformado_e_um_erro_do_fetcher(net):
    net.add("fonte.test", "/a", status=302, headers={"location": "https://[bad/"})
    assert kind_of("https://fonte.test/a") == "bad_redirect"
    assert kind_of("https://[bad/") == "bad_url"


def test_robots_com_erro_do_servidor_ou_inalcancavel_proibe(net):
    net.add("avariado.test", "/a")
    net.add("avariado.test", "/robots.txt", status=503)
    assert kind_of("https://avariado.test/a") == "robots_disallowed"
    net.add("mudo.test", "/a")
    net.add("mudo.test", "/robots.txt", status=302, headers={"location": "https://10.0.0.1/r"})
    assert kind_of("https://mudo.test/a") == "robots_disallowed"
    assert fetcher.fetch("https://mudo.test/a", respect_robots=False).status == 200


def test_robots_com_429_proibe_ate_nova_tentativa(net):
    net.add("cheio.test", "/a")
    net.add("cheio.test", "/robots.txt", status=429)
    assert kind_of("https://cheio.test/a") == "robots_disallowed"

