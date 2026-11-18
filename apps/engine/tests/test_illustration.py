"""E7 — imagens com licença comprovada, ilustração por IA e o passo na produção."""
from __future__ import annotations

import io
import json

import httpx
import pytest
from PIL import Image

from app.core.config import settings
from app.services import fetcher, illustration
from app.services.ai import gateway
from tests.conftest import fetch
from tests.test_production import NOW, FakeAI


def jpeg(width: int = 1800, height: int = 1100, color=(30, 90, 200)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buf, format="JPEG")
    return buf.getvalue()


OPENVERSE_REPLY = {"results": [
    {"url": "https://img.test/ok.jpg", "foreign_landing_url": "https://flickr.test/ok",
     "license": "cc0", "license_url": "https://creativecommons.org/publicdomain/zero/1.0/",
     "creator": "Ana", "title": "Server room", "tags": [{"name": "server"}],
     "width": 1800, "height": 1100},
    {"url": "https://img.test/by.jpg", "license": "by", "title": "Com atribuição"},
    {"url": "https://img.test/small.jpg", "license": "cc0", "title": "Pequena",
     "width": 400, "height": 300},
    {"url": "https://img.test/map.jpg", "license": "cc0", "title": "Data center map of the US",
     "width": 3000, "height": 2000},
    {"url": "http://img.test/plain.jpg", "license": "pdm", "title": "Sem https"},
]}
COMMONS_REPLY = {"query": {"pages": {
    "1": {"title": "File:Data center.jpg", "imageinfo": [{
        "thumburl": "https://upload.test/dc.jpg", "mime": "image/jpeg", "thumbwidth": 1600,
        "thumbheight": 1000, "descriptionurl": "https://commons.test/File:Data_center.jpg",
        "extmetadata": {"LicenseShortName": {"value": "CC0"},
                        "LicenseUrl": {"value": "https://creativecommons.org/zero"},
                        "Artist": {"value": '<a href="x">Bia</a>'}}}]},
    "2": {"title": "File:Com BY.jpg", "imageinfo": [{
        "thumburl": "https://upload.test/by.jpg", "mime": "image/jpeg", "thumbwidth": 1600,
        "extmetadata": {"LicenseShortName": {"value": "CC BY-SA 4.0"}}}]},
}}}


@pytest.fixture
def world(monkeypatch) -> dict:
    """Bancos de imagens e downloads simulados; `calls` regista o que foi pedido."""
    state: dict = {"calls": [], "files": {"https://img.test/ok.jpg": jpeg(),
                                          "https://upload.test/dc.jpg": jpeg()},
                   "openverse": OPENVERSE_REPLY, "commons": COMMONS_REPLY}

    def fake_fetch(url: str, **kwargs) -> fetcher.Fetched:
        state["calls"].append(url)
        if url.startswith(illustration.OPENVERSE):
            return fetcher.Fetched(url, 200, "application/json",
                                   json.dumps(state["openverse"]).encode())
        if url.startswith(illustration.COMMONS):
            return fetcher.Fetched(url, 200, "application/json",
                                   json.dumps(state["commons"]).encode())
        body = state["files"].get(url)
        if body is None:
            raise fetcher.FetchError("http_status", "404")
        return fetcher.Fetched(url, 200, "image/jpeg", body)

    monkeypatch.setattr(fetcher, "fetch", fake_fetch)
    monkeypatch.setattr(settings, "illustration_enabled", True)
    return state


# --- licenças -----------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("cc0", "cc0"), ("CC0", "cc0"), ("CC0 1.0", "cc0"), ("pdm", "pdm"),
    ("Public domain", "pdm"), ("by", None), ("CC BY-SA 4.0", None), ("by-nc", None),
    ("", None), ("all rights reserved", None)])
def test_licencas_permitidas(raw, expected):
    assert illustration.normalise_license(raw) == expected


