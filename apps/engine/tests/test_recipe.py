"""D-080 — modo receita: a receita é original; as fontes só conferem a plausibilidade."""
# ruff: noqa: F811 — as fixtures `ai` e `web` vêm de test_production
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from sqlalchemy import text

from app.core.db import session_scope
from app.services import article, discovery, dispatcher, recipe, sites
from tests.conftest import fetch
from tests.test_production import NOW, FakeAI, ai, run, state, web  # noqa: F401

R1 = "https://receitas.test/bolo-de-cenoura"
STEPS = ["Pré-aqueça o forno a 180 °C e unte uma forma de 24 cm.",
         "Bata no liquidificador as cenouras, os ovos e o óleo até ficar liso.",
         "Misture o açúcar e a farinha, junte o fermento e leve ao forno por 40 minutos."]
SCHEMA = {"@context": "https://schema.org", "@graph": [{"@type": "Recipe", "name": "Bolo",
          "recipeIngredient": ["3 cenouras médias", "4 ovos", "1 xícara de óleo",
                               "2 xícaras de açúcar", "2 xícaras de farinha de trigo",
                               "1 colher de sopa de fermento"],
          "recipeInstructions": [{"@type": "HowToStep", "text": t} for t in STEPS],
          "totalTime": "PT1H", "recipeYield": "10 porções"}]}
PAGE = ("<html><head><title>Bolo de cenoura</title><script type='application/ld+json'>{ld}"
        "</script></head><body><article><p>{text}</p></article></body></html>")
PAGE_TEXT = "Receita clássica de bolo de cenoura fofinho para o café da tarde. " * 6


def good_recipe(**override: str) -> str:
    """Uma receita que passa em todos os gates; `override` troca o corpo de uma seção."""
    intro = ("<p>" + "O bolo de cenoura é um clássico das cozinhas brasileiras, macio e úmido, "
             "ótimo para o café da tarde e para as festas da família. " * 4 + "</p>")
    parts = {
        "Informações da receita": "<ul><li><strong>Rendimento:</strong> 10 porções</li>"
        "<li><strong>Tempo de preparo:</strong> 20 minutos</li>"
        "<li><strong>Tempo de cozimento:</strong> 40 minutos</li>"
        "<li><strong>Tempo total:</strong> 1 hora</li></ul>",
        "Material necessário": "<ul><li>Liquidificador</li><li>Forma de 24 cm</li>"
        "<li>Tigela grande</li></ul>",
        "Ingredientes": "<ul><li>3 cenouras médias (cerca de 400 g)</li><li>4 ovos grandes</li>"
        "<li>200 ml de óleo de soja</li><li>350 g de açúcar</li><li>300 g de farinha de trigo</li>"
        "<li>1 colher (sopa) de fermento em pó</li></ul>",
        "Modo de preparo": "<ol><li>Aqueça o forno a 180 °C e unte a forma com óleo e farinha de "
        "trigo, retirando o excesso.</li><li>No liquidificador, bata as cenouras, os ovos e o óleo "
        "por 3 minutos, até a mistura ficar lisa e alaranjada.</li><li>Em uma tigela, junte o "
        "açúcar e a farinha, despeje o creme de cenoura e mexa até incorporar; por último, "
        "acrescente o fermento.</li><li>Asse por 40 minutos, até que um palito saia limpo do "
        "centro.</li></ol>",
        "Dicas e substituições": "<ul><li>Troque o óleo por manteiga derretida.</li>"
        "<li>Para um bolo mais úmido, não passe de 40 minutos no forno.</li></ul>",
        "Onde encontrar os ingredientes": "<p>Todos os itens são vendidos em qualquer "
        "supermercado ou feira; o fermento fica na seção de farinhas e misturas.</p>",
        "Conservação": "<p>Guarde coberto em temperatura ambiente por até 2 dias.</p>",
        "Alérgenos e cuidados": "<p>Contém glúten e ovos. Cuidado ao tirar a forma quente do "
        "forno.</p>",
    }
    parts.update(override)
    return intro + "".join(f"<h2>{title}</h2>{body}" for title, body in parts.items())


def reference(**changes) -> recipe.Reference:
    ref = recipe.parse_recipe(PAGE.format(ld=json.dumps(SCHEMA), text=PAGE_TEXT))
    assert ref is not None
    return recipe.Reference("receitas.test", ref, changes.pop("text", PAGE_TEXT))


# --- Leitura do schema.org/Recipe e conferência ---------------------------------------------


def test_le_a_receita_estruturada_em_graph():
    ref = reference().recipe
    assert len(ref.ingredients) == 6 and ref.steps == 3
    assert ref.total_minutes == 60 and ref.oven_temps == (180,) and ref.yield_text == "10 porções"


def test_pagina_sem_receita_ou_com_pouca_coisa_nao_serve():
    assert recipe.parse_recipe("<html><body>nada</body></html>") is None
    small = {"@type": "Recipe", "recipeIngredient": ["1 ovo", "sal"], "recipeInstructions": ["a"]}
    page = PAGE.format(ld=json.dumps(small), text="x")
    assert recipe.parse_recipe(page) is None


def test_duracoes():
    assert recipe.iso_minutes("PT1H30M") == 90 and recipe.iso_minutes("P0DT45M") == 45
    assert recipe.iso_minutes("PT0M") is None and recipe.iso_minutes("amanhã") is None
    assert recipe.minutes_in("1 hora e 20 minutos") == 80
    assert recipe.minutes_in("1h30") == 90 and recipe.minutes_in("45 min") == 45


