"""E3.5 — cadeia de provedores de IA por papel, com o gateway simulado."""
from __future__ import annotations

import json
import logging

import httpx
import pytest
from sqlalchemy import text

from app.core.db import session_scope
from app.services.ai import chain, gateway
from app.services.ai.secrets import SecretUnreadable, decrypt, encrypt
from tests.conftest import fetch

KEY = "sk-chave-de-teste"


class FakeGateway:
    """Responde por provedor: um dict de sucesso ou (status, kind) de erro."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.replies: dict[str, object] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        body["_path"] = request.url.path
        self.calls.append(body)
        reply = self.replies.get(body["provider"], {"text": f"texto de {body['provider']}"})
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, tuple):
            status, kind = reply
            return httpx.Response(status, json={"error": kind, "provider": body["provider"],
                                                "detail": "simulado"})
        payload = {"provider": body["provider"], "model": "modelo-real",
                   "usage": {"input_tokens": 100, "output_tokens": 20}, "duration_ms": 5}
        return httpx.Response(200, json={**payload, **reply})

    @property
    def providers_called(self) -> list[str]:
        return [c["provider"] for c in self.calls]


@pytest.fixture
def gw(monkeypatch) -> FakeGateway:
    fake = FakeGateway()
    monkeypatch.setattr(gateway, "_transport", httpx.MockTransport(fake))
    return fake


def add_api_provider(name: str = "openrouter", family: str = "meta", key: str = KEY) -> None:
    with session_scope() as db:
        db.execute(
            text("""INSERT INTO ai_providers (name, kind, family, model, base_url, api_key_enc)
                    VALUES (:n, 'api', :f, 'modelo-x', 'https://api.exemplo.test/v1', :k)"""),
            {"n": name, "f": family, "k": encrypt(key)},
        )


def set_enabled(name: str, enabled: bool) -> None:
    with session_scope() as db:
        db.execute(text("UPDATE ai_providers SET enabled = :e WHERE name = :n"),
                   {"e": enabled, "n": name})


# --- Seed e validação ----------------------------------------------------------


def test_seed_segue_a_prioridade_do_plano():
    with session_scope() as db:
        order = {role: [p.name for p in chain.load_chain(db, role)] for role in chain.ROLES}
        assert chain.validate(db) == []
    assert order["writer"] == ["claude", "codex", "agy"]
    assert order["translator"] == ["claude", "codex", "agy"]
    assert order["researcher"] == ["claude", "codex", "agy"]
    assert order["reviewer"][0] == "codex"  # outra família que o 1.º redator


def test_papel_desconhecido():
    with pytest.raises(ValueError, match="papel desconhecido"), session_scope() as db:
        chain.load_chain(db, "editor")


def test_redator_igual_a_revisor_e_rejeitado_sem_gravar():
    with pytest.raises(chain.ChainInvalid) as info, session_scope() as db:
        chain.set_chain(db, "reviewer", ["claude"])
    assert any("claude (anthropic) não tem revisor de outra família" in p
               for p in info.value.problems)
    with session_scope() as db:
        assert [p.name for p in chain.load_chain(db, "reviewer")] == ["codex", "claude", "agy"]


def test_familia_conta_e_nao_o_nome_do_provedor():
    # Um provedor por API key que serve um modelo Anthropic é da mesma família do Claude.
    add_api_provider("openrouter-claude", family="anthropic")
    with session_scope() as db:
        chain.set_chain(db, "writer", ["claude"])
    with pytest.raises(chain.ChainInvalid), session_scope() as db:
        chain.set_chain(db, "reviewer", ["openrouter-claude"])


def test_set_chain_valida_e_grava():
    with session_scope() as db:
        chain.set_chain(db, "translator", ["agy", "claude"])
    with session_scope() as db:
        assert [p.name for p in chain.load_chain(db, "translator")] == ["agy", "claude"]


@pytest.mark.parametrize(
    ("names", "fragment"),
    [(["claude", "claude"], "repetido"), (["copilot"], "desconhecido"), ([], "sem nenhum")],
)
def test_set_chain_rejeita(names, fragment):
    with pytest.raises(chain.ChainInvalid, match=fragment), session_scope() as db:
        chain.set_chain(db, "translator", names)


def test_validate_aponta_papel_sem_provedor_ativo():
    for name in ("claude", "codex", "agy"):
        set_enabled(name, False)
    with session_scope() as db:
        found = chain.validate(db)
    assert len(found) == 4 and all("sem nenhum provedor ativo" in p for p in found)


def test_validate_aponta_revisor_da_mesma_familia_apos_desligar():
    set_enabled("codex", False)
    set_enabled("agy", False)
    with session_scope() as db:
        found = chain.validate(db)
    assert found == ["redator claude (anthropic) não tem revisor de outra família"]


def test_arranque_regista_cadeia_invalida(client, caplog):
    set_enabled("codex", False)
    set_enabled("agy", False)
    from fastapi.testclient import TestClient

    from app.main import app

    with caplog.at_level(logging.ERROR), TestClient(app):
        pass
    assert "CADEIA DE IA INVÁLIDA" in caplog.text


# --- Execução e fallback -------------------------------------------------------


def test_primeiro_provedor_responde(gw):
    result = chain.run("writer", "escreve", system="regras", purpose="article")
    assert (result.provider, result.family) == ("claude", "anthropic")
    assert result.text == "texto de claude"
    assert result.skipped == ()
    call = gw.calls[0]
    assert call["_path"] == "/v1/complete"
    assert call["prompt"] == "escreve"
    assert call["system"].startswith("regras\n\nREGRAS DE SEGURANÇA")
    assert "endpoint" not in call and "model" not in call  # claude: modelo por omissão
    usage = fetch("SELECT provider, model, purpose, input_tokens, output_tokens "
                  "FROM provider_usage")
    assert [dict(u) for u in usage] == [{"provider": "claude", "model": "modelo-real",
                                         "purpose": "article", "input_tokens": 100,
                                         "output_tokens": 20}]


def test_modelo_fixo_do_agy_e_enviado(gw):
    gw.replies["claude"] = (504, "timeout")
    gw.replies["codex"] = (504, "timeout")
    chain.run("writer", "x")
    assert gw.calls[-1]["provider"] == "agy"
    assert gw.calls[-1]["model"] == "gemini-3.1-pro-high"


@pytest.mark.parametrize(
    "failure",
    [(429, "rate_limited"), (504, "timeout"), (502, "empty"), (503, "not_authenticated"),
     (502, "invalid_output"), (502, "failed"), httpx.ConnectError("sem rede")],
    ids=["429", "timeout", "vazia", "nao-autenticado", "json-invalido", "falha", "inalcancavel"],
)
def test_fallback_para_o_seguinte(gw, failure):
    gw.replies["claude"] = failure
    result = chain.run("writer", "x")
    assert result.provider == "codex"
    assert gw.providers_called == ["claude", "codex"]
    expected = "unreachable" if isinstance(failure, Exception) else failure[1]
    assert result.skipped == (("claude", expected),)
    # Só o provedor que respondeu é contabilizado.
    ok = fetch("SELECT provider FROM provider_usage WHERE outcome = 'ok'")
    assert [u["provider"] for u in ok] == ["codex"]


def test_resposta_sem_json_do_gateway_conta_como_falha(gw, monkeypatch):
    monkeypatch.setattr(
        gateway, "_transport",
        httpx.MockTransport(lambda _: httpx.Response(500, text="<html>erro</html>")),
    )
    with pytest.raises(chain.ChainExhausted) as info:
        chain.run("writer", "x")
    assert [reason for _, reason in info.value.attempts] == ["failed"] * 3


def test_provedor_desligado_sai_da_cadeia(gw):
    set_enabled("claude", False)
    result = chain.run("writer", "x")
    assert result.provider == "codex"
    assert gw.providers_called == ["codex"]
    assert result.skipped == (("claude", "disabled"),)


def test_todos_falham(gw):
    for name in ("claude", "codex", "agy"):
        gw.replies[name] = (429, "rate_limited")
    with pytest.raises(chain.ChainExhausted) as info:
        chain.run("translator", "x")
    assert info.value.attempts == (("claude", "rate_limited"), ("codex", "rate_limited"),
                                   ("agy", "rate_limited"))
    assert fetch("SELECT 1 FROM provider_usage WHERE outcome = 'ok'") == []


def test_structured_usa_a_rota_e_devolve_o_objeto(gw):
    schema = {"type": "object", "properties": {"nota": {"type": "integer"}},
              "required": ["nota"], "additionalProperties": False}
    gw.replies["claude"] = {"data": {"nota": 7}}
    result = chain.run("researcher", "x", schema=schema)
    assert result.data == {"nota": 7}
    assert gw.calls[0]["_path"] == "/v1/structured"
    assert gw.calls[0]["json_schema"] == schema


# --- Quem escreve nunca revê ----------------------------------------------------


def test_revisor_exige_a_familia_do_redator(gw):
    with pytest.raises(ValueError, match="writer_family"):
        chain.run("reviewer", "x")
    assert gw.calls == []


def test_revisor_salta_a_familia_do_redator(gw):
    # Codex (openai) escreveu: o revisor não pode ser o Codex, mesmo sendo o 1.º da cadeia.
    result = chain.run("reviewer", "x", writer_family="openai")
    assert (result.provider, result.family) == ("claude", "anthropic")
    assert gw.providers_called == ["claude"]
    assert result.skipped == (("codex", "same_family_as_writer"),)


def test_revisor_nunca_recorre_a_familia_do_redator_mesmo_sem_alternativa(gw):
    gw.replies["codex"] = (429, "rate_limited")
    gw.replies["agy"] = (504, "timeout")
    with pytest.raises(chain.ChainExhausted) as info:
        chain.run("reviewer", "x", writer_family="anthropic")
    assert "claude" not in gw.providers_called
    assert ("claude", "same_family_as_writer") in info.value.attempts


# --- Provedores por API key ------------------------------------------------------


def test_api_key_cifrada_em_repouso_e_enviada_ao_gateway(gw):
    add_api_provider()
    with session_scope() as db:
        chain.set_chain(db, "translator", ["openrouter", "claude"])
    stored = fetch("SELECT api_key_enc FROM ai_providers WHERE name = 'openrouter'")[0]
    assert KEY not in stored["api_key_enc"] and decrypt(stored["api_key_enc"]) == KEY

    result = chain.run("translator", "x")
    assert result.provider == "openrouter"
    assert gw.calls[0]["endpoint"] == {"base_url": "https://api.exemplo.test/v1",
                                       "api_key": KEY, "model": "modelo-x"}
    assert "model" not in gw.calls[0]


def test_api_key_ilegivel_passa_ao_seguinte(gw, monkeypatch, caplog):
    add_api_provider()
    with session_scope() as db:
        chain.set_chain(db, "translator", ["openrouter", "claude"])
    from app.core.config import settings

    monkeypatch.setattr(settings, "sb_secret_key", "outra-chave")
    result = chain.run("translator", "x")
    assert result.provider == "claude"
    assert result.skipped == (("openrouter", "secret_unreadable"),)
    assert gw.providers_called == ["claude"]
    with pytest.raises(SecretUnreadable):
        decrypt("isto-nao-e-um-token")


def test_provedor_api_sem_chave_passa_ao_seguinte(gw):
    with session_scope() as db:
        db.execute(text("""INSERT INTO ai_providers (name, kind, family, model, base_url)
                           VALUES ('groq', 'api', 'meta', 'm', 'https://x.test/v1')"""))
        chain.set_chain(db, "translator", ["groq", "claude"])
    result = chain.run("translator", "x")
    assert result.skipped == (("groq", "not_configured"),)


def test_chave_nunca_aparece_em_logs_nem_erros(gw, caplog):
    add_api_provider()
    with session_scope() as db:
        chain.set_chain(db, "translator", ["openrouter"])
    gw.replies["openrouter"] = httpx.ConnectError(f"falhou a ligar com {KEY}")
    with caplog.at_level(logging.DEBUG), pytest.raises(chain.ChainExhausted) as info:
        chain.run("translator", "x")
    assert KEY not in caplog.text
    assert KEY not in str(info.value)
    assert KEY not in repr(gateway.Endpoint("https://x", KEY, "m"))


# --- Rota ------------------------------------------------------------------------


def test_rota_exige_token(client):
    assert client.get("/api/ai/chain").status_code == 401


def test_rota_mostra_cadeia_sem_segredos(client, auth):
    add_api_provider()
    with session_scope() as db:
        chain.set_chain(db, "translator", ["openrouter", "claude"])
    res = client.get("/api/ai/chain", headers=auth)
    assert res.status_code == 200
    body = res.json()
    assert body["problems"] == []
    assert [p["name"] for p in body["roles"]["writer"]] == ["claude", "codex", "agy"]
    first = body["roles"]["translator"][0]
    assert first == {"name": "openrouter", "kind": "api", "family": "meta", "model": "modelo-x",
                     "enabled": True, "has_api_key": True, "health": "unknown",
                     "limit_reached": None}
    assert "api_key" not in res.text.replace("has_api_key", "")
    assert KEY not in res.text


# --- Imagem pelo Codex (D-056) ----------------------------------------------------------------


class FakeImageGateway:
    def __init__(self, reply=None) -> None:
        self.calls: list[dict] = []
        self.reply = reply

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        body["_path"] = request.url.path
        self.calls.append(body)
        if isinstance(self.reply, tuple):
            return httpx.Response(self.reply[0], json={"error": self.reply[1], "detail": "x"})
        import base64
        return httpx.Response(200, json={
            "provider": "codex", "model": "codex", "duration_ms": 9,
            "image_base64": base64.b64encode(b"PNGDATA").decode(),
            "usage": {"input_tokens": 300, "output_tokens": 20}})


@pytest.fixture
def img_gw(monkeypatch) -> FakeImageGateway:
    fake = FakeImageGateway()
    monkeypatch.setattr(gateway, "_transport", httpx.MockTransport(fake))
    return fake


def test_run_image_devolve_os_bytes_e_regista_o_uso(img_gw):
    assert chain.run_image("blue cubes", site="cloud") == b"PNGDATA"
    assert img_gw.calls[0]["_path"] == "/v1/image" and img_gw.calls[0]["provider"] == "codex"
    row = fetch("SELECT provider, role, purpose, site, outcome, input_tokens FROM provider_usage "
                "WHERE purpose = 'illustration_image'")[0]
    assert (row["provider"], row["role"], row["site"], row["outcome"]) == (
        "codex", "illustrator", "cloud", "ok")
    assert row["input_tokens"] == 300


def test_run_image_com_codex_desligado_nao_chama_o_gateway(img_gw):
    set_enabled("codex", False)
    with pytest.raises(chain.ImageUnavailable, match="desligado"):
        chain.run_image("x")
    assert img_gw.calls == []


def test_run_image_respeita_o_limite_diario(img_gw):
    with session_scope() as db:
        db.execute(text("UPDATE ai_providers SET daily_call_limit = 1 WHERE name = 'codex'"))
    chain.run_image("x")
    with pytest.raises(chain.ImageUnavailable, match="limit"):
        chain.run_image("x")
    assert len(img_gw.calls) == 1


def test_run_image_falha_do_gateway_gasta_cota_e_levanta(img_gw):
    img_gw.reply = (502, "failed")
    with pytest.raises(chain.ImageUnavailable):
        chain.run_image("x")
    rows = fetch("SELECT outcome FROM provider_usage WHERE purpose = 'illustration_image'")
    assert [r["outcome"] for r in rows] == ["failed"]


def test_run_image_nao_autenticado_nao_gasta_cota(img_gw):
    img_gw.reply = (503, "not_authenticated")
    with pytest.raises(chain.ImageUnavailable):
        chain.run_image("x")
    assert fetch("SELECT 1 FROM provider_usage WHERE purpose = 'illustration_image'") == []


def test_uso_fica_ligado_ao_job_em_producao(gw, img_gw):
    """`quota.for_job` (a produção usa-o): cada chamada leva o job; fora do bloco, nenhum."""
    from app.services import sites
    from app.services.ai import quota

    with session_scope() as db:
        sites.sync(db)
        job = db.execute(text(
            "INSERT INTO publication_jobs (site_key, local_date, slot_number, due_at_utc, "
            "produce_after_utc, status) VALUES ('cloud', '2026-10-05', 1, now(), now(), "
            "'RESEARCHING') RETURNING id")).scalar_one()
    with quota.for_job(job):
        chain.run("writer", "escreve", purpose="article", site="cloud")
        chain.run_image("blue cubes", site="cloud")
    chain.run("researcher", "triagem", purpose="triage", site="cloud")
    rows = {r["purpose"]: r["job_id"] for r in fetch(
        "SELECT purpose, job_id FROM provider_usage")}
    assert rows == {"article": job, "illustration_image": job, "triage": None}
