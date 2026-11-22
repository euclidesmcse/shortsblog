"""E18.3a entrega 1 (D-104): o banco é a fonte da verdade; o YAML semeia e se reimporta."""
from __future__ import annotations

import logging
import re

import pytest
from sqlalchemy import text

from app.core import migrate, reimport
from app.core.db import session_scope
from app.services import discovery, editorial, gates, pauta, sites, terms
from tests.conftest import fetch, login_as

FEED = "https://www.who.int/rss-feeds/news-english.xml"


def legacy_sync(common: bool = True, no_fetch: bool = True) -> None:
    """O que o deploy fazia até a E18.3a: sobrescrever tudo a partir do YAML. Com `common` e
    `no_fetch`, já com as regras comuns e o sem-leitura no banco (depois das entregas 2 e 3)."""
    with session_scope() as db:
        sites.sync(db)
    with session_scope() as db:
        discovery.sync_sources(db)
        pauta.sync(db)
        if common:
            editorial._write_policy_common(db, editorial._desired_policy_common())
        if no_fetch:
            editorial._write_no_fetch(db, [dict(e) for e in terms.load_file()])


def plan() -> list[editorial.Change]:
    with session_scope() as db:
        return editorial.plan(db)


def run(sql: str, **params) -> None:
    with session_scope() as db:
        db.execute(text(sql), params)


# --- equivalência com o estado atual da produção -----------------------------------------
def test_depois_da_sincronizacao_antiga_nada_a_reimportar():
    """A produção chega à E18.3a sincronizada pelo método antigo: o 1.º plano tem de ser vazio
    (senão o 1.º deploy anunciaria mudanças que não existem)."""
    legacy_sync()
    assert plan() == []


def test_primeiro_deploy_da_entrega_2_so_semeia_as_regras_comuns():
    """Produção depois da entrega 1: sem `editorial.policy_common`. O deploy da entrega 2 só
    acrescenta as regras comuns; nada mais muda (a política montada dos blogs já bate)."""
    legacy_sync(common=False, no_fetch=False)
    with session_scope() as db:
        added, pending = editorial.seed(db)
    assert [c.target for c in added] == ["policy_common:comum", "no_fetch:lista"]
    assert pending == [] and plan() == []


def test_primeiro_deploy_da_entrega_3_so_semeia_o_sem_leitura():
    """Produção depois da entrega 2: regras comuns no banco, sem-leitura ainda no arquivo."""
    legacy_sync(no_fetch=False)
    with session_scope() as db:
        added, pending = editorial.seed(db)
    assert [c.target for c in added] == ["no_fetch:lista"] and pending == []
    assert plan() == []


def test_base_vazia_o_deploy_semeia_tudo_como_adicao():
    with session_scope() as db:
        added, pending = editorial.seed(db)
    assert pending == []
    kinds = {c.entity for c in added}
    assert kinds == {"site", "source", "newsletters", "policy_common", "no_fetch"}
    assert len([c for c in added if c.entity == "site"]) == 6
    assert len(fetch("SELECT 1 FROM sources")) == len([c for c in added if c.entity == "source"])
    assert plan() == []
    # cada adição fica na auditoria, como importação do deploy
    assert fetch("SELECT count(*) AS n FROM audit_log WHERE action = 'config.import' "
                 "AND actor = 'deploy'")[0]["n"] == len(added)


# --- o deploy não sobrescreve -----------------------------------------------------------
def test_deploy_nao_sobrescreve_o_que_mudou_no_banco(caplog):
    legacy_sync()
    run("UPDATE sites SET daily_target = 3, config = jsonb_set(config, '{min_sources}', '4') "
        "WHERE site_key = 'cloud'")
    run("UPDATE sources SET tier = 9 WHERE site_key = 'saude' AND id = "
        "(SELECT min(id) FROM sources WHERE site_key = 'saude')")
    caplog.set_level(logging.WARNING)
    with session_scope() as db:
        added, pending = editorial.seed(db)
    assert added == []
    assert fetch("SELECT daily_target FROM sites WHERE site_key = 'cloud'")[0]["daily_target"] == 3
    targets = {c.target: c for c in pending}
    assert targets["site:cloud"].fields == ["daily_target", "config.min_sources"]
    assert any(c.entity == "source" and c.fields == ["tier"] for c in pending)