def test_receita_boa_passa():
    assert recipe.check_recipe(good_recipe(), [reference()]) == []


def test_faltam_secoes():
    html = good_recipe().replace("<h2>Conservação</h2>", "<h2>Guardar</h2>")
    assert any("faltam seções" in p and "Conservação" in p
               for p in recipe.check_recipe(html, []))


def test_ingrediente_sem_quantidade_e_ingrediente_que_nenhum_passo_usa():
    html = good_recipe(Ingredientes="<ul><li>3 cenouras médias</li><li>4 ovos</li>"
                       "<li>1 xícara de óleo</li><li>2 xícaras de açúcar</li>"
                       "<li>2 xícaras de farinha de trigo</li><li>chocolate meio amargo</li></ul>")
    problems = recipe.check_recipe(html, [])
    assert any("sem quantidade" in p and "chocolate" in p for p in problems)
    html = good_recipe(Ingredientes="<ul><li>3 cenouras médias</li><li>4 ovos</li>"
                       "<li>1 xícara de óleo</li><li>200 g de gengibre</li><li>5 g de canela</li>"
                       "<li>100 g de coco ralado</li></ul>")
    assert any("nenhum passo usa" in p for p in recipe.check_recipe(html, []))


def test_temperatura_fora_da_faixa_e_distante_das_fontes():
    hot = good_recipe().replace("180 °C", "450 °C")
    assert any("fora da faixa" in p for p in recipe.check_recipe(hot, []))
    far = good_recipe().replace("180 °C", "250 °C")
    assert any("as fontes ficam entre 180 e 180" in p
               for p in recipe.check_recipe(far, [reference()]))


def test_tempo_total_subestimado_contra_as_fontes_e_barrado_mas_o_maior_nao():
    short = good_recipe(**{"Informações da receita": "<ul><li>Rendimento: 10 porções</li>"
                           "<li>Tempo de preparo: 5 minutos</li>"
                           "<li>Tempo total: 10 minutos</li></ul>"})
    assert any("tempo total de 10 min" in p for p in recipe.check_recipe(short, [reference()]))
    # Noite na geladeira: as fontes dizem 60 min mas não contam o descanso (ensaio 2, pudim).
    long = good_recipe(**{"Informações da receita": "<ul><li>Rendimento: 10 porções</li>"
                          "<li>Tempo de preparo: 10 minutos</li>"
                          "<li>Tempo de descanso: 8 horas na geladeira</li>"
                          "<li>Tempo total: 8 horas e 10 minutos</li></ul>"})
    assert not any("tempo total" in p for p in recipe.check_recipe(long, [reference()]))


def test_passos_copiados_da_fonte_e_lista_de_ingredientes_igual():
    same_list = "<ul>" + "".join(f"<li>{i}</li>" for i in SCHEMA["@graph"][0]["recipeIngredient"]) \
        + "</ul>"
    copied = good_recipe(Ingredientes=same_list,
                         **{"Modo de preparo": "<ol>" + "".join(f"<li>{s}</li>" for s in STEPS)
                            + "</ol>"})
    ref = reference(text=" ".join(STEPS))
    problems = recipe.check_recipe(copied, [ref])
    assert any("parecidos demais" in p for p in problems)
    assert any("lista de ingredientes igual" in p for p in problems)


def test_prato_diferente_das_fontes_e_frase_proibida():
    other = good_recipe(Ingredientes="<ul><li>2 filés de peixe</li><li>1 limão</li>"
                        "<li>3 colheres de azeite</li><li>1 punhado de salsa</li></ul>")
    assert any("quase não coincidem" in p for p in recipe.check_recipe(other, [reference()]))
    tested = good_recipe(Conservação="<p>Nós testamos e fica perfeito por 2 dias.</p>")
    assert any("proibidas" in p for p in recipe.check_recipe(tested, []))


def test_aviso_nos_quatro_idiomas():
    for language in ("pt-BR", "en", "es", "zh-CN"):
        html = recipe.notice(["a.test", "b.test"], language)
        assert "a.test, b.test" in html and html.startswith("<p><em>")
    assert "2 receitas publicadas" in recipe.notice(["a.test", "b.test"], "pt-BR")
    assert "1 receita publicada" in recipe.notice(["a.test"], "pt-BR")


# --- Pipeline ---------------------------------------------------------------------------------


@pytest.fixture
def recipe_job(ai, web) -> tuple[int, str]:
    """Um job da Culinária (modo receita) com uma ideia cuja página traz a receita estruturada."""
    web[R1] = PAGE.format(ld=json.dumps(SCHEMA), text=PAGE_TEXT)
    ai.category, ai.title = "Doces", "Bolo de cenoura fofinho"
    ai.draft_html = [good_recipe()]
    ai.translations = {lang: [good_recipe()] for lang in ("en", "es", "zh-CN")}
    with session_scope() as db:
        sites.sync(db)
        db.execute(text("UPDATE sites SET enabled = true WHERE site_key = 'culinaria'"))
        job_id = db.execute(text(
            """INSERT INTO publication_jobs
                 (site_key, local_date, slot_number, due_at_utc, produce_after_utc, status)
               VALUES ('culinaria', '2026-10-05', 1, :d, :p, 'QUEUED') RETURNING id"""),
            {"d": NOW + timedelta(hours=3), "p": NOW}).scalar_one()
        discovery.store_idea(db, site_key="culinaria", origin="rss", topic="Bolo de cenoura",
                             url=R1, published_at=NOW, now=NOW)
        owner = dispatcher.new_owner()
        assert dispatcher.claim(db, owner, NOW) == job_id
    return job_id, owner


