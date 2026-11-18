"""E9 — publicador por site, contra um WordPress de mentira (nenhum pedido sai da máquina)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import text

from app.core.db import session_scope
from app.services import sitepub
from tests.conftest import advance, fetch
from tests.test_sites import cloud, insert_job  # noqa: F401  (fixture `cloud`)

BASE = "https://cloud.shortsblog.com"
SLUG = {"pt-BR": "pt", "en": "en", "es": "es", "zh-CN": "zh"}


class FakeWP:
    """Guarda posts, mídias e categorias; `fail` injeta o comportamento de um pedido."""

    def __init__(self) -> None:
        self.posts: dict[int, dict] = {}
        self.media: dict[int, dict] = {}
        self.requests: list[tuple[str, str]] = []
        self.next_id = 100
        self.on_post: str | None = None  # "timeout_after_create" | "timeout" | "401" | "429"
        self.link_host = "cloud.shortsblog.com"
        self.categories = [
            {"id": 5, "name": "Segurança", "translations": {"pt": 5, "en": 6, "es": 7, "zh": 8}}
        ]

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/wp-json")
        self.requests.append((request.method, path))
        q = dict(request.url.params)
        body = (
            json.loads(request.content)
            if request.headers.get("content-type", "").startswith("application/json")
            and request.content
            else {}
        )
        assert request.headers["authorization"].startswith("Basic ")
        if path == "/wp/v2/categories":
            if q.get("context") == "edit":  # o papel author não pode (visto no WordPress real)
                return httpx.Response(403, json={"code": "rest_forbidden_context"})
            return httpx.Response(
                200,
                json=[
                    c
                    for c in self.categories
                    if q.get("search", "").casefold() in c["name"].casefold()
                ],
            )
        if path == "/wp/v2/media" and request.method == "GET":
            if "status" in q:  # o WordPress real recusa `status=any` nas mídias (2026-10-07)
                return httpx.Response(400, json={"code": "rest_invalid_param"})
            found = [m for m in self.media.values() if m.get("slug") == q.get("slug")]
            return httpx.Response(200, json=found)
        if path == "/wp/v2/media" and request.method == "POST":
            mid = self._id()
            name = re.search(r'filename="([^"]+)"', request.headers["content-disposition"])[1]
            self.media[mid] = {"id": mid, "bytes": len(request.content),
                               "slug": name.rsplit(".", 1)[0],
                               "source_url":
                                   f"https://{self.link_host}/wp-content/uploads/{name}"}
            return httpx.Response(201, json=self.media[mid])
        if m := re.fullmatch(r"/wp/v2/media/(\d+)", path):
            self.media[int(m[1])].update(body)
            return httpx.Response(200, json=self.media[int(m[1])])
        if path == "/wp/v2/posts" and request.method == "GET":
            # Como o Polylang gratuito: o filtro `lang` é ignorado.
            found = [p for p in self.posts.values() if p["sb_job_id"] == int(q["sb_job_id"])]
            return httpx.Response(200, json=found)
        if path == "/wp/v2/posts" and request.method == "POST":
            mode, self.on_post = (
                self.on_post,
                None if self.on_post in ("timeout", "timeout_after_create") else self.on_post,
            )
            if mode in ("401", "429"):
                return httpx.Response(int(mode), headers={"Retry-After": "120"}, json={})
            if mode == "timeout":
                raise httpx.ReadTimeout("lento", request=request)
            pid = self._id()
            slug = body.get("slug", "")
            taken = {p.get("slug") for p in self.posts.values()}
            while slug in taken:  # o WordPress torna o slug único (visto no cloud: -2, -3…)
                slug = f"{body['slug']}-{len(taken)}"
            self.posts[pid] = {
                **body,
                "slug": slug,
                "id": pid,
                "link": f"https://{self.link_host}/{body['lang']}/p{pid}/",
                "translations": {},
            }
            if mode == "timeout_after_create":
                raise httpx.ReadTimeout("lento", request=request)
            return httpx.Response(201, json=self.posts[pid])
        if m := re.fullmatch(r"/wp/v2/posts/(\d+)", path):
            post = self.posts[int(m[1])]
            if request.method == "POST":
                post.update(body)
            return httpx.Response(200, json=post)
        return httpx.Response(404, json={})


@pytest.fixture
def wp(monkeypatch) -> FakeWP:
    fake = FakeWP()
    monkeypatch.setattr(sitepub, "_transport", httpx.MockTransport(fake))
    monkeypatch.setenv("SB_WP_CLOUD_APP_USER", "sb-publisher")
    monkeypatch.setenv("SB_WP_CLOUD_APP_PASSWORD", "abcd efgh")
    return fake


@pytest.fixture
def approved(cloud, tmp_path) -> int:  # noqa: F811
    job = insert_job(cloud, 1, "PLANNED")
    advance(
        job,
        "QUEUED",
        "CLAIMED",
        "RESEARCHING",
        "WRITING",
        "VALIDATING",
        "TRANSLATING",
        "AWAITING_APPROVAL",
        "APPROVED",
    )
    image = tmp_path / "capa.webp"
    image.write_bytes(b"RIFF....WEBPfoto")
    with session_scope() as db:
        db.execute(
            text("UPDATE publication_jobs SET review_report = CAST(:r AS JSONB) WHERE id = :i"),
            {"r": json.dumps({"category": "Segurança"}), "i": job},
        )
        for lang in SLUG:
            db.execute(
                text(
                    "INSERT INTO job_translations (job_id, language, is_original, title, slug, "
                    "excerpt, html, status) VALUES (:j, :l, :o, :t, :s, 'resumo', '<p>corpo</p>', "
                    "'ready')"
                ),
                {
                    "j": job,
                    "l": lang,
                    "o": lang == "pt-BR",
                    "t": f"Título {lang}",
                    "s": f"titulo-{SLUG[lang]}",
                },
            )
        db.execute(
            text(
                "INSERT INTO image_assets (job_id, provider, license, license_proof_url, "
                "local_path, alt_texts, captions) VALUES (:j, 'openverse', 'CC0', "
                "'https://openverse.org/x', :p, CAST(:a AS JSONB), CAST(:c AS JSONB))"
            ),
            {
                "j": job,
                "p": str(image),
                "a": json.dumps({k: f"alt {k}" for k in SLUG}),
                "c": json.dumps({k: f"legenda {k}" for k in SLUG}),
            },
        )
    return job


def status(job: int) -> str:
    return fetch("SELECT status FROM publication_jobs WHERE id = :i", i=job)[0]["status"]


def test_rascunhos_nos_4_idiomas_ligados_e_sem_mudar_o_estado(wp, approved):
    result = sitepub.publish_job(approved, mode="draft")
    assert sorted(result.posts) == sorted(SLUG) and sorted(result.created) == sorted(SLUG)
    assert {p["status"] for p in wp.posts.values()} == {"draft"}
    pt = wp.posts[result.posts["pt-BR"]]
    assert pt["translations"] == {SLUG[k]: v for k, v in result.posts.items()}
    assert {p["sb_job_id"] for p in wp.posts.values()} == {approved}
    # Categoria no idioma de cada post; imagem própria com alt e legenda por idioma.
    assert {p["lang"]: p["categories"] for p in wp.posts.values()} == {
        "pt": [5],
        "en": [6],
        "es": [7],
        "zh": [8],
    }
    assert sorted(m["alt_text"] for m in wp.media.values()) == sorted(f"alt {k}" for k in SLUG)
    # D-099: a URL pública de cada capa fica gravada para a newsletter reutilizar.
    saved = fetch("SELECT wordpress_media_urls FROM image_assets WHERE job_id = :i",
                  i=approved)[0]["wordpress_media_urls"]
    assert sorted(saved) == sorted(SLUG)
    assert all(u.startswith("https://") and f"sb-{approved}-" in u for u in saved.values())
    assert status(approved) == "APPROVED"
    assert fetch(
        "SELECT 1 FROM publication_events WHERE job_id = :i AND event_type = 'drafts_created'",
        i=approved,
    )


def test_segunda_execucao_nao_cria_nada(wp, approved):
    sitepub.publish_job(approved, mode="draft")
    again = sitepub.publish_job(approved, mode="draft")
    assert again.created == [] and len(wp.posts) == 4 and len(wp.media) == 4


def test_timeout_depois_de_criar_nao_duplica(wp, approved):
    wp.on_post = "timeout_after_create"
    sitepub.publish_job(approved, mode="draft")
    assert len(wp.posts) == 4  # o pt-BR criado antes do timeout foi encontrado pela consulta


def test_live_publica_verifica_e_confirma(wp, approved):
    result = sitepub.publish_job(approved, mode="live")
    assert result.outcome == "PUBLISHED_CONFIRMED" and status(approved) == "PUBLISHED_CONFIRMED"
    assert {p["status"] for p in wp.posts.values()} == {"publish"}
    job = fetch(
        "SELECT wordpress_post_id, wordpress_url, published_at_utc FROM publication_jobs "
        "WHERE id = :i",
        i=approved,
    )[0]
    assert job["wordpress_post_id"] == result.posts["pt-BR"] and job["wordpress_url"].startswith(
        BASE
    )
    assert {
        r["status"]
        for r in fetch("SELECT status FROM job_translations WHERE job_id = :i", i=approved)
    } == {"published"}


def test_timeout_sem_post_fica_em_verificacao_e_a_repeticao_reconcilia(wp, approved):
    wp.on_post = "timeout"
    assert sitepub.publish_job(approved, mode="live").outcome == "VERIFY_REQUIRED"
    assert status(approved) == "VERIFY_REQUIRED" and wp.posts == {}
    assert sitepub.publish_job(approved, mode="live").outcome == "PUBLISHED_CONFIRMED"
    assert len(wp.posts) == 4


def test_401_suspende_so_este_site(wp, approved, cloud):  # noqa: F811
    wp.on_post = "401"
    assert sitepub.publish_job(approved, mode="live").outcome == "RETRY_WAIT"
    assert status(approved) == "RETRY_WAIT"
    assert fetch(
        "SELECT 1 FROM publication_events WHERE job_id = :i AND event_type = 'site_suspended'",
        i=approved,
    )
    # Outro job do mesmo site é recusado sem tocar no WordPress; a tabela `sites` não muda.
    other = insert_job(cloud, 2, "PLANNED")
    advance(
        other,
        "QUEUED",
        "CLAIMED",
        "RESEARCHING",
        "WRITING",
        "VALIDATING",
        "TRANSLATING",
        "AWAITING_APPROVAL",
        "APPROVED",
    )
    requests = len(wp.requests)
    with pytest.raises(sitepub.PublishError, match="suspenso"):
        sitepub.publish_job(other, mode="live")
    assert len(wp.requests) == requests


def test_429_espera_o_retry_after(wp, approved):
    wp.on_post = "429"
    before = datetime.now(UTC)
    assert sitepub.publish_job(approved, mode="live").outcome == "RETRY_WAIT"
    wait = fetch("SELECT retry_after_utc FROM publication_jobs WHERE id = :i", i=approved)[0][
        "retry_after_utc"
    ]
    assert 100 < (wait - before).total_seconds() < 140


def test_link_fora_do_hostname_nao_confirma(wp, approved):
    wp.link_host = "outro.exemplo.test"
    assert sitepub.publish_job(approved, mode="live").outcome == "RETRY_WAIT"
    assert status(approved) == "RETRY_WAIT"


def test_so_publica_jobs_aprovados(wp, cloud):  # noqa: F811
    job = insert_job(cloud, 2, "PLANNED")
    with pytest.raises(sitepub.PublishError):
        sitepub.publish_job(job, mode="live")
    assert wp.requests == []


def test_sem_credencial_nao_escreve_nada(wp, approved, monkeypatch):
    monkeypatch.delenv("SB_WP_CLOUD_APP_PASSWORD")
    assert sitepub.publish_job(approved, mode="live").outcome == "RETRY_WAIT"
    assert wp.requests == []


def test_imagem_sem_prova_de_licenca_nao_se_publica(wp, approved):
    with session_scope() as db:
        db.execute(
            text("UPDATE image_assets SET license_proof_url = NULL WHERE job_id = :i"),
            {"i": approved},
        )
    assert sitepub.publish_job(approved, mode="live").outcome == "RETRY_WAIT"
    assert wp.posts == {}


def test_destino_vem_da_tabela_de_sites_e_nao_do_job(wp, approved):
    with session_scope() as db:
        db.execute(
            text(
                "UPDATE sites SET wordpress_api_url = 'https://evil.example/wp-json' "
                "WHERE site_key = 'cloud'"
            )
        )
    assert sitepub.publish_job(approved, mode="live").outcome == "RETRY_WAIT"
    assert wp.requests == []


def test_retry_after_aceita_segundos_com_teto_e_ignora_datas():
    assert sitepub._retry_after("120") == 120
    assert sitepub._retry_after("999999") == 3600
    assert sitepub._retry_after("Wed, 21 Oct 2026 07:28:00 GMT") == 60
    assert sitepub._retry_after(None) == 60


def test_api_sem_https_nao_recebe_a_credencial(wp, approved):
    with session_scope() as db:
        db.execute(
            text(
                "UPDATE sites SET wordpress_api_url = 'http://cloud.shortsblog.com/wp-json' "
                "WHERE site_key = 'cloud'"
            )
        )
    assert sitepub.publish_job(approved, mode="live").outcome == "RETRY_WAIT"
    assert wp.requests == []


def test_slug_de_pagina_ganha_sufixo_e_o_slug_real_fica_gravado(wp, approved):
    with session_scope() as db:
        db.execute(
            text(
                "UPDATE job_translations SET slug = 'sobre' WHERE job_id = :i "
                "AND language IN ('pt-BR', 'en')"
            ),
            {"i": approved},
        )
    sitepub.publish_job(approved, mode="draft")
    assert sorted(p["slug"] for p in wp.posts.values() if p["lang"] in ("pt", "en")) == [
        "sobre-artigo",
        "sobre-artigo-1",
    ]
    saved = {
        r["language"]: r["slug"]
        for r in fetch("SELECT language, slug FROM job_translations WHERE job_id = :i", i=approved)
    }
    assert saved["pt-BR"] == "sobre-artigo" and saved["es"] == "titulo-es"


def test_safe_slug():
    assert sitepub.safe_slug("privacidade") == "privacidade-artigo"
    assert sitepub.safe_slug("gitlab-alerta") == "gitlab-alerta"


def test_repeticao_depois_de_falha_nao_reenvia_a_midia(wp, approved):
    """R46a: a mídia já enviada (por nome) é reutilizada em vez de criar uma órfã nova."""
    sitepub.publish_job(approved)
    for post in list(wp.posts):
        del wp.posts[post]  # simula a falha antes de o post ficar gravado
    again = sitepub.publish_job(approved)
    assert len(again.created) == 4 and len(wp.media) == 4
    assert [m for m, p in wp.requests if p == "/wp/v2/media" and m == "POST"] == ["POST"] * 4


def test_dois_workers_no_mesmo_job_nao_duplicam(wp, approved):
    """R46b: com o bloqueio do job ocupado, a segunda publicação recusa e não toca no WordPress."""
    from app.core.cache import acquire_lock, release_lock
    assert acquire_lock(f"publish:{approved}", ttl=60)
    try:
        with pytest.raises(sitepub.PublishError, match="em curso"):
            sitepub.publish_job(approved)
    finally:
        release_lock(f"publish:{approved}")
    assert wp.requests == []
    sitepub.publish_job(approved)  # solto o bloqueio, publica
    assert len(wp.posts) == 4


def test_credencial_em_falta_nao_suspende_o_site_24h(wp, approved, monkeypatch):
    """R46d: falta de credencial é erro de configuração, não um 401 do WordPress."""
    monkeypatch.delenv("SB_WP_CLOUD_APP_PASSWORD")
    sitepub.publish_job(approved, mode="live")
    with session_scope() as db:
        n = db.execute(text("SELECT count(*) FROM publication_events "
                            "WHERE event_type = 'site_suspended'")).scalar()
    assert n == 0