def test_busca_so_devolve_licencas_permitidas(world):
    found = illustration.find_candidates(["server room"])
    assert {c.image_url for c in found} == {"https://img.test/ok.jpg", "https://upload.test/dc.jpg"}
    commons = next(c for c in found if c.provider == "commons")
    assert (commons.license, commons.author) == ("cc0", "Bia")
    assert commons.landing_url == "https://commons.test/File:Data_center.jpg"
    # só pede CC0 e domínio público ao Openverse
    assert "license=cc0%2Cpdm" in world["calls"][0]


def test_foto_ja_usada_nao_volta(world):
    world["commons"] = {}
    first = illustration.find_candidates(["x"])[0]
    job = _job()
    illustration.save(job, _art(source_url=first.image_url))
    assert illustration.find_candidates(["x"]) == []


def test_banco_em_baixo_nao_impede_o_outro(world, monkeypatch):
    real = fetcher.fetch

    def down(url, **kw):
        if url.startswith(illustration.OPENVERSE):
            raise fetcher.FetchError("timeout")
        return real(url, **kw)

    monkeypatch.setattr(fetcher, "fetch", down)
    assert [c.provider for c in illustration.find_candidates(["x"])] == ["commons"]


# --- imagem ---------------------------------------------------------------------------------

def test_prepare_recorta_em_16x9_webp():
    webp, w, h, digest = illustration.prepare(jpeg(2000, 2000))
    assert (w, h) == illustration.TARGET and len(digest) == 64
    assert Image.open(io.BytesIO(webp)).format == "WEBP"


@pytest.mark.parametrize("raw,why", [
    (jpeg(600, 400), "pequena"), (b"nao sou imagem", "válida"),
    (b"x" * (settings.illustration_max_bytes + 1), "grande")])
def test_prepare_recusa(raw, why):
    with pytest.raises(illustration.Refused, match=why):
        illustration.prepare(raw)


def test_prepare_recusa_animada():
    buf = io.BytesIO()
    frames = [Image.new("RGB", (1200, 700), c) for c in ((1, 2, 3), (200, 2, 3))]
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:])
    with pytest.raises(illustration.Refused, match="animada"):
        illustration.prepare(buf.getvalue())


# --- IA: o que nunca se gera -----------------------------------------------------------------

@pytest.mark.parametrize("concept", [
    "Lula giving a speech at a podium",                      # nome de político
    "a crowd of protesters in front of a building",           # pessoas / notícia
    "the president signing a law",                            # pessoa
    "glowing cubes inside the Microsoft logo",                # marca
    "a photorealistic burning building",                      # facto realista
    "cubos azuis brilhantes numa rede de nós",                               # não é inglês
    "short",                                                   # vazio
    "blue cubes with the word hello in it"])                  # texto
def test_prompt_de_ia_recusado(concept):
    with pytest.raises(illustration.Refused):
        illustration.check_ai_prompt(concept)


def test_prompt_de_ia_aceito_leva_o_estilo():
    prompt = illustration.check_ai_prompt("glowing blue cubes forming a shield over a network.")
    assert prompt.startswith(illustration.STYLE) and "no people" in prompt
    assert prompt.endswith("glowing blue cubes forming a shield over a network")


def test_ia_indisponivel_recusa_sem_chamar_o_gateway(monkeypatch):
    def unavailable(prompt, **kw):
        raise illustration.chain.ImageUnavailable("codex desligado")

    monkeypatch.setattr(illustration.chain, "run_image", unavailable)
    with pytest.raises(illustration.Refused, match="indisponível"):
        illustration.generate_ai("glowing blue cubes forming a shield")


def test_ia_valida_o_conceito_antes_de_chamar_o_gateway(monkeypatch):
    called = []
    monkeypatch.setattr(illustration.chain, "run_image", lambda *a, **k: called.append(1))
    with pytest.raises(illustration.Refused):
        illustration.generate_ai("a portrait of the president")
    assert called == []


def test_ia_devolve_os_bytes_do_gateway(monkeypatch):
    seen = {}

    def fake(prompt, **kw):
        seen["prompt"], seen["site"] = prompt, kw.get("site")
        return jpeg()

    monkeypatch.setattr(illustration.chain, "run_image", fake)
    raw = illustration.generate_ai("glowing blue cubes forming a shield", site="cloud")
    assert raw[:2] == b"\xff\xd8"
    assert seen["prompt"].startswith(illustration.STYLE) and seen["site"] == "cloud"