def test_migrate_avisa_das_pendencias_e_nao_trava(monkeypatch, caplog):
    monkeypatch.delenv("SB_DB_APP_PASSWORD", raising=False)
    monkeypatch.setattr(migrate, "setup_logging", lambda: None)  # deixa o caplog capturar
    legacy_sync()
    run("UPDATE sites SET name = 'Outro nome' WHERE site_key = 'ia'")
    caplog.set_level(logging.INFO)
    assert migrate.main() == 0
    assert fetch("SELECT name FROM sites WHERE site_key = 'ia'")[0]["name"] == "Outro nome"
    assert "esperam reimportação" in caplog.text and "site:ia" in caplog.text


def test_feed_novo_no_yaml_entra_no_deploy():
    legacy_sync()
    url = fetch("SELECT feed_url FROM sources WHERE site_key = 'saude' ORDER BY id LIMIT 1")[0]
    run("DELETE FROM sources WHERE feed_url = :u", u=url["feed_url"])
    with session_scope() as db:
        added, pending = editorial.seed(db)
    assert [c.kind for c in added] == ["add"] and pending == []
    assert fetch("SELECT enabled FROM sources WHERE feed_url = :u", u=url["feed_url"])[0]["enabled"]


# --- reimportar ---------------------------------------------------------------------------
def test_reimportar_aplica_e_grava_antes_e_depois():
    legacy_sync()
    run("UPDATE sites SET daily_target = 3 WHERE site_key = 'cloud'")
    changes = plan()
    with session_scope() as db:
        assert editorial.apply(db, changes, "euclides@example.com") == 1
    assert plan() == []
    assert fetch("SELECT daily_target FROM sites WHERE site_key = 'cloud'")[0]["daily_target"] == 5
    row = fetch("SELECT actor, target, details FROM audit_log WHERE action = 'config.import'")[0]
    assert (row["actor"], row["target"]) == ("euclides@example.com", "site:cloud")
    assert row["details"]["before"]["daily_target"] == 3
    assert row["details"]["after"]["daily_target"] == 5
    assert row["details"]["fields"] == ["daily_target"]


def test_feed_fora_do_yaml_e_desligado_na_reimportacao():
    legacy_sync()
    run("INSERT INTO sources (site_key, name, feed_url, domain, source_type, tier, lang, "
        "usage_notes, enabled) VALUES ('saude', 'Extra', :u, 'who.int', 'rss', 1, 'en', '', true)",
        u=FEED + "?extra")
    changes = plan()
    assert [(c.kind, c.entity) for c in changes] == [("disable", "source")]
    with session_scope() as db:
        editorial.apply(db, changes, "teste")
    assert fetch("SELECT enabled FROM sources WHERE feed_url = :u",
                 u=FEED + "?extra")[0]["enabled"] is False


def test_mudanca_de_politica_mostra_o_diff_do_texto():
    legacy_sync()
    run("UPDATE sites SET config = jsonb_set(config, '{policy}', "
        "to_jsonb(config->>'policy' || E'\\nRegra local inventada.')) WHERE site_key = 'saude'")
    change = next(c for c in plan() if c.target == "site:saude")
    assert change.fields == ["config.policy"]
    (name, diff), = change.text_diffs()
    assert name == "config.policy" and "-Regra local inventada." in diff


def test_marca_o_que_foi_editado_no_portal_depois_da_ultima_importacao():
    legacy_sync()
    run("UPDATE sites SET daily_target = 2 WHERE site_key = 'cloud'")
    run("INSERT INTO audit_log (actor, action, target) VALUES ('op', 'config.edit', 'site:cloud')")
    assert plan()[0].edited_in_portal is True
    run("INSERT INTO audit_log (actor, action, target, at) "
        "VALUES ('op', 'config.import', 'site:cloud', now() + interval '1 second')")
    assert plan()[0].edited_in_portal is False