def test_culinaria_esta_em_modo_receita_no_arquivo_do_site():
    with session_scope() as db:
        sites.sync(db)
        assert sites.get(db, "culinaria")["config"]["recipe_mode"] is True
        assert not sites.get(db, "cloud")["config"].get("recipe_mode")


def test_receita_percorre_o_pipeline_sem_pesquisa_de_fatos(recipe_job, ai):
    assert run(recipe_job) == "AWAITING_APPROVAL"
    assert not ai.of("research")                       # as fontes só conferem
    prompt = ai.of("write")[0]["prompt"]
    assert "Texto da fonte" not in prompt and "clássica de bolo" not in prompt  # não recebe fontes
    rows = {r["language"]: r["html"] for r in fetch(
        "SELECT language, html FROM job_translations WHERE job_id = :j", j=recipe_job[0])}
    assert set(rows) == {"pt-BR", "en", "es", "zh-CN"}
    assert "não foi testada na nossa cozinha" in rows["pt-BR"]
    assert "has not been tested in our kitchen" in rows["en"]
    assert "receitas.test" in rows["es"] and "<h2>Fuentes y créditos</h2>" in rows["es"]
    evidence = fetch("SELECT claims_supported, usage_rights_note FROM article_evidence "
                     "WHERE job_id = :j", j=recipe_job[0])
    assert "6 ingredientes" in evidence[0]["claims_supported"]
    assert "conferência de plausibilidade" in evidence[0]["usage_rights_note"]
    report = fetch("SELECT review_report FROM publication_jobs WHERE id = :j", j=recipe_job[0])
    assert report[0]["review_report"]["rewrites"] == 0


def test_receita_incompleta_volta_ao_redator_com_o_parecer(recipe_job, ai):
    bad = good_recipe().replace("<h2>Conservação</h2>", "<h2>Guardar</h2>")
    ai.draft_html = [bad, good_recipe()]
    assert run(recipe_job) == "AWAITING_APPROVAL"
    assert len(ai.of("write")) == 2 and len(ai.of("review")) == 1
    assert "faltam seções" in ai.of("write")[1]["prompt"] + json.dumps(ai.of("write")[1])
    report = fetch("SELECT review_report FROM publication_jobs WHERE id = :j", j=recipe_job[0])
    assert report[0]["review_report"]["rewrites"] == 1


def test_receita_que_continua_copiando_a_fonte_bloqueia(recipe_job, ai, web):
    web[R1] = PAGE.format(ld=json.dumps(SCHEMA), text=" ".join(STEPS) * 3)
    copied = good_recipe(**{"Modo de preparo": "<ol>" + "".join(f"<li>{s}</li>" for s in STEPS)
                            + "</ol>"})
    ai.draft_html = [copied]
    assert run(recipe_job) == "BLOCKED"
    assert "parecidos demais" in state(recipe_job[0])["blocked_reason"]
    assert fetch("SELECT status FROM job_translations WHERE job_id = :j AND is_original",
                 j=recipe_job[0])[0]["status"] == "failed"


def test_sem_receita_estruturada_nenhuma_fonte_serve(recipe_job, ai, web):
    web[R1] = "<html><head><title>x</title></head><body><article><p>" + "texto " * 80 \
        + "</p></article></body></html>"
    assert run(recipe_job) == "BLOCKED"
    assert "fontes insuficientes: 0 de 1" in state(recipe_job[0])["blocked_reason"]
    assert not ai.of("write")


def test_redator_recusa_tema_que_nao_e_prato(recipe_job, ai):
    ai.declined = "o tema é uma notícia sobre um restaurante, não um prato"
    assert run(recipe_job) == "BLOCKED"
    assert "recusou o tema" in state(recipe_job[0])["blocked_reason"]


def test_triagem_do_modo_receita_pede_pratos(recipe_job, ai):
    run(recipe_job)
    triage = ai.of("triage")[0]["prompt"]
    assert "receita completa por dia" in triage and "MESMO prato" in triage


def test_links_no_texto_da_receita_sao_removidos(recipe_job, ai):
    ai.draft_html = [good_recipe(Conservação='<p>Veja <a href="https://x.test/y">aqui</a> '
                                 "como guardar por até 2 dias.</p>")]
    ai.translations = {lang: [good_recipe(Conservação="<p>Veja aqui como guardar por até 2 dias."
                                          "</p>")] for lang in ("en", "es", "zh-CN")}
    assert run(recipe_job) == "AWAITING_APPROVAL"
    html = fetch("SELECT html FROM job_translations WHERE job_id = :j AND is_original",
                 j=recipe_job[0])[0]["html"]
    assert "x.test" not in html and "Veja aqui como guardar" in html
    assert article.recipes is recipe                    # o mesmo módulo, sem cópia


# --- Ajustes depois do ensaio (2026-10-24) ------------------------------------------------------


