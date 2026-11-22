"""Autenticação do painel — D-018: Cloudflare Access + login próprio (E2.9).

Duas regras, aplicadas por `panel_guard` a cada pedido:

1. **Pedido pelo domínio público** (`Host == SB_HOST`): só passa com um JWT do
   Cloudflare Access válido (assinatura do team, `aud` da aplicação, email na
   lista). A Cloudflare já barra quem não fez login; isto barra quem chame a
   origem diretamente pelo IP com o mesmo `Host`.
2. **Páginas do painel** (`/`, `/dashboard/*`, `/docs`): exigem também a sessão
   do login próprio (cookie assinado), venha o pedido de onde vier.

E18.1 (D-104): a sessão é de um usuário da tabela `users` (`uid` + `session_version`);
as áreas de `ADMIN_PREFIXES` só abrem para o papel `admin`; com troca de senha pendente
só `/conta/senha` abre; e o Access pode ser desligado (`PANEL_REQUIRE_ACCESS=false`) no
pacote da E19, que não o terá.

O n8n e os healthchecks falam pela rede interna (`Host: sb-engine-api:8000`):
`/api/**` continua só com o bearer token e `/health/**` livre.
"""
from __future__ import annotations

import ipaddress
import time
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any
from urllib.parse import urlencode, urlsplit

import jwt
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from itsdangerous import BadSignature, URLSafeTimedSerializer
from starlette.concurrency import run_in_threadpool

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

SESSION_COOKIE = "sb_session"
ACCESS_HEADER = "Cf-Access-Jwt-Assertion"
ACCESS_COOKIE = "CF_Authorization"

# Livres de sessão do painel (têm proteção própria ou são públicas por desenho).
_NO_SESSION_PREFIXES = ("/api/", "/health/", "/media/", "/metrics", "/login", "/logout", "/a/")
# Pelo domínio público, nada passa sem Access — exceto o liveness.
_NO_ACCESS_PATHS = ("/health/live",)


class AccessDenied(Exception):
    pass


# --- Cloudflare Access -------------------------------------------------------
@lru_cache(maxsize=4)
def _jwks_client(team_domain: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(f"https://{team_domain}/cdn-cgi/access/certs",
                           cache_keys=True, lifespan=3600)


def verify_access_token(token: str) -> str:
    """Valida o JWT do Cloudflare Access e devolve o email autorizado."""
    team, aud = settings.cf_access_team_domain, settings.cf_access_aud
    if not (team and aud):
        raise AccessDenied("Cloudflare Access não configurado (CF_ACCESS_*)")
    if not token:
        raise AccessDenied("sem token do Cloudflare Access")
    try:
        key = _jwks_client(team).get_signing_key_from_jwt(token).key
        claims: dict[str, Any] = jwt.decode(
            token, key, algorithms=["RS256"], audience=aud,
            issuer=f"https://{team}", options={"require": ["exp", "iat", "aud", "iss"]},
        )
    except jwt.PyJWTError as exc:
        raise AccessDenied(f"token do Access inválido: {exc}") from exc
    email = str(claims.get("email", "")).lower()
    if not email or email not in settings.allowed_panel_emails:
        raise AccessDenied(f"email não autorizado: {email or '(vazio)'}")
    return email


# --- Sessão do login próprio --------------------------------------------------
def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.sb_secret_key, salt="sb-panel-session")


def make_session(user_id: int, session_version: int) -> str:
    """E18.1: a sessão leva o usuário e a versão; mudar a versão no banco derruba o cookie."""
    return _serializer().dumps({"uid": int(user_id), "sv": int(session_version)})


def read_session(cookie: str | None) -> tuple[int, int] | None:
    return read_session_with_expiry(cookie)[0]