def test_newsletters_ausentes_sao_semeadas_e_alteradas_ficam_pendentes():
    legacy_sync()
    run("DELETE FROM settings WHERE key = :k", k=pauta.SETTING_KEY)
    with session_scope() as db:
        added, _ = editorial.seed(db)
    assert [c.target for c in added] == ["newsletters:newsletters"]
    run("UPDATE settings SET value = '[]'::jsonb WHERE key = :k", k=pauta.SETTING_KEY)
    with session_scope() as db:
        _, pending = editorial.seed(db)
    assert [c.target for c in pending] == ["newsletters:newsletters"]


def test_impressao_digital_muda_com_o_plano():
    legacy_sync()
    assert editorial.fingerprint(plan()) == editorial.fingerprint(plan())
    run("UPDATE sites SET daily_target = 3 WHERE site_key = 'cloud'")
    first = editorial.fingerprint(plan())
    run("UPDATE sites SET daily_target = 4 WHERE site_key = 'cloud'")
    assert editorial.fingerprint(plan()) != first


# --- linha de comando ----------------------------------------------------------------------
def test_cli_mostra_e_so_aplica_com_apply(capsys):
    legacy_sync()
    run("UPDATE sites SET daily_target = 3 WHERE site_key = 'cloud'")
    assert reimport.main([]) == 0
    out = capsys.readouterr().out
    assert "[update] site:cloud" in out and "daily_target: 3  →  5" in out
    assert "Nada foi gravado" in out
    assert fetch("SELECT daily_target FROM sites WHERE site_key = 'cloud'")[0]["daily_target"] == 3
    fingerprint = re.search(r"Impressão deste diff: (\w+)", out).group(1)
    assert reimport.main(["--apply"]) == 2                       # sem a impressão: recusado
    assert reimport.main(["--apply", "--fingerprint=errada"]) == 2
    capsys.readouterr()
    assert reimport.main(["--apply", f"--fingerprint={fingerprint}"]) == 0
    assert "Aplicadas 1" in capsys.readouterr().out
    assert fetch("SELECT daily_target FROM sites WHERE site_key = 'cloud'")[0]["daily_target"] == 5
    assert fetch("SELECT actor FROM audit_log")[0]["actor"].startswith("cli:")


# --- tela (admin) ----------------------------------------------------------------------------
def test_tela_mostra_em_dia(panel_client):
    legacy_sync()
    page = panel_client.get("/dashboard/config")
    assert page.status_code == 200 and "em dia" in page.text


def test_tela_mostra_o_diff_e_aplica_o_plano_mostrado(panel_client):
    import re

    legacy_sync()
    run("UPDATE sites SET daily_target = 3 WHERE site_key = 'cloud'")
    page = panel_client.get("/dashboard/config").text
    assert "daily_target" in page and "Reimportar 1" in page
    fingerprint = re.search(r'name="fingerprint" value="([0-9a-f]+)"', page).group(1)
    stale = panel_client.post("/dashboard/config/import", data={"fingerprint": "0" * 20},
                              follow_redirects=False)
    assert "err=" in stale.headers["location"]
    assert fetch("SELECT daily_target FROM sites WHERE site_key = 'cloud'")[0]["daily_target"] == 3
    ok = panel_client.post("/dashboard/config/import", data={"fingerprint": fingerprint},
                           follow_redirects=False)
    assert "msg=" in ok.headers["location"]
    assert fetch("SELECT daily_target FROM sites WHERE site_key = 'cloud'")[0]["daily_target"] == 5
    assert fetch("SELECT actor FROM audit_log WHERE action = 'config.import'")[0]["actor"] == \
        "euclides@example.com"