def test_fonte_com_dados_fora_de_escala_nao_serve():
    huge = {"@type": "Recipe", "recipeIngredient": [f"{n} ovo" for n in range(1, 22)],
            "recipeInstructions": [f"Passo {n}." for n in range(1, 104)]}
    assert recipe.parse_recipe(PAGE.format(ld=json.dumps(huge), text="x")) is None


def test_tempo_total_menor_que_a_soma_dos_tempos_declarados():
    html = good_recipe(**{"Informações da receita": "<ul><li>Rendimento: 10 porções</li>"
                          "<li>Tempo de preparo: 25 minutos</li>"
                          "<li>Tempo de cozimento: 40 minutos</li>"
                          "<li>Tempo de descanso: 30 minutos</li>"
                          "<li>Tempo total: 1 hora e 10 minutos</li></ul>"})
    assert any("menor que a soma dos tempos declarados (95 min)" in p
               for p in recipe.check_recipe(html, []))
    ok = html.replace("1 hora e 10 minutos", "1 hora e 35 minutos")
    assert not any("tempo total" in p for p in recipe.check_recipe(ok, []))


def test_tempo_total_que_nao_cobre_os_tempos_dos_passos():
    steps = ("<ol><li>Deixe a massa descansar por 2 horas.</li><li>Asse por 40 a 50 minutos a "
             "180 °C.</li><li>Espere esfriar por 30 minutos.</li></ol>")
    html = good_recipe(**{"Modo de preparo": steps, "Informações da receita":
                          "<ul><li>Rendimento: 8 porções</li><li>Tempo de preparo: 10 minutos</li>"
                          "<li>Tempo total: 1 hora</li></ul>"})
    assert any("não cobre os tempos dos passos (cerca de 200 min" in p
               for p in recipe.check_recipe(html, []))


def test_tempos_do_ensaio_reais_passam():
    assert recipe.steps_minutes(["Asse por 35 a 40 minutos. Espere 10 minutos.", "1 hora"]) == 110
    assert recipe.steps_minutes(["Deixe de molho por 8 horas.", "Cozinhe por 30 minutos."]) == 30
    info = ["Rendimento: 4 porções", "Tempo de preparo: 20 minutos, mais 15 minutos de marinada",
            "Tempo de cozimento: 35 minutos", "Tempo total: 1 hora e 10 minutos"]
    assert recipe._total_time_problems(info, []) == []
    info = ["Tempo de preparo: 25 minutos (mais 8 horas de molho do grão)",
            "Tempo de cozimento: 40 minutos (bolo) e 5 minutos (cobertura)",
            "Tempo total: 1 hora e 40 minutos, incluindo o resfriamento"]
    assert recipe._total_time_problems(info, []) == []


def test_prompts_do_modo_receita_trazem_as_regras_novas(recipe_job, ai):
    run(recipe_job)
    assert "SOMA de preparo" in ai.of("write")[0]["prompt"]
    review = ai.of("review")[0]["prompt"]
    assert "meia xícara de raspas de laranja" in review and "só pela cor" in review


def test_motivo_de_cada_reescrita_fica_no_relatorio(recipe_job, ai):
    ai.draft_html = [good_recipe().replace("<h2>Conservação</h2>", "<h2>Guardar</h2>"),
                     good_recipe()]
    assert run(recipe_job) == "AWAITING_APPROVAL"
    report = fetch("SELECT review_report FROM publication_jobs WHERE id = :j",
                   j=recipe_job[0])[0]["review_report"]
    reasons = report["rewrite_reasons"]
    assert len(reasons) == 1 and reasons[0]["attempt"] == 1 and reasons[0]["kind"] == "gates"
    assert any("faltam seções" in r for r in reasons[0]["reasons"])


def test_sem_reescrita_o_relatorio_nao_traz_motivos(recipe_job, ai):
    assert run(recipe_job) == "AWAITING_APPROVAL"
    report = fetch("SELECT review_report FROM publication_jobs WHERE id = :j",
                   j=recipe_job[0])[0]["review_report"]
    assert "rewrite_reasons" not in report


# --- Ajustes A, B e D (2026-10-24, depois do 2.º ensaio) ----------------------------------------


def test_prompt_do_redator_tem_a_conservacao_padrao_e_a_conferencia_do_material(recipe_job, ai):
    run(recipe_job)
    prompt = ai.of("write")[0]["prompt"]
    assert "nunca mande esperar esfriar por até 2 horas" in prompt
    assert "leve à geladeira logo depois de pronto" in prompt
    assert "uma segunda tigela" in prompt and "nada que a lista traz fica sem uso" in prompt


def _site() -> dict:
    return {"site_key": "culinaria", "config": {}}


def test_busca_de_receita_continua_ate_ter_fontes_utilizaveis(ai, monkeypatch):
    from app.services import websearch

    monkeypatch.setattr(websearch.settings, "searxng_url", "http://searx.test")
    asked: list[tuple[str, str, str]] = []

    def fake_ask(client, query, *, category, time_range, language="en"):
        asked.append((language, query, time_range))
        if language == "pt-BR":                      # só vetados, rede social e repetidos
            return ["https://tudogostoso.com.br/a", "https://tudoreceitas.com/b",
                    "https://youtube.com/c", "https://receitasnestle.com.br/d",
                    "https://escoladedoce.com/e", "https://tudogostoso.com.br/f"]
        return ["https://boa1.test/a", "https://boa2.test/b", "https://boa3.test/c"]

    monkeypatch.setattr(websearch, "_ask", fake_ask)
    found = websearch.search("Risoto teste de cogumelos A", site=_site(), need=1, recipe=True)
    assert found == ["https://boa1.test/a", "https://boa2.test/b", "https://boa3.test/c"]
    assert [a[0] for a in asked] == ["pt-BR", "en"]          # a 2.ª rodada: a 1.ª não serviu
    assert all(a[2] == "" for a in asked)                     # receita não tem filtro de tempo
    assert asked[0][1].startswith("receita ") and asked[1][1].startswith("recipe ")


