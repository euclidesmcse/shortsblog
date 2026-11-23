"""Fetcher seguro para conteúdo externo (E6.2, spec §11.2).

Tudo o que o motor vai buscar à internet por causa de conteúdo (feeds, artigos-
fonte, páginas de corroboração) passa por aqui. Um endereço vindo de um feed é
um dado não confiável: pode apontar para dentro da nossa rede.

Garantias:
- só `https`, só porta 443, sem credenciais no endereço;
- o nome é resolvido e **todos** os IPs têm de ser públicos: nada de redes
  privadas, loopback, link-local (inclui os metadados `169.254.169.254`),
  CGNAT, multicast ou reservados — em IPv4 e IPv6;
- a ligação é feita **ao IP que foi verificado** (com o nome no SNI e no `Host`),
  por isso um DNS que mude de resposta entre a verificação e o pedido não serve
  de nada;
- cada redirecionamento passa pelas mesmas verificações, e há um máximo — inclusive o
  `robots.txt` de cada salto intermédio (R42), não só o do destino final;
- limite de tamanho (lido em blocos) e de tempo total;
- identifica-se sempre como `ShortsBlogBot` e respeita o `robots.txt` (um `robots.txt`
  maior que o teto é lido até ao teto, como os motores de busca fazem — R42);
- espaça os pedidos ao mesmo servidor (`fetch_host_interval_s`, R42).
"""
from __future__ import annotations

import ipaddress
import socket
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

import certifi
import httpx

from app.core.config import settings
from app.core.logging import get_logger
from app.services.robots import DISALLOW_ALL, Robots

log = get_logger(__name__)

MAX_REDIRECTS = 5
ROBOTS_TTL_S = 3600
ROBOTS_MAX_BYTES = 512 * 1024

# Só os testes o substituem (httpx.MockTransport).
_transport: httpx.BaseTransport | None = None


class _NoAlpnContext(ssl.SSLContext):
    """TLS sem ALPN, como o `urllib`. O httpcore anuncia sempre `http/1.1` no ALPN, e há
    servidores que respondem diferente a isso: a ONU News (Fastly) dava 404 a todo o pedido
    com ALPN `http/1.1` e 200 sem ALPN — os 3 feeds de saúde da ONU (inglês, português,
    espanhol) estavam mortos no motor há ~500 leituras e passavam no validador (22/11)."""

    def set_alpn_protocols(self, alpn_protocols: object) -> None:  # noqa: D102
        return None


def _tls_context() -> ssl.SSLContext:
    context = _NoAlpnContext(ssl.PROTOCOL_TLS_CLIENT)        # verifica nome e certificado
    context.load_verify_locations(certifi.where())
    return context


_TLS = _tls_context()


def _accept_language(language: str | None) -> str:
    """Sem língua, a do blog (pt-BR). Com a da fonte, ela primeiro: a AWS servia ao
    `Accept-Language: pt-BR` o feed em português, parado desde junho de 2024, em vez do
    What's New em inglês (22/11)."""
    code = (language or "").strip()
    if not code or code.lower().startswith("pt"):
        return f"{settings.content_locale},pt;q=0.9,en;q=0.8"
    return f"{code},en;q=0.8,*;q=0.5" if not code.lower().startswith("en") else "en,*;q=0.5"
# host → (expira, regras | None). None = sem robots.txt utilizável → permitido.
_robots: dict[str, tuple[float, Robots | None]] = {}
# host → instante (monotónico) do último pedido; R42. Os testes substituem `_sleep`.
_last_hit: dict[str, float] = {}
_sleep = time.sleep
_LAST_HIT_MAX = 5000


class FetchError(Exception):
    """Pedido recusado ou falhado. `kind` diz porquê, para registo e testes."""

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}" + (f": {detail}" if detail else ""))
        self.kind = kind
        self.detail = detail


@dataclass(frozen=True)
class Fetched:
    url: str            # endereço final, depois dos redirecionamentos
    status: int
    content_type: str
    body: bytes

    @property
    def text(self) -> str:
        charset = "utf-8"
        for part in self.content_type.split(";")[1:]:
            name, _, value = part.strip().partition("=")
            if name.lower() == "charset" and value:
                charset = value.strip("\"' ")
        try:
            return self.body.decode(charset, errors="replace")
        except LookupError:
            return self.body.decode("utf-8", errors="replace")


_EMBEDDED_V4 = (ipaddress.ip_network("64:ff9b::/96"), ipaddress.ip_network("64:ff9b:1::/48"),
                ipaddress.ip_network("::/96"))


