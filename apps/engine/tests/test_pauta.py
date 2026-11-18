"""D-094 (opção A da D-OPEN-15) — newsletters de fabricantes viram ideias «só pauta»."""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.core.db import session_scope
from app.services import fetcher, pauta, sites
from tests.conftest import fetch

TOKEN = "token-de-teste-pauta"
SENDER = "news@em.microsoft.com"
AUTHSERV = "mx.brevo.test"
# Authentication-Results do recebedor: SPF e DKIM `pass`, domínios alinhados ao From.
AUTH = (f"{AUTHSERV}; spf=pass smtp.mailfrom=bounce.em.microsoft.com; "
        "dkim=pass header.d=microsoft.com header.i=@microsoft.com")
FINAL = "https://azure.microsoft.com/blog/novidade-de-ia"
TRACKED = "https://click.mail.example/r/abc123"

HTML = f"""
<html><body>
  <a href="{TRACKED}">Azure announces a new AI service for everyone</a>
  <a href="https://news.microsoft.com/source/ai">Microsoft shares AI updates for developers</a>
  <a href="https://news.microsoft.com/source/ai">Microsoft shares AI updates for developers</a>
  <a href="http://insecure.example/post">Insecure post that should be skipped entirely</a>
  <a href="https://example.com/unsubscribe?u=1">Unsubscribe</a>
  <a href="https://example.com/email/preferences">Manage your preferences and settings here</a>
  <a href="https://facebook.com/microsoft">Follow Microsoft on Facebook for daily updates</a>
  <a href="https://example.com/short">Read</a>
  <a href="https://example.com/bare-url">https://example.com/bare-url</a>
</body></html>
"""


@pytest.fixture
def pt(monkeypatch, tmp_path) -> None:
    """Remetentes de teste sincronizados e o desembrulho de rastreio simulado."""
    config = tmp_path / "newsletters.yaml"
    config.write_text(
        "newsletters:\n"
        "  - name: Microsoft.Source\n    sender_domain: microsoft.com\n"
        "    site_key: cloud\n    lang: en\n",
        encoding="utf-8")
    monkeypatch.setattr(settings, "newsletters_config_file", str(config))
    monkeypatch.setattr(settings, "newsletter_api_token", TOKEN)
    monkeypatch.setattr(settings, "pauta_webhook_token", "pauta-secreto")
    monkeypatch.setattr(settings, "pauta_authserv_id", AUTHSERV)

    def fake_fetch(url: str, **kwargs) -> fetcher.Fetched:
        if url == TRACKED:                       # rastreio: redireciona para a URL final
            url = FINAL
        if "fora-do-ar" in url:
            raise fetcher.FetchError("http_status", "404")
        return fetcher.Fetched(url=url, status=200,
                               content_type="text/html; charset=utf-8", body=b"<html></html>")

    monkeypatch.setattr(fetcher, "fetch", fake_fetch)
    with session_scope() as db:
        sites.sync(db)
        pauta.sync(db)


def ingest(**kw) -> dict:
    defaults = {"sender": SENDER, "subject": "Microsoft.Source — outubro",
                "html": HTML, "auth": [AUTH]}
    with session_scope() as db:
        return pauta.ingest(db, **{**defaults, **kw})


def test_yaml_sincroniza_e_autoriza_por_dominio_e_subdominio(pt):
    with session_scope() as db:
        assert pauta.authorized(db, "news@microsoft.com")["site_key"] == "cloud"
        assert pauta.authorized(db, SENDER)["name"] == "Microsoft.Source"
        assert pauta.authorized(db, "news@evilmicrosoft.com") is None
        assert pauta.authorized(db, "alguem@example.com") is None
        assert pauta.authorized(db, "") is None


def test_yaml_invalido_trava_a_sincronizacao(tmp_path):
    bad = tmp_path / "newsletters.yaml"
    bad.write_text("newsletters:\n  - name: SemDominio\n    site_key: cloud\n", encoding="utf-8")
    with pytest.raises(pauta.NewsletterConfigError):
        pauta.load(bad)
    assert pauta.load(tmp_path / "nao-existe.yaml") == []   # ausente = recurso desligado


def test_extrator_fica_so_com_as_noticias():
    items = pauta.extract_items(HTML)
    assert [u for _, u in items] == [TRACKED, "https://news.microsoft.com/source/ai"]
    titles = [t for t, _ in items]
    assert titles[0] == "Azure announces a new AI service for everyone"
    # Fora: http://, descadastro, preferências, social, título curto, título que é só URL,
    # e o duplicado da mesma ligação.


def test_extrator_respeita_o_teto_de_itens(monkeypatch):
    monkeypatch.setattr(settings, "pauta_max_items", 1)
    assert len(pauta.extract_items(HTML)) == 1