def test_busca_de_receita_para_na_primeira_rodada_quando_ela_serve(ai, monkeypatch):
    from app.services import websearch

    monkeypatch.setattr(websearch.settings, "searxng_url", "http://searx.test")
    asked: list[str] = []

    def fake_ask(client, query, *, category, time_range, language="en"):
        asked.append(language)
        return [f"https://ok{n}.test/r" for n in range(4)]

    monkeypatch.setattr(websearch, "_ask", fake_ask)
    assert len(websearch.search("Bolo teste de fubá B", site=_site(), need=1, recipe=True)) == 4
    assert asked == ["pt-BR"]


def test_busca_de_noticia_nao_mudou(ai, monkeypatch):
    from app.services import websearch

    monkeypatch.setattr(websearch.settings, "searxng_url", "http://searx.test")
    seen: list[tuple[str, str]] = []

    def fake_ask(client, query, *, category, time_range, language="en"):
        seen.append((language, time_range))
        return [f"https://n{n}.test/x" for n in range(3)]

    monkeypatch.setattr(websearch, "_ask", fake_ask)
    websearch.search("Notícia teste de saúde C", site=_site(), need=1, lang="pt")
    assert seen[0] == ("pt-BR", "week")                       # notícias seguem com filtro de tempo


# --- Ajustes E1, E2 e E3 (2026-10-24, depois do 3.º ensaio) -------------------------------------


def _ref_with(ingredients: list[str]) -> recipe.Reference:
    ref = recipe.RecipeRef(ingredients=tuple(ingredients), steps=5, total_minutes=45,
                           oven_temps=(), yield_text="4")
    return recipe.Reference("outra.test", ref, "texto qualquer da página")


RICH = good_recipe(Ingredientes="<ul><li>300 g de arroz arbóreo</li><li>300 g de cogumelo fresco"
                   "</li><li>1,2 litro de caldo de legumes</li><li>1 cebola média picada</li>"
                   "<li>100 ml de vinho branco seco</li><li>60 g de queijo parmesão</li>"
                   "<li>3 colheres de sopa de manteiga</li></ul>",
                   **{"Modo de preparo": "<ol><li>Refogue a cebola na manteiga por 5 minutos.</li>"
                      "<li>Junte o arroz, o cogumelo e o vinho e mexa até secar.</li>"
                      "<li>Acrescente o caldo aos poucos e cozinhe por 20 minutos, mexendo.</li>"
                      "<li>Finalize com o queijo parmesão.</li></ol>"})


def test_receita_mais_rica_que_a_fonte_nao_e_barrada():
    poor = _ref_with(["arroz", "cogumelo", "cebola", "caldo", "queijo"])        # 5 itens: ignorada
    assert not any("quase não coincidem" in p for p in recipe.check_recipe(RICH, [poor]))
    six = _ref_with(["200 g de arroz", "200 g de cogumelo", "1 cebola", "1 litro de caldo",
                     "50 g de queijo", "1 colher de manteiga"])
    assert not any("quase não coincidem" in p for p in recipe.check_recipe(RICH, [six]))


def test_prato_diferente_ainda_e_barrado_pela_cobertura_da_fonte():
    other = _ref_with(["3 cenouras", "4 ovos", "1 xícara de óleo", "2 xícaras de açúcar",
                       "2 xícaras de farinha", "1 colher de fermento"])
    assert any("quase não coincidem" in p for p in recipe.check_recipe(RICH, [other]))


def test_fonte_em_ingles_nao_serve_para_conferir_ingredientes():
    english = _ref_with(["2 cups arborio rice", "1 tablespoon butter", "1 cup mushrooms",
                         "4 cups chicken stock", "1 clove garlic", "1/2 cup parmesan"])
    assert not recipe._comparable(english)
    assert not any("quase não coincidem" in p for p in recipe.check_recipe(RICH, [english]))


SANDUICHE = good_recipe(Ingredientes="<ul><li>2 fatias de pão de forma</li>"
                        "<li>1 fatia de presunto cozido</li><li>1 fatia de queijo</li>"
                        "<li>1 ovo</li><li>10 g de manteiga</li><li>5 ml de azeite</li></ul>",
                        **{"Modo de preparo": "<ol><li>Passe a manteiga no pão.</li>"
                           "<li>Monte com o presunto e o queijo e doure no azeite.</li>"
                           "<li>Frite o ovo e coloque por cima.</li></ol>"})


def test_fonte_em_espanhol_nao_serve_para_conferir_ingredientes():
    # 13/11, job 292: «10 g»/«5 ml» faziam a lista espanhola passar por portuguesa, e a receita
    # certa era reprovada três vezes por «os ingredientes quase não coincidem».
    spanish = _ref_with(["2 rebanadas de pan de molde", "1 loncha de jamón cocido",
                         "1 loncha de queso", "1 huevo", "10 g de mantequilla",
                         "5 ml de aceite de oliva", "Sal al gusto"])
    assert not recipe._comparable(spanish)
    assert not any("quase não coincidem" in p for p in recipe.check_recipe(SANDUICHE, [spanish]))