def test_tela_com_yaml_invalido_mostra_o_erro(panel_client, tmp_path, monkeypatch):
    from app.core.config import settings

    legacy_sync()
    (tmp_path / "x.yaml").write_text("site_key: y\n", encoding="utf-8")
    monkeypatch.setattr(settings, "feeds_config_dir", str(tmp_path))
    page = panel_client.get("/dashboard/config").text
    assert "inválido" in page and 'name="fingerprint"' not in page


@pytest.mark.parametrize("method,path", [("get", "/dashboard/config"),
                                         ("post", "/dashboard/config/import")])
def test_operador_nao_entra_na_configuracao(client, method, path):
    login_as(client, "op@example.com", "operador")
    response = getattr(client, method)(path, follow_redirects=False)
    assert response.status_code == 403


def test_menu_do_admin_tem_configuracao(panel_client):
    assert 'href="/dashboard/config"' in panel_client.get("/").text


# --- E18.3a entrega 2: editar sites e políticas, desfazer, exportar -----------------------------
def site_form(**over) -> dict:
    """O formulário do blog saude como a tela o mostra (sem mudar nada)."""
    with session_scope() as db:
        site = editorial.site_values(db, "saude")
    cfg = site["config"]
    form = {"name": site["name"], "daily_target": str(site["daily_target"]),
            "slot_offset_minutes": str(site["slot_offset_minutes"]),
            "languages": site["languages"], "description": cfg.get("description", ""),
            "min_sources": str(cfg.get("min_sources")),
            "categories": "\n".join(cfg.get("categories", [])),
            "allow_domains": "\n".join(cfg.get("search_policy", {}).get("allow_domains", []))}
    if site["enabled"]:
        form["enabled"] = "true"
    if cfg.get("search_policy", {}).get("official_only"):
        form["official_only"] = "true"
    form.update(over)
    return form


def test_formulario_sem_mudanca_nao_grava_nada(panel_client):
    legacy_sync()
    response = panel_client.post("/dashboard/config/sites/saude", data=site_form(),
                                 follow_redirects=False)
    assert "Nada+mudou" in response.headers["location"]
    assert fetch("SELECT count(*) AS n FROM audit_log WHERE action = 'config.edit'")[0]["n"] == 0


def test_editar_blog_grava_valida_e_audita(panel_client):
    legacy_sync()
    page = panel_client.get("/dashboard/config/sites/saude").text
    assert "wordpress_api_url" in page and 'name="base_url"' not in page  # técnico só leitura
    response = panel_client.post("/dashboard/config/sites/saude", follow_redirects=False,
                                 data=site_form(daily_target="2", min_sources="3",
                                                allow_domains="who.int\nnih.gov"))
    assert "msg=" in response.headers["location"]
    row = fetch("SELECT daily_target, config FROM sites WHERE site_key = 'saude'")[0]
    assert row["daily_target"] == 2 and row["config"]["min_sources"] == 3
    assert row["config"]["search_policy"] == {"official_only": True,
                                              "allow_domains": ["who.int", "nih.gov"]}
    assert "policy" in row["config"]                      # a política não se perde
    log = fetch("SELECT actor, target, details FROM audit_log WHERE action = 'config.edit'")[0]
    assert (log["actor"], log["target"]) == ("euclides@example.com", "site:saude")
    assert set(log["details"]["fields"]) == {"daily_target", "config.min_sources",
                                             "config.search_policy"}
    # a reimportação agora avisa que vai sobrescrever uma edição do portal
    change = next(c for c in plan() if c.target == "site:saude")
    assert change.edited_in_portal