def test_generate_ai_liga_a_chamada_ao_job(monkeypatch):
    """Regressão (revisão E14): o `job_id` era aceito e ignorado — fora do pipeline
    (scripts/ai_cover.py) a linha de provider_usage ficava sem job."""
    seen = {}

    def fake(prompt, **kw):
        seen["job"] = illustration.quota._JOB.get()
        return b"\xff\xd8x"

    monkeypatch.setattr(illustration.chain, "run_image", fake)
    illustration.generate_ai("glowing blue cubes forming a shield", site="cloud", job_id=77)
    assert seen["job"] == 77
    illustration.generate_ai("glowing blue cubes forming a shield", site="cloud")
    assert seen["job"] is None


# --- o passo completo -----------------------------------------------------------------------

def _job() -> int:
    from datetime import timedelta

    from sqlalchemy import text

    from app.core.db import session_scope
    from app.services import sites as sites_svc
    with session_scope() as db:
        sites_svc.sync(db)
        return db.execute(text(
            """INSERT INTO publication_jobs
                 (site_key, local_date, slot_number, due_at_utc, produce_after_utc, status)
               VALUES ('cloud', '2026-10-05', 1, :d, :p, 'QUEUED') RETURNING id"""),
            {"d": NOW + timedelta(hours=3), "p": NOW}).scalar_one()


@pytest.fixture
def ai_pick(monkeypatch):
    """Gateway simulado; `ai_pick(n)` fixa a foto escolhida (-1: nenhuma serve)."""
    fake = FakeAI()
    monkeypatch.setattr(gateway, "_transport", httpx.MockTransport(fake))

    def choose(index: int) -> FakeAI:
        fake.pick = index
        return fake

    return choose


def _art(**kw) -> illustration.Illustration:
    base = dict(provider="openverse", license="cc0", license_url="u", source_url="s",
                author="Ana", title="t", credit="c", ai_generated=False, prompt="",
                alt_texts={"pt-BR": "a"}, captions={}, webp=b"w", width=1600, height=900,
                sha256="a" * 64)
    return illustration.Illustration(**{**base, **kw})


def test_escolhe_foto_com_licenca_e_guarda_a_prova(world, ai_pick):
    ai_pick(0)
    site = {"site_key": "cloud"}
    job = _job()
    path = illustration.illustrate(job, "Título", "Resumo", site=site)
    row = fetch("SELECT * FROM image_assets WHERE job_id = :j", j=job)[0]
    assert path and row["provider"] == "openverse" and row["license"] == "cc0"
    assert row["source_url"] == "https://img.test/ok.jpg" and row["author"] == "Ana"
    assert row["license_proof_url"].startswith("https://creativecommons.org/")
    assert row["ai_generated"] is False and len(row["sha256"]) == 64
    assert row["alt_texts"]["zh-CN"] and row["width"] == 1600
    assert row["captions"]["en"] == "Photo: Ana / Openverse / CC0"
    assert row["captions"]["pt-BR"].startswith("Foto: Ana") and "图片" in row["captions"]["zh-CN"]
    assert Image.open(path).format == "WEBP"


def test_sem_foto_boa_gera_ilustracao_com_legenda_de_ia(world, ai_pick, monkeypatch):
    ai_pick(-1)
    monkeypatch.setattr(illustration, "generate_ai", lambda concept, **kw: jpeg())
    job = _job()
    assert illustration.illustrate(job, "T", "R", site={"site_key": "cloud"})
    row = fetch("SELECT * FROM image_assets WHERE job_id = :j", j=job)[0]
    assert row["ai_generated"] is True and row["license"] == "ai-generated"
    assert row["captions"]["en"] == "AI-generated image"
    assert row["prompt"].startswith(illustration.STYLE)