def test_fonte_em_italiano_ou_frances_nao_serve_para_conferir_ingredientes():
    italian = _ref_with(["320 g di riso carnaroli", "1 cipolla", "1 spicchio d'aglio",
                         "50 g di burro", "80 g di formaggio grana", "sale q.b."])
    french = _ref_with(["2 oeufs", "100 g de farine", "50 g de beurre", "1 oignon",
                        "1 gousse d'ail", "1 pincée de sel"])
    assert not recipe._comparable(italian)
    assert not recipe._comparable(french)


def test_fonte_em_portugues_com_medidas_metricas_continua_valendo():
    metric = _ref_with(["200 g de pão", "100 g de presunto", "100 g de queijo", "2 ovos",
                        "20 g de manteiga", "10 ml de azeite"])
    assert recipe._comparable(metric)
    other = _ref_with(["3 cenouras", "4 ovos", "1 xícara de óleo", "2 xícaras de açúcar",
                       "2 xícaras de farinha", "1 colher de fermento"])
    assert any("quase não coincidem" in p for p in recipe.check_recipe(SANDUICHE, [other]))


def test_revisor_so_bloqueia_ponto_pela_cor_se_nao_ha_temperatura_em_lugar_nenhum(recipe_job, ai):
    run(recipe_job)
    review = ai.of("review")[0]["prompt"]
    assert "em lugar nenhum" in review and "só um reparo `minor`" in review


def test_bloqueio_guarda_o_motivo_de_cada_tentativa(recipe_job, ai, web):
    web[R1] = PAGE.format(ld=json.dumps(SCHEMA), text=" ".join(STEPS) * 3)
    copied = good_recipe(**{"Modo de preparo": "<ol>" + "".join(f"<li>{s}</li>" for s in STEPS)
                            + "</ol>"})
    ai.draft_html = [copied]
    assert run(recipe_job) == "BLOCKED"
    # D-093: a pauta esgotada sai e o job procura outra; o histórico fica no evento do esgotamento.
    details = fetch("SELECT details FROM publication_events WHERE job_id = :j "
                    "AND event_type = 'review_exhausted'", j=recipe_job[0])[0]["details"]
    reasons = details["rewrite_reasons"]
    assert [r["attempt"] for r in reasons] == [1, 2, 3]
    assert all(r["kind"] == "gates" for r in reasons)
    assert any("parecidos demais" in x for x in reasons[-1]["reasons"])


# --- Ajustes F1 e F2 (2026-10-24, depois do 4.º ensaio) -----------------------------------------


def test_marinada_e_esperas_longas_ficam_fora_da_soma_dos_passos():
    steps = ["Misture o leite e o limão, junte o frango e leve à geladeira por 4 horas "
             "(no máximo 12 horas).", "Deixe o empanado descansar por 15 minutos.",
             "Frite por 16 a 20 minutos.", "Deixe a massa fermentar por 8 horas."]
    assert recipe.steps_minutes(steps) == 35


def test_total_que_deixa_a_marinada_a_parte_passa():
    steps = ("<ol><li>Leve o frango à geladeira por 4 horas na marinada (no máximo 12 horas).</li>"
             "<li>Deixe o empanado descansar por 15 minutos.</li><li>Frite por 16 a 20 "
             "minutos em óleo a 175 °C.</li></ol>")
    html = good_recipe(**{"Modo de preparo": steps, "Informações da receita":
                          "<ul><li>Rendimento: 4 porções</li><li>Tempo de preparo: 20 minutos</li>"
                          "<li>Tempo de cozimento: 30 minutos</li>"
                          "<li>Tempo total: 50 minutos, mais 4 horas de marinada</li></ul>"})
    assert not any("tempo total" in p for p in recipe.check_recipe(html, []))


def test_traducao_pode_dizer_de_50_em_50_ml_uma_vez_so():
    from app.services import gates

    original = "<p>Acrescente a água aos poucos, de 50 em 50 ml, até chegar ao ponto.</p>"
    once = "<p>每次加 50 毫升水,直至达到浓稠度。</p>"
    twice = "<p>每次加 50 毫升、再加 50 毫升水,直至达到浓稠度。</p>"
    assert gates.check_translation(original, once, language="zh-CN") == []
    assert gates.check_translation(original, twice, language="zh-CN") == []


def test_numero_perdido_de_verdade_continua_barrado():
    from app.services import gates

    original = "<p>Acrescente a água de 50 em 50 ml e asse por 40 minutos.</p>"
    lost_time = "<p>每次加 50 毫升水,然后烘烤。</p>"
    assert any("números" in p for p in gates.check_translation(original, lost_time,
                                                              language="zh-CN"))
    plain = "<p>Asse por 50 minutos.</p>"
    assert any("números" in p for p in gates.check_translation(plain, "<p>烘烤。</p>",
                                                              language="zh-CN"))


# --- Ajustes G1, G2 e G3 (2026-10-24, depois do 5.º ensaio) -------------------------------------


