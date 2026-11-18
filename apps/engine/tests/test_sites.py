"""E4.1/E4.2 — esquema multi-site e configuração dos seis sites."""
from __future__ import annotations

import pathlib

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core import migrate
from app.core.db import engine, session_scope
from app.services import sites
from tests.conftest import fetch

EXPECTED = ["cloud", "ia", "financas", "saude", "politica", "culinaria"]
VALID = """
site_key: teste
name: "Blog de teste"
base_url: https://teste.shortsblog.com
wordpress_api_url: https://teste.shortsblog.com/wp-json
"""


def write(directory: pathlib.Path, name: str, body: str) -> None:
    (directory / name).write_text(body)


# --- Os ficheiros reais (content-config/sites) ---------------------------------------


def test_os_seis_sites_reais_sao_validos():
    loaded = sites.load_all()
    assert sorted(s.site_key for s in loaded) == sorted(EXPECTED)
    by_key = {s.site_key: s for s in loaded}
    # Desfasamentos da spec §5.2, um por site, pela ordem do piloto.
    assert [by_key[k].slot_offset_minutes for k in EXPECTED] == [0, 7, 14, 21, 28, 35]
    for site in loaded:
        assert site.base_url == f"https://{site.site_key}.shortsblog.com"
        assert site.wordpress_api_url == f"{site.base_url}/wp-json"
        assert site.timezone == "Europe/Lisbon" and site.language == "pt-BR"  # D-066
        assert site.languages == ["pt-BR", "en", "es", "zh-CN"]
        assert site.daily_target == 5
        # Nenhum site publica antes de o WordPress existir (E8) e o piloto arrancar (E14).
        assert site.enabled is False


def test_sites_reais_refletem_o_piloto():
    """Os ficheiros de `content-config/` de verdade (os outros testes partem de tudo desligado)."""
    from tests import conftest

    real = {s.site_key: s for s in conftest._REAL_LOAD_ALL()}
    assert real["cloud"].enabled is True and real["cloud"].daily_target == 1  # E14.1, D-057
    assert real["ia"].enabled is True and real["ia"].daily_target == 1  # E14.3, D-067
    assert real["culinaria"].enabled is True and real["culinaria"].daily_target == 1  # E14.3, 26/10
    assert real["financas"].enabled is True and real["financas"].daily_target == 1  # D-091, 29/10
    assert real["saude"].enabled is True and real["saude"].daily_target == 1  # D-091, 1/11
    assert real["politica"].enabled is True and real["politica"].daily_target == 1  # D-091, 8/11
    # Com os 6 blogs ligados a 1 slot/dia, não resta site desligado para excluir.


def test_sync_copia_para_a_tabela_e_e_idempotente():
    for _ in range(2):
        with session_scope() as db:
            assert sorted(sites.sync(db)) == sorted(EXPECTED)
    rows = fetch("SELECT site_key, slot_offset_minutes, enabled, config FROM sites "
                 "ORDER BY slot_offset_minutes")
    assert [r["site_key"] for r in rows] == EXPECTED
    assert all(r["enabled"] is False for r in rows)
    assert rows[0]["config"]["description"]  # o que não é coluna fica em `config`
    with session_scope() as db:
        assert sites.enabled(db) == []
        assert sites.get(db, "cloud")["name"] == "Cloud e Infraestrutura"
        assert sites.get(db, "inexistente") is None


def test_migrate_sincroniza_os_sites(monkeypatch):
    monkeypatch.delenv("SB_DB_APP_PASSWORD", raising=False)
    assert migrate.main() == 0
    assert len(fetch("SELECT 1 FROM sites")) == 6


def test_migrate_trava_com_configuracao_invalida(tmp_path, monkeypatch):
    monkeypatch.delenv("SB_DB_APP_PASSWORD", raising=False)
    write(tmp_path, "teste.yaml", VALID.replace("https://teste.shortsblog.com\n", "http://x\n"))
    from app.core.config import settings

    monkeypatch.setattr(settings, "sites_config_dir", str(tmp_path))
    with pytest.raises(SystemExit):
        migrate.main()


# --- Validação -----------------------------------------------------------------------------