def test_filtro_nao_descarta_noticia_com_management():
    """Regressão (revisão E14): `manage`/`preference` como substring descartavam pauta
    legítima («cost management», «/alert-management/»)."""
    html = ('<a href="https://aws.amazon.com/blogs/cost-management/novidade">'
            'New cost management features in AWS Console</a>'
            '<a href="https://grafana.com/blog/alert-management/update">'
            'Alert management updates land in Grafana</a>'
            '<a href="https://example.com/email/preferences">'
            'Manage your preferences and settings</a>')
    assert [t for t, _ in pauta.extract_items(html)] == [
        "New cost management features in AWS Console",
        "Alert management updates land in Grafana"]


def test_ingest_desembrulha_o_rastreio_e_cria_ideias_so_pauta(pt):
    stats = ingest()
    assert stats["accepted"] and (stats["items"], stats["new"]) == (2, 2)
    rows = fetch("SELECT origin, canonical_url, site_key, created_by, source_id FROM ideas "
                 "ORDER BY id")
    assert [r["origin"] for r in rows] == ["newsletter", "newsletter"]
    assert rows[0]["canonical_url"] == FINAL                 # a URL final, não a de rastreio
    assert rows[0]["site_key"] == "cloud"
    assert rows[0]["created_by"] == "newsletter:microsoft.com"
    assert rows[0]["source_id"] is None                      # a newsletter nunca é fonte (D-070)
    # Reenvio do mesmo e-mail (URLs de rastreio iguais): tudo já conhecido, nada novo.
    again = ingest()
    assert (again["new"], again["known"]) == (0, 2)


def test_ingest_descarta_item_cujo_destino_nao_resolve(pt):
    html = '<a href="https://click.mail.example/fora-do-ar">A good story that went missing</a>'
    stats = ingest(html=html)
    assert (stats["items"], stats["new"], stats["discarded"]) == (1, 0, 1)


def test_ingest_recusa_remetente_desconhecido_e_auth_fraca(pt, monkeypatch):
    assert ingest(sender="news@example.com") == {"accepted": False, "reason": "sender"}
    # Sem cabeçalho, sem `pass`, ou `pass` do domínio do atacante (não alinha com o From).
    assert ingest(auth=[]) == {"accepted": False, "reason": "auth"}
    assert ingest(auth=[f"{AUTHSERV}; spf=fail smtp.mailfrom=microsoft.com; "
                        "dkim=fail header.d=microsoft.com"]) == \
        {"accepted": False, "reason": "auth"}
    assert ingest(auth=[f"{AUTHSERV}; spf=pass smtp.mailfrom=attacker.example; "
                        "dkim=pass header.d=attacker.example"]) == \
        {"accepted": False, "reason": "auth"}
    # Authentication-Results escrito pelo próprio remetente (authserv-id alheio) não vale,
    # mesmo alegando `pass` alinhado (RFC 8601).
    assert ingest(auth=["forjado.example; dkim=pass header.d=microsoft.com"]) == \
        {"accepted": False, "reason": "auth"}
    # Sem o authserv-id configurado, nada passa (R62).
    monkeypatch.setattr(settings, "pauta_authserv_id", "")
    assert ingest() == {"accepted": False, "reason": "auth"}
    assert fetch("SELECT count(*) AS n FROM ideas")[0]["n"] == 0


def test_rota_exige_bearer_chave_e_token_configurado(pt, client, monkeypatch):
    queued: list[dict] = []
    monkeypatch.setattr(pauta, "enqueue_ingest", lambda **kw: queued.append(kw))
    body = {"key": "pauta-secreto", "sender": SENDER, "subject": "s", "html": HTML,
            "auth": [AUTH]}
    assert client.post("/api/pauta/brevo-inbound", json=body).status_code == 401
    ok = {"Authorization": f"Bearer {TOKEN}"}
    assert client.post("/api/pauta/brevo-inbound", json={**body, "key": "errada"},
                       headers=ok).status_code == 401
    assert queued == []                       # nada entra na fila sem o Bearer e a chave
    # Com credenciais, o webhook só enfileira (revisão E14); o worker processa depois.
    reply = client.post("/api/pauta/brevo-inbound", json=body, headers=ok).json()
    assert reply == {"accepted": True, "queued": True} and len(queued) == 1
    stats = pauta.ingest_email(**queued[0])
    assert stats["accepted"] and stats["new"] == 2
    monkeypatch.setattr(settings, "pauta_webhook_token", "")
    assert client.post("/api/pauta/brevo-inbound", json=body, headers=ok).status_code == 503
    assert len(queued) == 1