def test_g1_de_50_ml_em_50_ml_pode_virar_uma_mencao():
    from app.services import gates

    for original in ("<p>Junte a água de 50 em 50 ml, mexendo.</p>",
                     "<p>Junte a água de 50 ml em 50 ml (até 200 ml no total), mexendo.</p>"):
        total = "（共 200 毫升）" if "200" in original else ""
        translated = f"<p>每次加 50 毫升水{total}，边加边搅拌。</p>"
        assert gates.check_translation(original, translated, language="zh-CN") == [], original


def test_g2_quantidade_implicita_vira_1_na_traducao_mas_so_ate_o_limite():
    from app.services import gates

    original = "<p>Unte as mãos com a colher de sopa de óleo e modele bolinhas de 30 g.</p>"
    ok = "<p>用1汤匙油涂抹双手，把面团搓成约30 g的小球。</p>"
    assert gates.check_translation(original, ok, language="zh-CN") == []
    extra = "<p>用1汤匙油涂抹双手，把面团搓成约30 g的小球，烤2小时。</p>"
    assert any("números" in p for p in gates.check_translation(original, extra,
                                                              language="zh-CN"))
    two_ones = "<p>用1汤匙油涂抹双手，再加1汤匙，把面团搓成约30 g的小球。</p>"
    assert any("números" in p for p in gates.check_translation(original, two_ones,
                                                              language="zh-CN"))


def test_g3_quantidades_que_nao_tem_algarismo_mas_sao_quantidades():
    lines = ["½ colher de chá de sal", "¼ de xícara de leite", "meia xícara de açúcar",
             "uma pitada de noz-moscada", "Água quente, se precisar ajustar a textura",
             "Água, o suficiente para cobrir as batatas", "Manteiga, para untar a forma",
             "Salsinha picada, para servir", "Pimenta-do-reino a gosto"]
    html = good_recipe(Ingredientes="<ul>" + "".join(f"<li>{i}</li>" for i in lines) + "</ul>",
                       **{"Modo de preparo": "<ol><li>Misture o sal, o leite, o açúcar, a água, "
                          "a noz-moscada, a manteiga, a salsinha e a pimenta por 5 minutos.</li>"
                          "<li>Leve ao fogo por 10 minutos.</li><li>Sirva quente.</li></ol>"})
    assert not any("sem quantidade" in p for p in recipe.check_recipe(html, []))


def test_g3_ingrediente_sem_quantidade_de_verdade_continua_barrado():
    html = good_recipe(Ingredientes="<ul><li>3 cenouras médias</li><li>4 ovos</li>"
                       "<li>1 xícara de óleo</li><li>chocolate meio amargo</li>"
                       "<li>farinha de trigo</li></ul>")
    assert any("sem quantidade" in p and "chocolate meio amargo" in p
               for p in recipe.check_recipe(html, []))



# --- Ajuste C: polimento com as notas do revisor (2026-10-26) -----------------------------------

POLIDA = ("<p>Todos os itens são vendidos em qualquer supermercado ou feira; o fermento fica na "
          "seção de farinhas e misturas, e o óleo pode ser dividido em duas medidas iguais.</p>")


def test_polimento_aplica_as_notas_e_a_re_revisao_limpa_os_reparos(recipe_job, ai):
    ai.draft_html = [good_recipe(),
                     good_recipe(**{"Onde encontrar os ingredientes": POLIDA})]
    ai.reviews = [
        {"score": 92, "approved": True, "unsupported_claims": [],
         "issues": [{"text": "diga como dividir o óleo", "severity": "minor"}]},
        {"score": 96, "approved": True, "issues": [], "unsupported_claims": []}]
    assert run(recipe_job) == "AWAITING_APPROVAL"
    assert len(ai.of("write")) == 2 and len(ai.of("review")) == 2
    assert "reparos do revisor" in ai.of("write")[1]["prompt"]
    assert "dividir o óleo" in ai.of("write")[1]["prompt"]
    report = fetch("SELECT review_report FROM publication_jobs WHERE id = :j",
                   j=recipe_job[0])[0]["review_report"]
    assert report["polish"] == {"applied": True, "notes_before": 1, "notes_after": 0,
                                "scores": [92, 96]}
    assert report["notes"] == [] and report["rewrites"] == 0
    html = fetch("SELECT html FROM job_translations WHERE job_id = :j AND is_original",
                 j=recipe_job[0])[0]["html"]
    assert "duas medidas iguais" in html
    events = fetch("SELECT details FROM publication_events WHERE job_id = :j "
                   "AND event_type = 'recipe_polish'", j=recipe_job[0])
    assert len(events) == 1 and events[0]["details"]["applied"] is True


def test_polimento_que_piora_na_re_revisao_volta_a_versao_aprovada(recipe_job, ai):
    ai.draft_html = [good_recipe(),
                     good_recipe(**{"Onde encontrar os ingredientes": POLIDA})]
    ai.reviews = [
        {"score": 92, "approved": True, "unsupported_claims": [],
         "issues": [{"text": "diga como dividir o óleo", "severity": "minor"}]},
        {"score": 60, "approved": False, "unsupported_claims": [],
         "issues": [{"text": "a divisão do óleo ficou errada", "severity": "blocking"}]}]
    assert run(recipe_job) == "AWAITING_APPROVAL"
    report = fetch("SELECT review_report FROM publication_jobs WHERE id = :j",
                   j=recipe_job[0])[0]["review_report"]
    assert report["polish"]["applied"] is False
    assert "re-revisão" in report["polish"]["reason"]
    assert report["notes"] == ["diga como dividir o óleo"] and report["score"] == 92
    html = fetch("SELECT html FROM job_translations WHERE job_id = :j AND is_original",
                 j=recipe_job[0])[0]["html"]
    assert "duas medidas iguais" not in html       # ficou a versão aprovada (reserva)