def test_site_minimo_valido(tmp_path):
    write(tmp_path, "teste.yaml", VALID)
    (site,) = sites.load_all(tmp_path)
    assert (site.enabled, site.daily_target, site.slot_offset_minutes) == (False, 5, 0)


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        (("base_url: https://teste.shortsblog.com", "base_url: http://teste.shortsblog.com"),
         "https"),
        (("site_key: teste", "site_key: Outro Nome"), "site_key"),
        (("site_key: teste", "site_key: outro"), "diferente do nome do ficheiro"),
        (("name: \"Blog de teste\"", "name: \"Blog\"\ntimezone: Marte/Olympus"), "fuso horário"),
        (("name: \"Blog de teste\"", "name: \"Blog\"\nlanguages: [pt-BR, fr]"), "não suportados"),
        (("name: \"Blog de teste\"", "name: \"Blog\"\nlanguages: [en, es]"), "idioma principal"),
        (("name: \"Blog de teste\"", "name: \"Blog\"\ndaily_target: 9"), "daily_target"),
        (("name: \"Blog de teste\"", "name: \"Blog\"\nwordpress_app_password: abc"), "segredo"),
        (("name: \"Blog de teste\"", "name: \"Blog\"\napi_token: abc"), "segredo"),
        # a qualquer profundidade (revisão E4 #8)
        (("name: \"Blog de teste\"", "name: \"Blog\"\nwordpress:\n  app_password: abc"),
         "segredo"),
        (("name: \"Blog de teste\"",
          "name: \"Blog\"\ncontas:\n  - nome: x\n    extra: {api_key: abc}"), "segredo"),
    ],
)
def test_ficheiro_invalido(tmp_path, change, fragment):
    write(tmp_path, "teste.yaml", VALID.replace(*change))
    with pytest.raises(sites.SiteConfigError, match=fragment) as info:
        sites.load_all(tmp_path)
    assert "teste.yaml" in str(info.value)


def test_yaml_que_nao_e_mapa_ou_esta_partido(tmp_path):
    write(tmp_path, "teste.yaml", "- uma\n- lista\n")
    with pytest.raises(sites.SiteConfigError, match="mapa YAML"):
        sites.load_all(tmp_path)
    write(tmp_path, "teste.yaml", "site_key: [aberto\n")
    with pytest.raises(sites.SiteConfigError, match="teste.yaml"):
        sites.load_all(tmp_path)
    with pytest.raises(sites.SiteConfigError, match="não encontrado"):
        sites.load_all(tmp_path / "nao-existe")


def test_dois_sites_com_o_mesmo_desfasamento(tmp_path):
    write(tmp_path, "teste.yaml", VALID)
    write(tmp_path, "outro.yaml", VALID.replace("teste", "outro"))
    with pytest.raises(sites.SiteConfigError, match="slot_offset_minutes"):
        sites.load_all(tmp_path)


def test_site_sem_ficheiro_e_desligado_e_nao_apagado(tmp_path):
    write(tmp_path, "teste.yaml", VALID + "enabled: true\n")
    with session_scope() as db:
        sites.sync(db, tmp_path)
        assert [s["site_key"] for s in sites.enabled(db)] == ["teste"]
    (tmp_path / "teste.yaml").unlink()
    with session_scope() as db:
        assert sites.sync(db, tmp_path) == []
    assert [dict(r) for r in fetch("SELECT site_key, enabled FROM sites")] == [
        {"site_key": "teste", "enabled": False}]


# --- Esquema (E4.1) --------------------------------------------------------------------------


@pytest.fixture
def cloud():
    with session_scope() as db:
        sites.sync(db)
    return "cloud"


def insert_job(site: str, slot: int = 1, status: str = "PLANNED") -> int:
    with session_scope() as db:
        return db.execute(
            text("""INSERT INTO publication_jobs
                      (site_key, local_date, slot_number, due_at_utc, produce_after_utc, status)
                    VALUES (:s, '2026-10-05', :n, '2026-10-05 11:30+00', '2026-10-05 08:30+00',
                            :st) RETURNING id"""),
            {"s": site, "n": slot, "st": status},
        ).scalar_one()


