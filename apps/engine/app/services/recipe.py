"""Receita original do redator, conferida com receitas publicadas (D-080, ADR 0004).

No modo receita o redator escreve a receita a partir do conhecimento culinário geral; as
fontes servem só para conferir a plausibilidade (ingredientes típicos, faixas de tempo e de
temperatura). Aqui ficam as peças que não dependem de um modelo:

- ler o `schema.org/Recipe` de uma página (só números e listas curtas, nunca o texto dos passos);
- conferir a receita escrita: estrutura obrigatória, coerência, faixas plausíveis, semelhança com
  cada fonte e frases proibidas;
- montar o aviso de transparência (em código, nos quatro idiomas: o modelo não o escreve).
"""
from __future__ import annotations

import html as html_lib
import json
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from statistics import median
from typing import Any

from bs4 import BeautifulSoup, Tag

from app.core.config import settings
from app.services import gates

# Títulos de seção, na ordem em que o redator os escreve (pt-BR).
SECTIONS = ("Informações da receita", "Material necessário", "Ingredientes", "Modo de preparo",
            "Dicas e substituições", "Onde encontrar os ingredientes", "Conservação",
            "Alérgenos e cuidados")

# Tabela canônica dos títulos por idioma, na mesma ordem de SECTIONS. A tradução NÃO é de
# confiança para estes textos: `localize_structure` reescreve os <h2> por posição depois de
# traduzir, e o `sb-recipe.php` casa por igualdade exata para decorar e gerar o
# `schema.org/Recipe` — mudar aqui exige mudar lá (e vice-versa).
SECTIONS_BY_LANG: dict[str, tuple[str, ...]] = {
    "pt-BR": SECTIONS,
    "en": ("Recipe information", "Equipment", "Ingredients", "Instructions",
           "Tips and substitutions", "Where to find the ingredients", "Storage",
           "Allergens and safety"),
    "es": ("Información de la receta", "Material necesario", "Ingredientes",
           "Modo de preparación", "Consejos y sustituciones",
           "Dónde encontrar los ingredientes", "Conservación", "Alérgenos y precauciones"),
    "zh-CN": ("食谱信息", "所需工具", "食材", "制作方法", "小贴士与替换",
              "哪里买到食材", "保存方法", "过敏原与注意事项"),
}

# Rótulos canônicos do cartão «Informações da receita» (chave → rótulo por idioma); o JSON-LD
# do sb-recipe.php lê Rendimento e os Tempos por estes textos exatos.
INFO_LABELS: dict[str, dict[str, str]] = {
    "pt-BR": {"yield": "Rendimento", "prep": "Tempo de preparo", "cook": "Tempo de cozimento",
              "rest": "Tempo de descanso ou resfriamento", "total": "Tempo total"},
    "en": {"yield": "Yield", "prep": "Prep time", "cook": "Cooking time",
           "rest": "Resting or chilling time", "total": "Total time"},
    "es": {"yield": "Rendimiento", "prep": "Tiempo de preparación", "cook": "Tiempo de cocción",
           "rest": "Tiempo de reposo o enfriado", "total": "Tiempo total"},
    "zh-CN": {"yield": "份量", "prep": "准备时间", "cook": "烹饪时间",
              "rest": "静置或冷藏时间", "total": "总时间"},
}

_BANNED = ("testamos", "testei", "testámos", "fica perfeito", "emagrece", "desintoxica")
_NO_QUANTITY = ("a gosto", "quanto baste", "pitada", "fio de", "punhado", "suficiente",
                "se precis", "se necess", "conforme necess", "para untar", "para polvilhar",
                "para fritar", "para servir", "para decorar", "para pincelar", "para regar",
                "opcional")