def test_polimento_que_quebra_um_gate_volta_a_reserva_sem_gastar_re_revisao(recipe_job, ai):
    quebrada = good_recipe().replace("<h2>Conservação</h2>", "<h2>Guardar</h2>")
    ai.draft_html = [good_recipe(), quebrada]
    ai.reviews = [
        {"score": 92, "approved": True, "unsupported_claims": [],
         "issues": [{"text": "diga como dividir o óleo", "severity": "minor"}]}]
    assert run(recipe_job) == "AWAITING_APPROVAL"
    assert len(ai.of("review")) == 1               # a re-revisão nem foi chamada
    report = fetch("SELECT review_report FROM publication_jobs WHERE id = :j",
                   j=recipe_job[0])[0]["review_report"]
    assert report["polish"]["applied"] is False
    assert report["polish"]["reason"].startswith("gates:")
    assert report["notes"] == ["diga como dividir o óleo"]


def test_sem_reparos_menores_nao_ha_polimento(recipe_job, ai):
    assert run(recipe_job) == "AWAITING_APPROVAL"
    assert len(ai.of("write")) == 1 and len(ai.of("review")) == 1
    report = fetch("SELECT review_report FROM publication_jobs WHERE id = :j",
                   j=recipe_job[0])[0]["review_report"]
    assert "polish" not in report
    assert not fetch("SELECT 1 FROM publication_events WHERE job_id = :j "
                     "AND event_type = 'recipe_polish'", j=recipe_job[0])


# --- Estrutura canônica da tradução (revisão E14) -------------------------------------------------


def test_localize_structure_reescreve_h2_e_rotulos_por_posicao():
    original = (
        "<p>Abertura.</p>"
        "<h2>Informações da receita</h2><ul><li>Rendimento: 8 fatias</li>"
        "<li>Tempo de preparo: 25 minutos</li><li>Tempo total: 1 hora</li></ul>"
        "<h2>Material necessário</h2><ul><li>tigela</li></ul>"
        "<h2>Ingredientes</h2><ul><li>2 ovos</li></ul>"
        "<h2>Modo de preparo</h2><ol><li>Misture.</li></ol>"
        "<h2>Dicas e substituições</h2><ul><li>dica</li></ul>"
        "<h2>Onde encontrar os ingredientes</h2><p>mercado</p>"
        "<h2>Conservação</h2><p>geladeira</p>"
        "<h2>Alérgenos e cuidados</h2><p>ovo</p>")
    translated = (
        "<p>Opening.</p>"
        "<h2>Information about this recipe</h2><ul><li>Serves: 8 slices</li>"
        "<li>Preparation: 25 minutes</li><li>Overall time: 1 hour</li></ul>"
        "<h2>What you need</h2><ul><li>bowl</li></ul>"
        "<h2>The ingredients</h2><ul><li>2 eggs</li></ul>"
        "<h2>Method</h2><ol><li>Mix.</li></ol>"
        "<h2>Tips</h2><ul><li>tip</li></ul>"
        "<h2>Where to buy</h2><p>market</p>"
        "<h2>Keeping</h2><p>fridge</p>"
        "<h2>Allergens</h2><p>egg</p>")
    out = recipe.localize_structure(translated, "en", original=original)
    assert out is not None
    for title in recipe.SECTIONS_BY_LANG["en"]:
        assert f"<h2>{title}</h2>" in out
    # Rótulos canônicos por posição (o sb-recipe.php lê por igualdade exata)…
    assert "<li>Yield: 8 slices</li>" in out
    assert "<li>Prep time: 25 minutes</li>" in out
    assert "<li>Total time: 1 hour</li>" in out
    # …e fora do cartão nada muda.
    assert "<li>bowl</li>" in out and "<li>Mix.</li>" in out


def test_localize_structure_mantem_dois_pontos_largos_do_chines():
    original = ("<h2>Informações da receita</h2><ul><li>Rendimento: 4 porções</li></ul>"
                + "".join(f"<h2>{s}</h2><p>x</p>" for s in recipe.SECTIONS[1:]))
    translated = ("<h2>关于本食谱</h2><ul><li>供应量：4份</li></ul>"
                  + "".join(f"<h2>第{i}节</h2><p>x</p>" for i in range(7)))
    out = recipe.localize_structure(translated, "zh-CN", original=original)
    assert out is not None
    assert "<h2>食谱信息</h2>" in out and "<li>份量：4份</li>" in out


def test_localize_structure_devolve_none_sem_as_secoes():
    original = "<h2>Informações da receita</h2><ul><li>Rendimento: 2</li></ul>"
    assert recipe.localize_structure("<p>só texto</p><h2>Um</h2><p>x</p>", "en",
                                     original=original) is None
    # Idioma fora da tabela: devolve como veio (pt-BR nunca passa por aqui).
    html = "<h2>Qualquer</h2><p>x</p>"
    assert recipe.localize_structure(html, "fr", original=original) == html