def test_foto_que_falha_no_download_cai_para_a_ia(world, ai_pick, monkeypatch):
    ai_pick(0)
    world["files"].clear()            # nenhuma foto descarrega
    monkeypatch.setattr(illustration, "generate_ai", lambda concept, **kw: jpeg())
    job = _job()
    illustration.illustrate(job, "T", "R", site={"site_key": "cloud"})
    rows = fetch("SELECT ai_generated FROM image_assets WHERE job_id = :j", j=job)
    assert rows[0]["ai_generated"] is True


def test_foto_recusada_e_substituida_por_outra_foto(world, ai_pick, monkeypatch):
    fake = ai_pick(0)                  # escolhe sempre a primeira da lista restante
    world["files"]["https://img.test/ok.jpg"] = jpeg(500, 300)       # pequena: recusada
    job = _job()
    illustration.illustrate(job, "T", "R", site={"site_key": "cloud"})
    row = fetch("SELECT provider, source_url, ai_generated FROM image_assets WHERE job_id = :j",
                j=job)[0]
    assert (row["provider"], row["source_url"], row["ai_generated"]) == (
        "commons", "https://upload.test/dc.jpg", False)
    assert len(fake.of("pick")) == 2


def test_capa_com_titulo_em_ingles_sai_numa_so_chamada(world, ai_pick):
    """P4 do ensaio E14 (3/11): com o título em inglês (o pipeline o tem, da tradução), a
    primeira leva de fotos vem das palavras do título — sem chamada — e plano+escolha saem
    numa chamada só, em vez de illustration_plan + illustration_pick."""
    fake = ai_pick(0)
    job = _job()
    path = illustration.illustrate(job, "Título", "Resumo", site={"site_key": "cloud"},
                                   title_en="Flu vaccination campaign expands nationwide")
    assert path
    row = fetch("SELECT provider, ai_generated, alt_texts FROM image_assets "
                "WHERE job_id = :j", j=job)[0]
    assert row["provider"] == "openverse" and row["ai_generated"] is False
    assert row["alt_texts"]["zh-CN"]
    assert [c["kind"] for c in fake.calls] == ["plan_pick"]


def test_capa_fundida_sem_foto_boa_tenta_as_pesquisas_do_modelo(world, ai_pick):
    """Se nada da primeira leva serve (`pick` = -1), as `queries` da mesma resposta alimentam
    uma segunda leva, escolhida pelo caminho antigo — no pior caso ficam as 2 chamadas de hoje."""
    fake = ai_pick([-1, 0])
    job = _job()
    illustration.illustrate(job, "Título", "Resumo", site={"site_key": "cloud"},
                            title_en="Flu vaccination campaign expands nationwide")
    row = fetch("SELECT ai_generated FROM image_assets WHERE job_id = :j", j=job)[0]
    assert row["ai_generated"] is False
    assert [c["kind"] for c in fake.calls] == ["plan_pick", "pick"]


def test_keyword_queries_tira_stopwords_e_nao_repete():
    queries = illustration._keyword_queries(
        "The flu vaccination campaign is expanded in Brazil")
    assert queries[0] == "flu vaccination campaign expanded"
    assert all(" the " not in f" {q} " and " is " not in f" {q} " for q in queries)
    assert len(queries) == len(set(queries)) and len(queries) <= 3
    assert illustration._keyword_queries("") == []


def test_nada_serve_nao_bloqueia(world, ai_pick):
    ai_pick(-1)                       # sem token do Workers AI
    job = _job()
    assert illustration.illustrate(job, "T", "R", site={"site_key": "cloud"}) is None
    assert fetch("SELECT 1 FROM image_assets WHERE job_id = :j", j=job) == []


def test_desligada_nao_faz_pedidos(world, monkeypatch):
    monkeypatch.setattr(settings, "illustration_enabled", False)
    assert illustration.illustrate(_job(), "T", "R", site={"site_key": "cloud"}) is None
    assert world["calls"] == []


def test_ilustrar_de_novo_substitui_a_linha(world, ai_pick):
    ai_pick(0)
    job = _job()
    illustration.illustrate(job, "T", "R", site={"site_key": "cloud"})
    world["openverse"] = {}
    illustration.save(job, _art(sha256="b" * 64))
    assert len(fetch("SELECT 1 FROM image_assets WHERE job_id = :j", j=job)) == 1