def read_session_with_expiry(cookie: str | None) -> tuple[tuple[int, int] | None, float | None]:
    """(user_id, session_version) do cookie e o instante (epoch) em que ele vence.

    Cookies do login único anterior (`{"email": …}`) não valem mais: pedem login de novo."""
    if not cookie:
        return None, None
    max_age = settings.panel_session_hours * 3600
    try:
        data, issued = _serializer().loads(cookie, max_age=max_age, return_timestamp=True)
    except BadSignature:
        return None, None
    if not isinstance(data, dict) or not isinstance(data.get("uid"), int) \
            or not isinstance(data.get("sv"), int):
        return None, None
    return (data["uid"], data["sv"]), issued.timestamp() + max_age


def _load_user(user_id: int, version: int) -> dict[str, Any] | None:
    from app.core.db import session_scope
    from app.services import users

    with session_scope() as db:
        return users.session_user(db, user_id, version)


@lru_cache(maxsize=4)
def _networks(spec: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    nets = []
    for item in spec.split(","):
        if item.strip():
            try:
                nets.append(ipaddress.ip_network(item.strip(), strict=False))
            except ValueError:
                log.warning("TRUSTED_PROXIES: rede inválida ignorada: %s", item)
    return tuple(nets)


def _trusted(ip: str | None) -> bool:
    """O IP é de um proxy de confiança (`settings.trusted_proxies`)?"""
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip.strip())
    except ValueError:
        return False
    return any(addr in net for net in _networks(settings.trusted_proxies))


def _peer(request: Request) -> str | None:
    return request.client.host if request.client else None


def client_ip(request: Request) -> str:
    """IP de quem tenta entrar (base do teto de tentativas por IP, R69).

    - Atrás do Cloudflare (Access validado): o `CF-Connecting-IP`.
    - Atrás de um proxy de confiança (`TRUSTED_PROXIES`: o Caddy do pacote, o ingress do Helm):
      o último salto do `X-Forwarded-For` que NÃO é um proxy de confiança — os saltos à esquerda
      vêm do cliente e podem ser forjados, os da direita foram acrescentados pelos nossos proxies.
    - Senão, o par TCP (um `X-Forwarded-For` vindo de fora é ignorado).
    """
    if request.state.access_email and request.headers.get("cf-connecting-ip"):
        return request.headers["cf-connecting-ip"]
    peer = _peer(request)
    if _trusted(peer):
        chain = request.headers.get("x-forwarded-for", "")
        hops = [h.strip() for h in chain.split(",") if h.strip()]
        for hop in reversed(hops):
            try:
                ipaddress.ip_address(hop)
            except ValueError:
                break  # salto malformado: não dá para confiar no que está à esquerda
            if not _trusted(hop):
                return hop
    return peer or "desconhecido"


def forwarded_https(request: Request) -> bool:
    """O navegador falou https? (direto, ou pelo `X-Forwarded-Proto` de um proxy de confiança)."""
    if request.url.scheme == "https":
        return True
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return proto == "https" and _trusted(_peer(request))


def _access_expiry(token: str) -> float | None:
    """`exp` do JWT do Access, já validado por `verify_access_token` (aqui só se lê o prazo)."""
    try:
        exp = jwt.decode(token, options={"verify_signature": False}).get("exp")
    except jwt.PyJWTError:
        return None
    return float(exp) if isinstance(exp, (int, float)) else None


def _login_redirect(request: Request, path: str) -> RedirectResponse:
    """Sem sessão. Num GET, volta ao mesmo caminho depois do login. Num POST (ex.: aprovar em
    lote com a sessão vencida) a ação NÃO foi aplicada: volta à página de onde veio, com aviso,
    em vez de cair num GET da rota do formulário e perder a ação em silêncio."""
    if request.method in ("GET", "HEAD"):
        return RedirectResponse(f"/login?next={path}", status_code=303)
    back = "/"
    referer = urlsplit(request.headers.get("referer", ""))
    host = request.headers.get("host", "").lower()
    if referer.path.startswith("/") and (referer.netloc or "").lower() == host:
        back = referer.path + (f"?{referer.query}" if referer.query else "")
    return RedirectResponse(f"/login?{urlencode({'next': back, 'expired': 1})}", status_code=303)