def is_public_ip(raw: str) -> bool:
    """True só para endereços globalmente encaminháveis."""
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif any(ip in net for net in _EMBEDDED_V4):
            # NAT64 e IPv4-compatível trazem um IPv4 dentro: nunca se liga por aí.
            return False
    # `is_global` exclui privadas, loopback, link-local, CGNAT (100.64/10),
    # documentação, reservadas e não especificadas; multicast verifica-se à parte.
    return ip.is_global and not ip.is_multicast


def resolve(host: str) -> list[str]:
    """IPs de um nome. Separado para os testes o substituírem."""
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def check_url(url: str) -> tuple[str, str]:
    """Valida o endereço e devolve (host, IP público verificado a que ligar)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        raise FetchError("bad_url", url[:200]) from None
    if parts.scheme.lower() != "https":
        raise FetchError("not_https", url[:200])
    if parts.username or parts.password:
        raise FetchError("credentials_in_url")
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise FetchError("no_host")
    try:
        port = parts.port
    except ValueError:
        raise FetchError("bad_port") from None
    if port not in (None, 443):
        raise FetchError("bad_port", str(port))
    # Um IP literal no endereço também tem de ser público.
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        try:
            addresses = resolve(host)
        except OSError as exc:
            raise FetchError("dns_error", f"{host}: {type(exc).__name__}") from None
    if not addresses:
        raise FetchError("dns_error", host)
    blocked = [ip for ip in addresses if not is_public_ip(ip)]
    if blocked:
        # Basta um IP interno entre as respostas para recusar: não se escolhe "o bom".
        raise FetchError("blocked_address", f"{host} → {blocked[0]}")
    return host, addresses[0]


def _space(host: str) -> None:
    """Espera o que faltar para o intervalo mínimo desde o último pedido a `host` (R42)."""
    interval = settings.fetch_host_interval_s
    if interval <= 0:
        return
    last = _last_hit.get(host)
    if last is not None:
        gap = interval - (time.monotonic() - last)
        if gap > 0:
            _sleep(gap)
    if len(_last_hit) >= _LAST_HIT_MAX:
        _last_hit.clear()
    _last_hit[host] = time.monotonic()


def _request(url: str, *, max_bytes: int, deadline: float, accept: str,
             max_bytes_pdf: int | None = None, truncate: bool = False,
             language: str | None = None) -> Fetched | str:
    """Um pedido, sem seguir redirecionamentos. Devolve o conteúdo ou o endereço seguinte.

    `truncate`: passado o teto, devolve o que leu até ele em vez de falhar (robots.txt)."""
    host, ip = check_url(url)
    _space(host)
    parts = urlsplit(url)
    target = f"[{ip}]" if ":" in ip else ip
    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FetchError("timeout")
    try:
        with httpx.Client(transport=_transport, timeout=min(remaining, settings.http_timeout),
                          follow_redirects=False, trust_env=False, verify=_TLS) as client, \
                client.stream(
            "GET", f"https://{target}{path}",
            headers={"Host": host, "User-Agent": settings.http_user_agent, "Accept": accept,
                     "Accept-Language": _accept_language(language)},
            # Liga ao IP verificado; o certificado é validado contra o nome.
            extensions={"sni_hostname": host},
        ) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("location", "")
                if not location:
                    raise FetchError("bad_redirect")
                try:
                    return urljoin(url, location)
                except ValueError:
                    raise FetchError("bad_redirect", location[:200]) from None
            chunks: list[bytes] = []
            size = 0
            limit = max_bytes
            if max_bytes_pdf and "application/pdf" in (
                    response.headers.get("content-type", "").lower()):
                limit = max(max_bytes, max_bytes_pdf)
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > limit:
                    if not truncate:
                        raise FetchError("too_large", f"> {limit} bytes")
                    chunks.append(chunk[: len(chunk) - (size - limit)])
                    break
                if time.monotonic() > deadline:
                    raise FetchError("timeout")
                chunks.append(chunk)
            return Fetched(url=url, status=response.status_code,
                           content_type=response.headers.get("content-type", ""),
                           body=b"".join(chunks))
    except httpx.TimeoutException:
        raise FetchError("timeout") from None
    except httpx.HTTPError as exc:
        raise FetchError("network_error", type(exc).__name__) from None


def _fetch(url: str, *, max_bytes: int, timeout_s: float, accept: str,
           max_bytes_pdf: int | None = None, truncate: bool = False,
           on_hop: Callable[[str], None] | None = None,
           language: str | None = None) -> Fetched:
    """`on_hop`: chamado com cada destino de redirecionamento ANTES de lhe fazer o pedido
    (o `fetch` confere ali o robots.txt do salto, R42)."""
    deadline = time.monotonic() + timeout_s
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if on_hop is not None and current != url:
            on_hop(current)
        result = _request(current, max_bytes=max_bytes, deadline=deadline, accept=accept,
                          max_bytes_pdf=max_bytes_pdf, truncate=truncate, language=language)
        if isinstance(result, Fetched):
            return result
        current = result          # o destino do redirecionamento é verificado na volta seguinte
    raise FetchError("too_many_redirects")


_DISALLOW_ALL = DISALLOW_ALL
ROBOTS_RETRY_S = 600
ROBOTS_MAX_HOSTS = 5000


def _robots_for(host: str) -> Robots | None:
    cached = _robots.get(host)
    if cached and cached[0] > time.monotonic():
        return cached[1]
    parser: Robots | None = None
    ttl = ROBOTS_TTL_S
    try:
        # Maior que o teto: lê-se o início (R42) — proibir o sítio inteiro por um ficheiro
        # grande era pior do que ignorar regras no fim dele.
        page = _fetch(f"https://{host}/robots.txt", max_bytes=ROBOTS_MAX_BYTES,
                      timeout_s=settings.http_timeout, accept="text/plain, */*", truncate=True)
        if page.status == 200:
            parser = Robots.parse(page.text.splitlines())   # RFC 9309 (curingas, 23/11)
        elif page.status in (401, 403, 429) or page.status >= 500:
            # Convenção: robots.txt proibido = o sítio não quer robôs. Com erro do servidor
            # não se sabe o que ele quer: trata-se como proibido (RFC 9309) e volta-se a
            # perguntar mais cedo.
            parser = _DISALLOW_ALL
            ttl = ROBOTS_TTL_S if page.status in (401, 403) else ROBOTS_RETRY_S
    except FetchError as exc:
        log.info("robots.txt de %s inalcançável (%s): tratado como proibido", host, exc.kind)
        parser, ttl = _DISALLOW_ALL, ROBOTS_RETRY_S
    if len(_robots) >= ROBOTS_MAX_HOSTS:
        _robots.clear()
    _robots[host] = (time.monotonic() + ttl, parser)
    return parser


def allowed_by_robots(url: str, agents: tuple[str, ...] = ()) -> bool:
    """O nosso robô pode ler o endereço? Com `agents`, cada um deles também tem de poder."""
    host = (urlsplit(url).hostname or "").lower()
    parser = _robots_for(host)
    if parser is None:
        return True
    return all(parser.can_fetch(agent, url) for agent in (settings.http_user_agent, *agents))


def fetch(url: str, *, max_bytes: int | None = None, timeout_s: float | None = None,
          accept: str = "text/html, application/xhtml+xml, application/xml;q=0.9, */*;q=0.5",
          respect_robots: bool = True, ai_agents: tuple[str, ...] = (),
          max_bytes_pdf: int | None = None, language: str | None = None) -> Fetched:
    """Vai buscar um endereço externo com todas as verificações. Levanta `FetchError`.

    `ai_agents` (D-070): a página vai ser entregue a um modelo de IA; se o robots.txt
    proíbe esses robôs, ela não é lida (`ai_disallowed`), mesmo que o nosso possa.
    `max_bytes_pdf`: teto maior só para respostas `application/pdf` (relatórios oficiais
    são maiores que páginas HTML); sem ele vale `max_bytes` para tudo.
    `language`: a língua da fonte (`sources.lang`) no `Accept-Language`; sem ela, a do blog.
    """
    def robots(target: str) -> None:
        # O endereço é verificado antes de se pedir o robots.txt dele: um salto para a rede
        # interna é `blocked_address`, não «robots inalcançável».
        check_url(target)
        if not allowed_by_robots(target):
            raise FetchError("robots_disallowed", target[:200])
        if ai_agents and not allowed_by_robots(target, ai_agents):
            raise FetchError("ai_disallowed", target[:200])

    check_url(url)                       # falha cedo, antes de ir ao robots.txt
    if respect_robots:
        robots(url)
    page = _fetch(url,
                  max_bytes=settings.fetch_max_bytes if max_bytes is None else max_bytes,
                  timeout_s=settings.fetch_timeout_s if timeout_s is None else timeout_s,
                  accept=accept, max_bytes_pdf=max_bytes_pdf,
                  on_hop=robots if respect_robots else None, language=language)
    # O destino de um redirecionamento passa pelas mesmas verificações, com os mesmos
    # tipos de erro: um bloqueio só dos robôs de IA é `ai_disallowed` (conta como «só de
    # pauta» na D-070), não `robots_disallowed` (revisão E14).
    # (Cada salto, inclusive o último, já passou pelo `robots` em `on_hop`.)
    return page