_DIGIT_OR_FRACTION = re.compile(r"[\d¼½¾⅐-⅞]")
_LEADING_QUANTITY = re.compile(r"(?:meio|meia|um|uma|dois|duas|tres)\b")
_UNITS = frozenset((
    "xicara", "xicaras", "colher", "colheres", "sopa", "cha", "sobremesa", "copo", "copos",
    "grama", "gramas", "quilo", "quilos", "litro", "litros", "unidade", "unidades", "dente",
    "dentes", "pitada", "fatia", "fatias", "lata", "latas", "pacote", "pacotes", "maco", "ramo",
    "ramos", "folha", "folhas", "cubo", "cubos", "gosto", "baste", "quanto", "picado", "picada",
    "ralado", "ralada", "cortado", "cortada", "fatiado", "fatiada", "fresco", "fresca", "grande",
    "grandes", "medio", "media", "pequeno", "pequena", "temperatura", "ambiente", "derretido",
    "derretida", "peneirado", "peneirada", "descascado", "descascada", "cozido", "cozida",
    "moido", "moida", "com", "sem", "para", "uma", "uns", "umas", "cerca", "mais", "bem",
))
_TEMP = re.compile(r"(\d{2,3})\s*(?:°|º|˚)\s*C\b|(\d{2,3})\s*graus(?:\s*celsius)?", re.IGNORECASE)
_DURATION = re.compile(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?")
_WORD = re.compile(r"[a-z0-9]+")


def fold(text: str) -> str:
    """Minúsculas e sem acentos: compara títulos e palavras sem depender da grafia."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c)).lower().strip()


def fold_keep_fractions(text: str) -> str:
    """Como `fold`, mas sem decompor as frações Unicode (½ continua sendo ½, e não «1⁄2»)."""
    return "".join(c if c in "¼½¾⅐⅑⅒⅓⅔⅕⅖⅗⅘⅙⅚⅛⅜⅝⅞" else " " if c.isspace() else fold(c)
                   for c in text)


def _has_quantity(line: str) -> bool:
    """A linha traz quantidade: algarismo, fração Unicode ou «meia», «uma»… no início."""
    folded = fold_keep_fractions(line).strip()
    return bool(_DIGIT_OR_FRACTION.search(folded) or _LEADING_QUANTITY.match(folded))


@dataclass(frozen=True)
class RecipeRef:
    """O que se guarda da receita estruturada de uma página: só dados para conferir."""
    ingredients: tuple[str, ...]
    steps: int
    total_minutes: int | None
    oven_temps: tuple[int, ...]
    yield_text: str


@dataclass(frozen=True)
class Reference:
    """Uma fonte consultada: nome (domínio), receita estruturada e texto da página."""
    name: str
    recipe: RecipeRef
    text: str


# --- Leitura do schema.org/Recipe ------------------------------------------------------------


def _find_recipe(data: Any) -> dict[str, Any] | None:
    if isinstance(data, dict):
        kind = data.get("@type")
        if kind == "Recipe" or (isinstance(kind, list) and "Recipe" in kind):
            return data
        graph = data.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                found = _find_recipe(item)
                if found:
                    return found
    elif isinstance(data, list):
        for item in data:
            found = _find_recipe(item)
            if found:
                return found
    return None


def _plain(value: Any) -> str:
    return gates.plain_text(str(value))


def _step_texts(raw: Any) -> list[str]:
    steps: list[str] = []
    if isinstance(raw, str):
        steps = [_plain(line) for line in raw.split("\n") if line.strip()]
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, str):
                steps.append(_plain(item))
            elif isinstance(item, dict):
                if item.get("itemListElement"):
                    steps.extend(_step_texts(item["itemListElement"]))
                elif item.get("text") or item.get("name"):
                    steps.append(_plain(item.get("text") or item.get("name")))
    return [s for s in steps if s]


def iso_minutes(value: Any) -> int | None:
    """Duração ISO 8601 (PT1H30M, P0DT45M) em minutos; None se não for uma."""
    match = _DURATION.fullmatch(str(value or "").strip().upper())
    if not match:
        return None
    days, hours, minutes, _ = (int(g) if g else 0 for g in match.groups())
    total = days * 1440 + hours * 60 + minutes
    return total or None


def oven_temps(text: str) -> list[int]:
    return [int(a or b) for a, b in _TEMP.findall(text)]


def parse_recipe(body: bytes | str) -> RecipeRef | None:
    """A receita estruturada de uma página, se tiver ao menos 3 ingredientes e 2 passos."""
    soup = BeautifulSoup(body, "html.parser")
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads((script.string or script.get_text() or "").strip())
        except ValueError:
            continue
        recipe = _find_recipe(data)
        if not recipe:
            continue
        raw = recipe.get("recipeIngredient")
        ingredients = tuple(_plain(i) for i in raw if i) if isinstance(raw, list) else ()
        steps = _step_texts(recipe.get("recipeInstructions"))
        if len(ingredients) < 3 or len(steps) < 2:
            continue
        if (len(ingredients) > settings.recipe_max_ingredients
                or len(steps) > settings.recipe_max_steps):
            continue            # dados fora de escala: várias receitas ou lixo na página
        total = iso_minutes(recipe.get("totalTime"))
        if total is None:
            parts = [iso_minutes(recipe.get("prepTime")), iso_minutes(recipe.get("cookTime"))]
            total = sum(p for p in parts if p) or None
        yielded = recipe.get("recipeYield")
        if isinstance(yielded, list):
            yielded = yielded[0] if yielded else ""
        return RecipeRef(ingredients=ingredients, steps=len(steps), total_minutes=total,
                         oven_temps=tuple(oven_temps(" ".join(steps))),
                         yield_text=_plain(yielded or "")[:60])
    return None


def summary(ref: RecipeRef) -> str:
    """Resumo da fonte para o registro de evidências e para o revisor: só números."""
    parts = [f"{len(ref.ingredients)} ingredientes", f"{ref.steps} passos"]
    if ref.total_minutes:
        parts.append(f"tempo total {ref.total_minutes} min")
    if ref.oven_temps:
        parts.append("temperaturas " + ", ".join(f"{t} °C" for t in sorted(set(ref.oven_temps))))
    if ref.yield_text:
        parts.append(f"rendimento {ref.yield_text}")
    return "; ".join(parts)


def references_summary(refs: Sequence[Reference]) -> str:
    """Bloco para o revisor: faixas e ingredientes típicos, nunca o texto dos passos."""
    lines = [f"- {r.name}: {summary(r.recipe)}" for r in refs]
    words = sorted({w for r in refs for line in r.recipe.ingredients
                    for w in _ingredient_words(line)})
    lines.append("Palavras dos ingredientes das fontes: " + ", ".join(words[:80]))
    return "\n".join(lines)


# --- Conferência da receita escrita ---------------------------------------------------------


def _stem(word: str) -> str:
    return (word[:-1] if len(word) > 3 and word.endswith("s") else word)[:5]


def _ingredient_words(line: str) -> set[str]:
    folded = re.sub(r"\([^)]*\)", " ", fold(line))
    return {w for w in _WORD.findall(folded)
            if w not in _UNITS and len(w) >= 3 and not re.fullmatch(r"\d+[a-z]{0,2}", w)}


_EN_UNITS = re.compile(r"\b(?:cups?|tablespoons?|teaspoons?|tbsp|tsp|ounces?|oz|lbs?|pounds?|"
                       r"cloves?|pinch)\b")
_PT_UNITS = re.compile(r"\b(?:xicaras?|colheres?|gramas?|g|ml|dentes?|pitada|unidades?|fatias?|"
                       r"latas?|a gosto)\b")


def _wordset(words: str) -> frozenset[str]:
    return frozenset(words.split())


# Palavras que só o português usa (já sem acento), para pesar contra as das outras línguas.
_PT_WORDS = _wordset(
    "ovo ovos cebola cebolas alho azeite manteiga farinha acucar leite queijo presunto cenoura "
    "cenouras fermento cogumelo cogumelos pao agua oleo")
# Espanhol, italiano e francês: medidas e ingredientes comuns que não existem em português.
# Medidas métricas (g, ml) são iguais nas quatro línguas e não decidem nada (13/11, job 292:
# «10 g de mantequilla» passava por português e a fonte espanhola reprovou a receita certa).
_OTHER_WORDS = _wordset(
    # espanhol
    "cucharada cucharadas cucharadita cucharaditas taza tazas diente dientes pizca rebanada "
    "rebanadas loncha lonchas huevo huevos cebolla cebollas ajo ajos aceite mantequilla harina "
    "azucar leche queso jamon zanahoria zanahorias levadura champinones pan al y con "
    # italiano
    "cucchiaio cucchiai cucchiaino cucchiaini spicchio spicchi cipolla cipolle aglio burro "
    "farina uovo uova zucchero latte formaggio qb "
    # francês
    "cuillere cuilleres gousse gousses oignon oignons ail beurre farine oeuf oeufs sucre lait "
    "fromage pincee")


def _comparable(ref: Reference) -> bool:
    """Fonte que serve para conferir os ingredientes: lista com 6 ou mais itens e em português.

    Uma lista curta ou em outro idioma não diz se o prato é o mesmo (ensaio 3, risoto: fonte de
    5 ingredientes para uma receita correta de 13; 13/11, job 292: fontes espanholas, «huevo,
    jamón, queso» contra «ovo, presunto, queijo», reprovaram duas receitas certas).
    """
    lines = [fold(i) for i in ref.recipe.ingredients]
    if len(lines) < 6:
        return False
    english = sum(1 for line in lines if _EN_UNITS.search(line))
    words = [set(_WORD.findall(line)) for line in lines]
    portuguese = sum(1 for line, ws in zip(lines, words, strict=True)
                     if _PT_UNITS.search(line) or ws & _PT_WORDS)
    other = sum(1 for ws in words if ws & _OTHER_WORDS)
    return english <= portuguese and other <= portuguese


def _coverage(ref: Reference, written: set[str]) -> float:
    """Fração dos ingredientes da fonte que a receita escrita também tem."""
    named = [line for line in ref.recipe.ingredients if _ingredient_words(line)]
    if not named:
        return 1.0
    return sum(1 for line in named if _stems(_ingredient_words(line)) & written) / len(named)


def _stems(words: Iterable[str]) -> set[str]:
    return {_stem(w) for w in words}


def _shingles(text: str, size: int = 5) -> set[tuple[str, ...]]:
    words = _WORD.findall(fold(text))
    return {tuple(words[i:i + size]) for i in range(max(0, len(words) - size + 1))}


def minutes_in(text: str) -> int | None:
    """Minutos de uma expressão como «1 hora e 30 minutos», «45 min» ou «1h30»."""
    folded = fold(text)
    total = 0
    for hours, minutes in re.findall(r"(\d+)\s*h\s*(\d{1,2})\b", folded):
        total += int(hours) * 60 + int(minutes)
    folded = re.sub(r"(\d+)\s*h\s*(\d{1,2})\b", " ", folded)
    total += sum(int(h) * 60 for h in re.findall(r"(\d+)\s*(?:horas?|h)\b", folded))
    total += sum(int(m) for m in re.findall(r"(\d+)\s*(?:minutos?|min)\b", folded))
    return total or None


_RANGE = re.compile(r"(\d+)(?:\s*(?:a|à|ou|-|–)\s*(\d+))?\s*(horas?|h|minutos?|min)\b")


# Passos que ficam fora do tempo total: o que se faz na véspera não conta como tempo da receita.
_EVE = ("de molho", "vespera", "de um dia para o outro", "durante a noite", "overnight", "marin")
# Espera de 6 horas ou mais: o total pode deixá-la à parte (marinada, geladeira).
_LONG_WAIT = 360
# Passo que manda para a geladeira por 1 hora ou mais é espera (marinada, pudim gelando).
_FRIDGE = ("geladeira", "freezer", "refriger")
_FRIDGE_WAIT = 60


def steps_minutes(steps: Sequence[str]) -> int:
    """Soma dos tempos citados nos passos; numa faixa («35 a 40 minutos») vale o maior.

    Passos de véspera (molho, marinada, noite na geladeira), passos de uma hora ou mais na
    geladeira e esperas de 6 horas ou mais não entram: a receita as declara à parte («mais 4
    horas de marinada»).
    """
    total = 0
    for step in steps:
        folded = fold(step)
        if any(marker in folded for marker in _EVE):
            continue
        spans = [max(int(first), int(second or 0)) * (60 if unit.startswith("h") else 1)
                 for first, second, unit in _RANGE.findall(folded)]
        if any(m in folded for m in _FRIDGE) and any(s >= _FRIDGE_WAIT for s in spans):
            continue
        total += sum(s for s in spans if s < _LONG_WAIT)
    return total


def _field_minutes(item: str) -> int | None:
    """Minutos do campo «Tempo de X: ...»: só a primeira duração, sem o «mais N de molho»."""
    value = item.split(":", 1)[1] if ":" in item else item
    first = re.split(r"[(;,]|\bmais\b", fold(value), maxsplit=1)[0]
    return minutes_in(first)


def _total_time_problems(info_items: list[str], steps: Sequence[str]) -> list[str]:
    """O tempo total tem de caber os tempos que a própria receita declara e os dos passos."""
    total = parts = None
    parts_sum = 0
    for item in info_items:
        label = fold(item.split(":", 1)[0])
        minutes = _field_minutes(item)
        if not label.startswith("tempo") or minutes is None:
            continue
        if "total" in label:
            total = minutes
        else:
            parts_sum += minutes
            parts = True
    if not total:
        return []
    problems: list[str] = []
    if parts and total < parts_sum:
        problems.append(f"tempo total de {total} min é menor que a soma dos tempos declarados "
                        f"({parts_sum} min): some preparo, cozimento e descanso")
    needed = steps_minutes(steps)
    if needed and total < needed * settings.recipe_total_steps_ratio:
        problems.append(f"tempo total de {total} min não cobre os tempos dos passos (cerca de "
                        f"{needed} min em sequência): corrija o total ou diga o que corre em "
                        "paralelo")
    return problems


def _sections(html: str) -> dict[str, list[Tag]]:
    """Os elementos de cada seção `<h2>`, pelo título sem acentos nem maiúsculas."""
    soup = BeautifulSoup(html or "", "html.parser")
    found: dict[str, list[Tag]] = {}
    current = ""
    for node in soup.children:
        if not isinstance(node, Tag):
            continue
        if node.name == "h2":
            current = fold(node.get_text(" ", strip=True))
            found.setdefault(current, [])
        elif current:
            found[current].append(node)
    return found


def _texts(nodes: Iterable[Tag]) -> list[str]:
    return [n.get_text(" ", strip=True) for n in nodes]


def _list_items(nodes: Iterable[Tag], kind: str) -> list[str]:
    items: list[str] = []
    for node in nodes:
        lists = [node] if node.name == kind else node.find_all(kind)
        for lst in lists:
            items.extend(li.get_text(" ", strip=True) for li in lst.find_all("li"))
    return items


def check_recipe(html: str, refs: Sequence[Reference]) -> list[str]:
    """Problemas que impedem a receita de seguir. Lista vazia = passa."""
    problems: list[str] = []
    sections = _sections(html)
    missing = [s for s in SECTIONS if fold(s) not in sections]
    if missing:
        problems.append("faltam seções (títulos <h2> exatos): " + "; ".join(missing))
    plain = BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)
    banned = [b for b in _BANNED if b in fold(plain)]
    if banned:
        problems.append("expressões proibidas: " + ", ".join(banned))
    info_items = _list_items(sections.get(fold(SECTIONS[0]), []), "ul")
    info = fold(" ".join(_texts(sections.get(fold(SECTIONS[0]), []))))
    gaps = [label for label, key in (("rendimento", "rendimento"), ("tempo de preparo", "preparo"),
                                     ("tempo total", "total")) if key not in info]
    if gaps:
        problems.append("informações sem: " + ", ".join(gaps))

    ingredients = _list_items(sections.get(fold(SECTIONS[2]), []), "ul")
    steps = _list_items(sections.get(fold(SECTIONS[3]), []), "ol")
    if len(ingredients) < 4:
        problems.append(f"poucos ingredientes: {len(ingredients)} (mínimo 4)")
    if len(steps) < 3:
        problems.append(f"passos poucos: {len(steps)} (mínimo 3, em lista numerada)")
    unquantified = [i for i in ingredients if not _has_quantity(i)
                    and not any(k in fold(i) for k in _NO_QUANTITY)]
    if unquantified:
        problems.append("ingredientes sem quantidade: " + "; ".join(unquantified[:3]))

    steps_text = " ".join(steps)
    problems.extend(_total_time_problems(info_items, steps))
    steps_stems = _stems(w for w in _WORD.findall(fold(steps_text)) if len(w) >= 3)
    unused = [i for i in ingredients if _ingredient_words(i)
              and not _stems(_ingredient_words(i)) & steps_stems]
    if ingredients and len(unused) > max(1, len(ingredients) // 4):
        problems.append("ingredientes que nenhum passo usa: " + "; ".join(unused[:4]))

    temps = oven_temps(steps_text)
    if any(t < 40 or t > 300 for t in temps):
        problems.append("temperatura fora da faixa plausível (40 a 300 °C): "
                        + ", ".join(str(t) for t in temps if t < 40 or t > 300))
    long_steps = [m for m in re.findall(r"(\d+)\s*horas?\b", fold(steps_text)) if int(m) > 24]
    if long_steps:
        problems.append("tempo de mais de 24 horas num passo")
    if refs:
        problems.extend(_against_references(info, ingredients, steps_text, temps, refs))
    return problems


def _against_references(info: str, ingredients: list[str], steps_text: str, temps: list[int],
                        refs: Sequence[Reference]) -> list[str]:
    problems: list[str] = []
    ref_temps = [t for r in refs for t in r.recipe.oven_temps if t >= 100]
    oven = [t for t in temps if t >= 100]
    if ref_temps and oven:
        gap = abs(median(oven) - median(ref_temps))
        if gap > settings.recipe_temp_tolerance:
            problems.append(f"temperatura {int(median(oven))} °C; as fontes ficam entre "
                            f"{min(ref_temps)} e {max(ref_temps)} °C")
    ref_totals = [r.recipe.total_minutes for r in refs if r.recipe.total_minutes]
    written = minutes_in(info.split("total", 1)[1][:60]) if "total" in info else None
    # Só o total subestimado é suspeito: as fontes quase nunca contam geladeira, descanso ou
    # noite de molho, então um total maior pode ser o mais honesto (ensaio 2, pudim).
    if ref_totals and written and written < min(ref_totals) / 2.5:
        problems.append(f"tempo total de {written} min; as fontes ficam entre "
                        f"{min(ref_totals)} e {max(ref_totals)} min")
    comparable = [r for r in refs if _comparable(r)]
    if comparable:
        mine_stems = _stems(w for i in ingredients for w in _ingredient_words(i))
        best = max(_coverage(r, mine_stems) for r in comparable)
        if best < 0.4:
            problems.append("os ingredientes quase não coincidem com os das receitas consultadas "
                            "(o prato é o mesmo?)")
    mine = _shingles(steps_text)
    written_lines = {fold(re.sub(r"\s+", " ", i)) for i in ingredients}
    for ref in refs:
        theirs = _shingles(ref.text)
        if mine and len(mine & theirs) / len(mine) > settings.recipe_copy_ratio:
            problems.append(f"passos parecidos demais com os de {ref.name} (reescreva com "
                            "palavras e ordem próprias)")
        same = written_lines & {fold(re.sub(r"\s+", " ", i)) for i in ref.recipe.ingredients}
        if len(written_lines) >= 5 and len(same) / len(written_lines) >= 0.8:
            problems.append(f"lista de ingredientes igual à de {ref.name}")
    return problems


# --- Aviso de transparência (montado pelo código) -------------------------------------------


def notice(names: Sequence[str], language: str) -> str:
    """Parágrafo que diz ao leitor como a receita foi feita, no idioma do post."""
    n = len(names)
    who = html_lib.escape(", ".join(names))
    text = {
        "pt-BR": (f"Receita elaborada pela redação a partir de conhecimento culinário geral e "
                  f"conferida com {n} receita{'s publicadas' if n != 1 else ' publicada'} por "
                  f"{who}. Os ingredientes, as quantidades e o modo de preparo foram escritos de "
                  "forma original, e a receita não foi testada na nossa cozinha. Ajuste tempos e "
                  "temperos ao seu fogão ou forno."),
        "en": (f"Recipe written by our editorial team from general culinary knowledge and checked "
               f"against {n} recipe{'s published' if n != 1 else ' published'} by {who}. The "
               "ingredients, quantities and method are original, and the recipe has not been "
               "tested in our kitchen. Adjust times and seasoning to your stove or oven."),
        "es": (f"Receta elaborada por la redacción a partir de conocimientos culinarios generales "
               f"y contrastada con {n} receta{'s publicadas' if n != 1 else ' publicada'} por "
               f"{who}. Los ingredientes, las cantidades y el modo de preparación son originales, "
               "y la receta no ha sido probada en nuestra cocina. Ajuste los tiempos y los "
               "condimentos a su cocina u horno."),
        "zh-CN": (f"本食谱由编辑部根据一般烹饪知识撰写，并与{who}发布的{n}份食谱对照核实。"
                  "食材、用量和做法均为原创，且未在我们的厨房中试做过。"
                  "请根据您的炉灶或烤箱调整时间和调味。"),
    }[language]
    return f"<p><em>{text}</em></p>"


# --- Estrutura canônica da tradução (revisão E14) -------------------------------------------------

_H2_SPLIT = re.compile(r"(<h2[^>]*>.*?</h2>)", re.IGNORECASE | re.DOTALL)
_LI = re.compile(r"<li>(.*?)</li>", re.DOTALL)
_LABEL = re.compile(r"^\s*([^:：<]{1,60})([:：])")


def _label_key(item: str) -> str | None:
    """A chave canônica do rótulo de um item do cartão de informações (pt-BR), ou None."""
    head = fold(re.split(r"[:：]", _plain(item), maxsplit=1)[0])
    if "rendimento" in head:
        return "yield"
    if "descanso" in head or "resfriamento" in head:
        return "rest"       # antes de prep/cozimento: «descanso» não tem «preparo» nem «cozimento»
    if "preparo" in head:
        return "prep"
    if "cozimento" in head or "forno" in head:
        return "cook"
    if "total" in head:
        return "total"
    return None


def localize_structure(html: str, language: str, *, original: str) -> str | None:
    """Reescreve na tradução, por posição, os <h2> e os rótulos do cartão de informações com os
    textos canônicos do idioma (SECTIONS_BY_LANG / INFO_LABELS).

    O modelo de tradução não é de confiança para estes textos: o `sb-recipe.php` casa por
    igualdade exata para decorar a página e gerar o `schema.org/Recipe`, e um título «criativo»
    derrubava os dois em silêncio naquele idioma. A ordem das seções é fixa (conferida em pt-BR
    por `check_recipe`), então a posição identifica cada uma. Devolve None se a tradução não
    manteve o número de seções — quem chama reprova e pede a tradução de novo.
    """
    titles = SECTIONS_BY_LANG.get(language)
    if titles is None:
        return html
    pieces = _H2_SPLIT.split(html)
    headings = [i for i in range(1, len(pieces), 2)]
    if len(headings) != len(titles):
        return None
    for position, index in enumerate(headings):
        pieces[index] = f"<h2>{titles[position]}</h2>"
    # Rótulos do cartão: a posição na lista traduzida casa com a do original pt-BR.
    source = _H2_SPLIT.split(original)
    keys = [_label_key(item) for item in _LI.findall(source[2])] if len(source) > 2 else []
    body = pieces[headings[0] + 1]
    items = list(_LI.finditer(body))
    if len(items) == len(keys):
        rebuilt: list[str] = []
        cursor = 0
        for match, key in zip(items, keys, strict=True):
            inner = match.group(1)
            label = _LABEL.match(inner)
            if key is not None and label is not None:
                inner = INFO_LABELS[language][key] + label.group(2) + inner[label.end():]
            rebuilt.append(body[cursor:match.start(1)])
            rebuilt.append(inner)
            cursor = match.end(1)
        rebuilt.append(body[cursor:])
        pieces[headings[0] + 1] = "".join(rebuilt)
    return "".join(pieces)