@pytest.mark.parametrize("over,erro", [
    ({"daily_target": "9"}, "daily_target"),
    ({"slot_offset_minutes": "x"}, "inválido"),
    ({"languages": ["en", "es"]}, "principal"),
    ({"name": ""}, "name"),
    ({"categories": "Notícias\napi_key: segredo"}, ""),
])
def test_edicao_invalida_e_recusada_sem_gravar(panel_client, over, erro):
    from urllib.parse import unquote

    legacy_sync()
    response = panel_client.post("/dashboard/config/sites/saude", data=site_form(**over),
                                 follow_redirects=False)
    location = unquote(response.headers["location"])
    if over.get("categories"):          # categoria com texto livre é válida
        assert "msg=" in location
        return
    assert "err=" in location and erro in location
    assert fetch("SELECT count(*) AS n FROM audit_log WHERE action = 'config.edit'")[0]["n"] == 0


def test_desfazer_volta_ao_antes_e_recusa_se_mudou_depois(panel_client):
    legacy_sync()
    original = fetch("SELECT daily_target FROM sites WHERE site_key = 'saude'")[0]["daily_target"]
    panel_client.post("/dashboard/config/sites/saude", data=site_form(daily_target="2"))
    panel_client.post("/dashboard/config/sites/saude", data=site_form(daily_target="3"))
    first, second = [r["id"] for r in fetch(
        "SELECT id FROM audit_log WHERE action = 'config.edit' ORDER BY id")]
    blocked = panel_client.post(f"/dashboard/config/history/{first}/undo", follow_redirects=False)
    assert "err=" in blocked.headers["location"]
    panel_client.post(f"/dashboard/config/history/{second}/undo")
    assert fetch("SELECT daily_target FROM sites WHERE site_key = 'saude'")[0]["daily_target"] == 2
    panel_client.post(f"/dashboard/config/history/{first}/undo")
    assert fetch("SELECT daily_target FROM sites WHERE site_key = 'saude'")[0]["daily_target"] \
        == original
    assert all(c.target != "site:saude" for c in plan())
    undos = fetch("SELECT details FROM audit_log WHERE action = 'config.undo' ORDER BY id")
    assert [u["details"]["undo_of"] for u in undos] == [second, first]


def test_desfazer_uma_reimportacao():
    legacy_sync()
    run("UPDATE sites SET daily_target = 3 WHERE site_key = 'cloud'")
    with session_scope() as db:
        editorial.apply(db, plan(), "teste")
    audit_id = fetch("SELECT id FROM audit_log WHERE action = 'config.import'")[0]["id"]
    with session_scope() as db:
        editorial.undo(db, audit_id, "teste")
    assert fetch("SELECT daily_target FROM sites WHERE site_key = 'cloud'")[0]["daily_target"] == 3


def test_regras_do_blog_editadas_remontam_a_politica(panel_client):
    legacy_sync()
    with session_scope() as db:
        common, owns = editorial.policies(db)
    assert all(v for v in owns.values())
    page = panel_client.get("/dashboard/config/policies").text
    assert "Regras comuns" in page and "MUNDO" in page
    panel_client.post("/dashboard/config/policies/cloud", data={"text": "Só nuvem.\r\nNada mais."})
    policy = fetch("SELECT config->>'policy' AS p FROM sites WHERE site_key = 'cloud'")[0]["p"]
    assert policy == common + "\n\nSó nuvem.\nNada mais."
    assert fetch("SELECT target FROM audit_log WHERE action = 'config.edit'")[0]["target"] == \
        "site:cloud"