def test_tabelas_da_e4_existem():
    names = {r["table_name"] for r in fetch(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'")}
    assert {"sites", "sources", "ideas", "publication_jobs", "job_translations",
            "article_evidence", "image_assets", "approvals", "publication_events",
            "daily_digest", "provider_usage"} <= names


def test_slot_unico_por_site_e_dia(cloud):
    insert_job(cloud, 1)
    insert_job(cloud, 2)
    insert_job("ia", 1)
    with pytest.raises(IntegrityError):
        insert_job(cloud, 1)


@pytest.mark.parametrize(("slot", "status"), [(0, "PLANNED"), (6, "PLANNED"),
                                              (1, "READY_TO_PUBLISH"), (1, "planned")])
def test_slot_e_estado_invalidos(cloud, slot, status):
    with pytest.raises(IntegrityError):
        insert_job(cloud, slot, status)


def test_todos_os_estados_da_e4_sao_aceites(cloud):
    states = ["PLANNED", "QUEUED", "CLAIMED", "RESEARCHING", "WRITING", "VALIDATING",
              "TRANSLATING", "AWAITING_APPROVAL", "APPROVED", "PUBLISHING", "VERIFY_REQUIRED",
              "RETRY_WAIT", "PUBLISHED_CONFIRMED", "BLOCKED", "FAILED", "REJECTED", "EXPIRED"]
    assert len(states) == 17
    job = insert_job(cloud)
    with session_scope() as db:
        for state in states:
            db.execute(text("UPDATE publication_jobs SET status = :s WHERE id = :i"),
                       {"s": state, "i": job})


def test_uma_traducao_por_idioma_e_so_os_quatro_idiomas(cloud):
    job = insert_job(cloud)
    with session_scope() as db:
        for lang in ("pt-BR", "en", "es", "zh-CN"):
            db.execute(text("INSERT INTO job_translations (job_id, language) VALUES (:j, :l)"),
                       {"j": job, "l": lang})
    for lang in ("en", "fr"):
        with pytest.raises(IntegrityError), session_scope() as db:
            db.execute(text("INSERT INTO job_translations (job_id, language) VALUES (:j, :l)"),
                       {"j": job, "l": lang})


def test_post_do_wordpress_unico_por_site(cloud):
    first, second = insert_job(cloud, 1), insert_job(cloud, 2)
    other = insert_job("ia", 1)
    with session_scope() as db:
        db.execute(text("UPDATE publication_jobs SET wordpress_post_id = 42 WHERE id IN (:a, :b)"),
                   {"a": first, "b": other})  # mesmo id em sites diferentes: permitido
    with pytest.raises(IntegrityError), session_scope() as db:
        db.execute(text("UPDATE publication_jobs SET wordpress_post_id = 42 WHERE id = :i"),
                   {"i": second})


def test_ideia_repetida_no_mesmo_site_e_recusada(cloud):
    sql = text("""INSERT INTO ideas (site_key, origin, topic, canonical_url)
                  VALUES (:s, 'rss', 'tema', 'https://fonte.test/noticia')""")
    with session_scope() as db:
        db.execute(sql, {"s": cloud})
        db.execute(sql, {"s": "ia"})  # noutro blog é outra ideia (dedup global é da E6)
    with pytest.raises(IntegrityError), session_scope() as db:
        db.execute(sql, {"s": cloud})
    with pytest.raises(IntegrityError), session_scope() as db:
        db.execute(text("INSERT INTO ideas (site_key, origin, topic) VALUES (:s, 'email', 't')"),
                   {"s": cloud})


def test_apagar_um_job_leva_o_que_depende_dele(cloud):
    job = insert_job(cloud)
    with session_scope() as db:
        db.execute(text("INSERT INTO job_translations (job_id, language) VALUES (:j, 'pt-BR')"),
                   {"j": job})
        db.execute(text("INSERT INTO publication_events (job_id, event_type) "
                        "VALUES (:j, 'created')"), {"j": job})
        db.execute(text("INSERT INTO approvals (job_id, decision, decided_by, channel) "
                        "VALUES (:j, 'approved', 'euclides', 'panel')"), {"j": job})
        db.execute(text("INSERT INTO article_evidence (job_id, source_url) "
                        "VALUES (:j, 'https://fonte.test')"), {"j": job})
        db.execute(text("INSERT INTO image_assets (job_id, provider, license) "
                        "VALUES (:j, 'openverse', 'CC0')"), {"j": job})
        db.execute(text("DELETE FROM publication_jobs WHERE id = :j"), {"j": job})
    for table in ("job_translations", "publication_events", "approvals", "article_evidence",
                  "image_assets"):
        assert fetch(f"SELECT 1 FROM {table}") == [], table


def test_site_com_jobs_nao_pode_ser_apagado(cloud):
    insert_job(cloud)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("DELETE FROM sites WHERE site_key = :s"), {"s": cloud})


def test_decisao_e_canal_de_aprovacao_validados(cloud):
    job = insert_job(cloud)
    for decision, channel in (("talvez", "panel"), ("approved", "telefone")):
        with pytest.raises(IntegrityError), session_scope() as db:
            db.execute(text("INSERT INTO approvals (job_id, decision, decided_by, channel) "
                            "VALUES (:j, :d, 'x', :c)"), {"j": job, "d": decision, "c": channel})