# E18.1d: só o admin entra aqui (o operador recebe 403).
ADMIN_PREFIXES = ("/dashboard/ai", "/dashboard/users", "/dashboard/config", "/dashboard/system",
                  "/dashboard/setup", "/docs", "/openapi.json", "/redoc")
# Com troca de senha pendente, só estas rotas abrem.
_PASSWORD_CHANGE_PATHS = ("/conta/senha", "/logout")


def is_admin_path(path: str) -> bool:
    return path.startswith(ADMIN_PREFIXES)


# --- Middleware ---------------------------------------------------------------
def is_public_request(request: Request) -> bool:
    host = request.headers.get("host", "").split(":")[0].lower()
    return bool(settings.sb_host) and host == settings.sb_host.lower()


def needs_session(path: str) -> bool:
    return not path.startswith(_NO_SESSION_PREFIXES)


def _deny(request: Request, status: int, detail: str) -> Response:
    if "text/html" in request.headers.get("accept", ""):
        from fastapi.responses import HTMLResponse

        return HTMLResponse(f"<h1>{status}</h1><p>{detail}</p>", status_code=status)
    return JSONResponse({"detail": detail}, status_code=status)


def cross_site_write(request: Request) -> bool:
    """POST do painel vindo de outra origem (CSRF, R46c). Os navegadores enviam `Origin` (ou, no
    mínimo, `Referer`) nos formulários; sem nenhum dos dois não é um navegador e a sessão decide."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return False
    sent = request.headers.get("origin") or request.headers.get("referer") or ""
    if not sent:
        return False
    host = request.headers.get("host", "").lower()
    return not host or (urlsplit(sent).netloc or "").lower() != host


async def panel_guard(request: Request,
                      call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    path = request.url.path
    request.state.access_email = None
    request.state.panel_user = None          # email do usuário logado (é o que os eventos gravam)
    request.state.panel_role = None
    request.state.panel_user_id = None
    # Segundos até a 1.ª das duas sessões (Access ou login próprio) vencer; o `_base.html` usa
    # isto para avisar e travar formulários antes de um clique se perder (12/11: a aprovação do
    # lote de 13/11 nem chegou ao motor porque a sessão do Access tinha vencido).
    request.state.session_seconds_left = None
    deadlines: list[float] = []

    if settings.panel_require_access and is_public_request(request) \
            and path not in _NO_ACCESS_PATHS:
        token = request.headers.get(ACCESS_HEADER) or request.cookies.get(ACCESS_COOKIE, "")
        try:
            request.state.access_email = verify_access_token(token)
        except AccessDenied as exc:
            log.warning("Painel: acesso negado em %s (%s)", path, exc)
            return _deny(request, 403, "Acesso negado")
        if (access_exp := _access_expiry(token)) is not None:
            deadlines.append(access_exp)

    if needs_session(path):
        session, session_exp = read_session_with_expiry(request.cookies.get(SESSION_COOKIE))
        user = await run_in_threadpool(_load_user, *session) if session else None
        if not user:
            if request.method not in ("GET", "HEAD"):
                log.warning("Painel: %s %s sem sessão válida — ação não aplicada",
                            request.method, path)
            return _login_redirect(request, path)
        request.state.panel_user = user["email"]
        request.state.panel_role = user["role"]
        request.state.panel_user_id = user["id"]
        if user["must_change_password"] and not path.startswith(_PASSWORD_CHANGE_PATHS):
            return RedirectResponse("/conta/senha?forced=1", status_code=303)
        if is_admin_path(path) and user["role"] != "admin":
            log.warning("Painel: %s sem papel admin em %s", user["email"], path)
            return _deny(request, 403, "Só o admin acessa esta área")
        if session_exp is not None:
            deadlines.append(session_exp)
        if deadlines:
            request.state.session_seconds_left = max(0, int(min(deadlines) - time.time()))
        if cross_site_write(request):
            log.warning("Painel: pedido de outra origem recusado em %s", path)
            return _deny(request, 403, "Pedido de outra origem")

    return await call_next(request)