def test_regras_comuns_editadas_remontam_os_seis_blogs_e_desfazem_juntas(panel_client):
    legacy_sync()
    with session_scope() as db:
        old, owns = editorial.policies(db)
    panel_client.post("/dashboard/config/policies/common", data={"text": "Regra comum nova."})
    rows = fetch("SELECT site_key, config->>'policy' AS p FROM sites ORDER BY site_key")
    assert all(r["p"] == "Regra comum nova.\n\n" + owns[r["site_key"]] for r in rows)
    log = fetch("SELECT target, details->>'via' AS via FROM audit_log "
                "WHERE action = 'config.edit' ORDER BY id")
    assert log[0]["target"] == "policy_common:comum" and log[0]["via"] is None
    assert {r["target"] for r in log[1:]} == {f"site:{k}" for k in owns}
    assert all(r["via"] == "policy_common" for r in log[1:])
    # o registro de cada blog não se desfaz sozinho; o do comum desfaz tudo
    site_entry = fetch("SELECT id FROM audit_log WHERE target = 'site:cloud'")[0]["id"]
    blocked = panel_client.post(f"/dashboard/config/history/{site_entry}/undo",
                                follow_redirects=False)
    assert "err=" in blocked.headers["location"]
    common_entry = fetch("SELECT id FROM audit_log WHERE target = 'policy_common:comum'")[0]["id"]
    panel_client.post(f"/dashboard/config/history/{common_entry}/undo")
    rows = fetch("SELECT site_key, config->>'policy' AS p FROM sites ORDER BY site_key")
    assert all(r["p"] == old + "\n\n" + owns[r["site_key"]] for r in rows)
    assert plan() == []


def test_politica_vazia_e_recusada(panel_client):
    legacy_sync()
    for path in ("/dashboard/config/policies/common", "/dashboard/config/policies/cloud"):
        response = panel_client.post(path, data={"text": "   "}, follow_redirects=False)
        assert "err=" in response.headers["location"]


def test_exportar_yaml_e_md(panel_client):
    import yaml

    legacy_sync()
    exported = panel_client.get("/dashboard/config/export/sites/saude.yaml")
    assert exported.status_code == 200 and "attachment" in exported.headers["content-disposition"]
    data = yaml.safe_load(exported.text)
    assert data["site_key"] == "saude" and "policy" not in data and data["min_sources"] == 2
    real = sites.load_all()
    assert next(s for s in real if s.site_key == "saude").name == data["name"]
    common = panel_client.get("/dashboard/config/export/policies/_comum.md").text
    assert common.strip() == editorial._desired_policy_common()
    assert panel_client.get("/dashboard/config/export/policies/nada.md").status_code == 404


@pytest.mark.parametrize("path", ["/dashboard/config/sites/saude", "/dashboard/config/policies"])
def test_operador_nao_edita(client, path):
    login_as(client, "op@example.com", "operador")
    assert client.get(path, follow_redirects=False).status_code == 403


# --- R44: número por extenso tem de chegar à tradução --------------------------------------


DOSE = "<p>Tome dois comprimidos a cada oito horas, por cinco dias.</p>"


@pytest.mark.parametrize(("language", "translated", "flagged"), [
    ("en", "<p>Take two tablets every three hours, for five days.</p>", "«oito» (8)"),
    ("en", "<p>Take two tablets every eight hours, for five days.</p>", ""),
    ("en", "<p>Take 2 tablets every 8 hours, for 5 days.</p>", ""),
    ("es", "<p>Tome dos comprimidos cada tres horas, durante cinco días.</p>", "«oito» (8)"),
    ("es", "<p>Tome dos comprimidos cada ocho horas, durante cinco días.</p>", ""),
    ("zh-CN", "<p>每三小时服用两片,持续五天。</p>", "«oito» (8)"),
    ("zh-CN", "<p>每八小时服用两片,持续五天。</p>", ""),
])
def test_numero_por_extenso_trocado_na_traducao(language, translated, flagged):
    found = gates.spelled_flaws(DOSE, translated, language)
    assert (found[0] if found else "").endswith(flagged) if flagged else found == []


def test_por_extenso_que_nao_e_numero_sozinho_nao_conta():
    original = ("<p>Os dois países somam vinte e dois estados e dois mil municípios, segundo o "
                "Rio de Janeiro e a fonte veja.abril.com.br.</p>")
    translated = "<p>Both countries have 22 states and 2,000 municipalities, per the source.</p>"
    assert gates.spelled_flaws(original, translated, "en") == []


def test_check_translation_acusa_o_numero_por_extenso_perdido():
    problems = gates.check_translation(DOSE, "<p>Take two tablets every three hours, for "
                                             "five days.</p>", language="en")
    assert any("«oito» (8)" in p for p in problems)
