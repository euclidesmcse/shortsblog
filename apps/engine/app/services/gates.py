"""Gates determinísticos do artigo (E6.7) e verificação das traduções (E6.8).

Nada aqui depende de um modelo: são regras que o código verifica sempre, por
muito que o revisor de IA tenha gostado do texto.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag

from app.services import discovery

# Só estas etiquetas chegam ao WordPress; o resto é desembrulhado (fica o texto).
ALLOWED_TAGS = frozenset({
    "p", "h2", "h3", "h4", "ul", "ol", "li", "strong", "em", "b", "i", "blockquote", "a",
    "br", "table", "thead", "tbody", "tr", "th", "td", "code", "pre",
})
# Estas saem com tudo o que têm dentro.
DROP_WITH_CONTENT = frozenset({
    "script", "style", "iframe", "object", "embed", "form", "input", "button", "textarea",
    "select", "noscript", "svg", "math", "link", "meta", "base", "img", "video", "audio",
    "template", "frame", "frameset", "applet",
})
# Dígito colado a uma letra ASCII à esquerda é parte de um nome (IPv6, A380, Python3), não um
# número — a tradução pode repetir o nome à vontade (job 137). Só à esquerda: «5G»/«19h» contam
# (perder o 5 de «5G» continua acusado), e letras CJK não entram (em chinês, 7人 é «7 pessoas»).
_NUMBER = re.compile(r"(?<![A-Za-z])\d+(?:[.,]\d+)*")
# Data numérica dia/mês/ano ("1/10/2026", "01-10-2026", "1.10.2026"): ambígua entre línguas.
_NUMERIC_DATE = re.compile(r"(?<![\d./-])\d{1,2}([/.-])\d{1,2}\1\d{2,4}(?!\d|[./-]\d)")
_WORD = re.compile(r"\w+", re.UNICODE)
# «de 50 em 50 ml» ou «de 50 ml em 50 ml»: o número se repete no original e outra língua o diz uma
# vez só.
_REPEATED = re.compile(r"\bde\s+(\d+(?:[.,]\d+)?)(?:\s*[a-zçãõé]{1,12}\.?)?\s+em\s+\1\b",
                       re.IGNORECASE)
# «a colher de sopa», «um dente»: a quantidade 1 está implícita e outra língua pode escrevê-la «1».
_ONE_IMPLIED = re.compile(
    r"\b(?:um|uma|a|o)\s+(?:colher(?:es)?|xícaras?|copos?|dentes?|pitadas?|fios?|punhados?|"
    r"folhas?|ramos?|fatias?|latas?|pacotes?|cubos?|ovos?)\b", re.IGNORECASE)


def sanitize_html(html: str) -> str:
    """HTML seguro: só etiquetas permitidas, sem atributos, e links só `https`."""
    soup = BeautifulSoup(html or "", "html.parser")
    # Só texto simples e etiquetas: comentários, CDATA, DOCTYPE, declarações e instruções de
    # processamento saem (num documento HTML, `<![CDATA[` fecha no primeiro `>` e o que vem
    # a seguir é interpretado pelo browser).
    for node in soup.find_all(string=True):
        if type(node) is not NavigableString:
            node.extract()
    for tag in list(soup.find_all(True)):
        if not isinstance(tag, Tag) or tag.decomposed:
            continue                        # já saiu com um antepassado
        name = tag.name.lower()
        if name in DROP_WITH_CONTENT:
            tag.decompose()
            continue
        if name not in ALLOWED_TAGS:
            tag.unwrap()
            continue
        href = str(tag.get("href", "")).strip() if name == "a" else ""
        tag.attrs = {}
        if name == "a":
            if discovery.canonical_url(href) is not None:
                tag.attrs = {"href": href, "rel": "noopener noreferrer nofollow"}
            else:
                tag.unwrap()                # link sem https utilizável: fica só o texto
    return str(soup).strip()


def links(html: str) -> list[str]:
    soup = BeautifulSoup(html or "", "html.parser")
    return [str(a["href"]) for a in soup.find_all("a") if a.get("href")]


def plain_text(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)


def word_count(html: str) -> int:
    return len(_WORD.findall(plain_text(html)))


def plain_line(value: str) -> str:
    """Título ou resumo como texto simples numa linha: sem etiquetas e sem `<` nem `>`."""
    text = BeautifulSoup(sanitize_html(value or ""), "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text.replace("<", "").replace(">", "")).strip()


def check_plain(**fields: str) -> list[str]:
    """Título e resumo têm de ser texto simples (vão para o painel e para o WordPress)."""
    return [f"{name} com marcação HTML" for name, value in fields.items()
            if "<" in value or ">" in value]


def _normal(number: str) -> str:
    """1.299,90 e 1,299.90 → 1299.90; 1,5 e 1.5 → 1.5; 15 fica 15 (não é o mesmo que 1,5)."""
    last = max(number.rfind("."), number.rfind(","))
    if last == -1:
        return number
    whole, tail = number[:last], number[last + 1:]
    if len(tail) == 3:                       # separador de milhar: 2.500, 1,299
        return re.sub(r"[.,]", "", number)
    return re.sub(r"[.,]", "", whole) + "." + tail


def numbers(html: str) -> Counter[str]:
    """Números do texto, com o separador decimal normalizado e sem os de milhar."""
    return Counter(_normal(n) for n in _NUMBER.findall(plain_text(html)))


def check_article(*, title: str, html: str, category: str, site: dict[str, Any],
                  source_count: int, recent_titles: list[str], min_words: int,
                  excerpt: str = "") -> list[str]:
    """Problemas que impedem um artigo de seguir. Lista vazia = passa."""
    problems: list[str] = []
    config = site.get("config") or {}
    minimum = int(config.get("min_sources", 2))
    if source_count < minimum:
        problems.append(f"fontes insuficientes: {source_count} de {minimum}")
    if not title.strip():
        problems.append("sem título")
    problems.extend(check_plain(título=title, resumo=excerpt))
    if html != sanitize_html(html):
        problems.append("HTML não sanitizado")
    insecure = [u for u in links(html) if not u.lower().startswith("https://")]
    if insecure:
        problems.append(f"links sem https: {len(insecure)}")
    words = word_count(html)
    if words < min_words:
        problems.append(f"texto curto: {words} palavras (mínimo {min_words})")
    categories = list(config.get("categories") or [])
    if category not in categories:
        problems.append(f"categoria fora da taxonomia: {category!r}")
    key = discovery.title_key(title)
    if any(discovery.similar(key, discovery.title_key(other)) for other in recent_titles):
        problems.append("duplicado: título muito parecido com um artigo recente do blog")
    return problems


_MONTHS = {
    "janeiro": 1, "fevereiro": 2, "março": 3, "abril": 4, "maio": 5, "junho": 6,
    "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11, "dezembro": 12,
}
# Números pequenos por extenso: há línguas que os escrevem com algarismos (três semanas → 3周).
# "um"/"uma" ficam de fora: são artigos, e abririam a porta a qualquer 1 a mais.
_SPELLED = {
    **_MONTHS, "dois": 2, "duas": 2, "três": 3, "quatro": 4, "cinco": 5, "seis": 6, "sete": 7,
    "oito": 8, "nove": 9, "dez": 10, "onze": 11, "doze": 12,
}
_SPELLED_WORD = re.compile(r"\b(" + "|".join(_SPELLED) + r")\b", re.IGNORECASE)


def spelled_numbers(html: str) -> Counter[str]:
    """Os meses e os números pequenos escritos por extenso no original, como números.

    Há línguas que os escrevem com algarismos (em chinês, outubro é 10月 e "três semanas"
    é 3周): esses números aparecem na tradução sem estarem no original, e não são um erro.
    Cada palavra do original autoriza um número a mais, não mais do que isso.
    """
    return Counter(str(_SPELLED[m.lower()]) for m in _SPELLED_WORD.findall(plain_text(html)))


# Citação seguida da tradução entre parênteses: «Maintain 0 mg tomorrow» (mantenha 0 mg amanhã).
_QUOTE_GLOSS = re.compile(r"[«“\"]([^«»“”\"]{1,300})[»”\"]\s*[(（]([^()（）]{1,300})[)）]")


def glossed_numbers(html: str) -> Counter[str]:
    """Números repetidos numa citação e na sua tradução entre parênteses, logo a seguir.

    Caso real (job 121): o original cita «Maintain 0 mg tomorrow» em inglês E traduz
    entre parênteses — o 0 conta duas vezes no original, mas a tradução para a língua
    da citação funde as duas menções numa só, e isso não é um número perdido. Cada par
    citação+parêntese autoriza a perda de UMA ocorrência dos números presentes nos dois
    lados; perder as duas menções continua a ser acusado.
    """
    allowed: Counter[str] = Counter()
    for quote, gloss in _QUOTE_GLOSS.findall(plain_text(html)):
        inside = Counter(_normal(n) for n in _NUMBER.findall(quote))
        beside = Counter(_normal(n) for n in _NUMBER.findall(gloss))
        allowed += inside & beside
    return allowed


# Ordinal por extenso no original que a tradução escreve em dígito: «terceiro trimestre» →
# «3rd quarter» / «第3季度» / «3.er trimestre». Só o PAR palavra+dígito-ordinal autoriza o
# número a mais: «segunda-feira» no original não cobre um «2» solto na tradução.
_ORDINAL_WORDS = {
    "primeiro": 1, "primeira": 1, "segundo": 2, "segunda": 2, "terceiro": 3, "terceira": 3,
    "quarto": 4, "quarta": 4, "quinto": 5, "quinta": 5, "sexto": 6, "sexta": 6,
    "sétimo": 7, "sétima": 7, "oitavo": 8, "oitava": 8, "nono": 9, "nona": 9,
    "décimo": 10, "décima": 10,
}
_ORDINAL_WORD = re.compile(r"\b(" + "|".join(_ORDINAL_WORDS) + r")\b", re.IGNORECASE)
_ORDINAL_DIGIT = re.compile(r"\b(\d+)(?:st|nd|rd|th)\b|第\s*(\d+)|\b(\d+)\.?(?:[ºª]|er\b)")


def ordinal_numbers(original_html: str, translated_html: str) -> Counter[str]:
    """Números a mais autorizados por ordinais por extenso do original.

    Caso real (job 149): o original diz «terceiro trimestre de 2026» e a tradução EN
    escreve «the 3rd quarter of 2026» — o 3 em dígito não é um número inventado. Cada
    palavra ordinal do original autoriza no máximo um dígito ordinal da tradução.
    """
    words = Counter(str(_ORDINAL_WORDS[w.lower()])
                    for w in _ORDINAL_WORD.findall(plain_text(original_html)))
    digits = Counter(next(g for g in groups if g)
                     for groups in _ORDINAL_DIGIT.findall(plain_text(translated_html)))
    return words & digits


# Taxa unitária com unidade implícita: «dólares por dólar investido» não tem dígito, mas o
# chinês precisa de explicitar o numeral (每投入1美元, «para cada 1 dólar investido») e o
# inglês/espanhol podem escrever «per 1 dollar»/«por 1 dólar».
_RATE_WORD = re.compile(r"\b(?:por|per|a\s+cada)\s+(?!\d)[^\W\d_]{3,}", re.IGNORECASE)
_RATE_ONE = re.compile(r"每[^\d\s]{0,6}1(?![\d.,])|\b(?:per|por)\s+1(?![\d.,])", re.IGNORECASE)


def unit_rate_numbers(original_html: str, translated_html: str) -> Counter[str]:
    """«1» a mais autorizado por taxa unitária com unidade implícita no original.

    Caso real (job 194): o original diz «809 dólares por dólar investido» — sem dígito —
    e a tradução zh-CN só consegue dizê-lo explicitando o numeral: 每投入1美元. Cada
    construção «por/per/a cada + palavra» do original autoriza no máximo um «1» que venha
    numa marca de taxa da tradução (每…1, «per 1», «por 1»); um «1» solto continua acusado.
    """
    rates = len(_RATE_WORD.findall(plain_text(original_html)))
    ones = len(_RATE_ONE.findall(plain_text(translated_html)))
    return Counter({"1": min(rates, ones)})


# Milhão/bilhão por extenso no original: o chinês não tem palavra própria para milhão —
# diz-se em dezenas de milhar (milhão = 100万, bilhão = 10亿, trilhão = 1万亿) — e o dígito
# de escala não existe no original quando este o escreve por extenso. Com dígito antes
# («5 milhões» → 500万) é a _zh_rescaled que trata; o plural sem dígito («milhões de
# bancos») vira 数百万, sem dígito, e não precisa de licença.
_SCALE_WORD = re.compile(r"\b(milhão|bilhão|trilhão)\b", re.IGNORECASE)
_SCALE_MARKS = {
    "milhão": ("100", re.compile(r"(?<![\d.,])100\s*万(?!\s*亿)")),
    "bilhão": ("10", re.compile(r"(?<![\d.,])10\s*亿")),
    "trilhão": ("1", re.compile(r"(?<![\d.,])1\s*万\s*亿")),
}


def scale_word_numbers(original_html: str, translated_html: str) -> Counter[str]:
    """Dígitos de escala chinesa autorizados por milhão/bilhão por extenso no original.

    Caso real (job 218): «um milhão de bancos de dados» e «0,001 USD por milhão de
    linhas» não têm dígito, mas o chinês só sabe dizer milhão em dezenas de milhar:
    100万. Cada «milhão»/«bilhão»/«trilhão» singular do original SEM dígito antes
    autoriza no máximo um 100万/10亿/1万亿 da tradução; o mesmo dígito solto, fora da
    marca de escala, continua acusado.
    """
    original = plain_text(original_html)
    translated = plain_text(translated_html)
    words: Counter[str] = Counter()
    for found in _SCALE_WORD.finditer(original):
        if original[:found.start()].rstrip()[-1:].isdigit():
            continue
        words[found.group(1).lower()] += 1
    allowed: Counter[str] = Counter()
    for word, (digit, mark) in _SCALE_MARKS.items():
        if words[word]:
            allowed[digit] += min(words[word], len(mark.findall(translated)))
    return allowed


# Termo seguido do equivalente entre parênteses NA TRADUÇÃO: «November 7 (7/11)»,
# «Decree No. 12.712 (Decreto nº 12.712)» — o mesmo número do original conta duas vezes.
_PAREN_GLOSS = re.compile(r"([^()（）]{1,40})[(（]([^()（）]{1,120})[)）]")


def paren_gloss_numbers(original_html: str, translated_html: str) -> Counter[str]:
    """Números a mais autorizados por um parêntese da tradução que repete o vizinho.

    Caso real (job 222): a tradução EN escreve «November 7 (7/11)» e «Decree No. 12.712
    (Decreto nº 12.712)» — o número aparece no termo traduzido E na glosa entre
    parênteses, logo a seguir. Cada par termo+parêntese autoriza UMA ocorrência a mais
    de um número que esteja dos dois lados do parêntese E no original; um número que o
    original não tem continua acusado, mesmo repetido.
    """
    before = numbers(original_html)
    allowed: Counter[str] = Counter()
    for window, inside in _PAREN_GLOSS.findall(plain_text(translated_html)):
        outside = Counter(_normal(n) for n in _NUMBER.findall(window))
        within = Counter(_normal(n) for n in _NUMBER.findall(inside))
        for number, count in (outside & within).items():
            if before[number]:
                allowed[number] += count
    return allowed


_ZH_SCALED = re.compile(r"(\d+(?:[.,]\d+)*)\s*(?:万|亿)")
_ZH_COMBO = re.compile(r"(\d+(?:[.,]\d+)*)\s*亿\s*(\d+(?:[.,]\d+)*)\s*万")
_ZH_FACTORS = (0.1, 10.0, 100.0, 1000.0)


def _zh_ratio_ok(value: float, old: str, factors: tuple[float, ...]) -> bool:
    try:
        ratio = value / float(old)
    except (ValueError, ZeroDivisionError):
        return False
    return any(abs(ratio - f) < f * 1e-9 for f in factors)


def _zh_rescaled(lost: Counter[str], added: Counter[str], translated: str,
                 original: Counter[str]) -> None:
    """Tira dos dois lados os pares que são o mesmo valor na escala chinesa.

    Milhão e bilhão não têm unidade própria em chinês: 700 bilhões é 7000亿, 3 milhões é
    300万. Só se aceita um número novo que venha colado a 万 ou 亿 e seja um número do
    original multiplicado por 10, 100, 1000 ou dividido por 10 — mesmo que o original
    continue na tradução e nada se tenha perdido, como em «约123万（1.23百万）» (G4):
    cada ocorrência no original autoriza no máximo um número reescalado.

    Um reescalado pode colidir com outro número do original (job 133: «US$ 2 milhões»
    vira 200万, mas o texto também tem «US$ 200 milhões», e então o 200 não é novo).
    A 1.ª passagem reatribui a perda ao número que colidiu — o total de perdas não
    muda, só o nome — e a 2.ª desconta-o se a tradução o trouxer reescalado (20000万);
    se não trouxer, a perda continua acusada. Antes de tudo, a 0.ª passagem cobre a
    decomposição 亿+万 (job 149: «R$ 750 milhões» → 7亿5000万), em que o valor
    reescalado vem repartido em DOIS números.
    """
    budget = Counter(original)

    def pair(new: str, *, swap: bool) -> bool:
        # Cobrir uma perda primeiro: com «20» e «200» no original, 20000万 é o 200
        # perdido ×100, não o 20 ×1000 — o greedy na ordem do texto escolhia o 20.
        for old in sorted(budget, key=lambda o: lost[o] <= 0):
            if budget[old] <= 0 or (swap and lost[old] <= 0):
                continue
            try:
                value = float(new)
            except ValueError:
                continue
            if not _zh_ratio_ok(value, old, _ZH_FACTORS):
                continue
            budget[old] -= 1
            if swap:
                lost[old] -= 1
                lost[new] += 1
            else:
                if lost[old] > 0:
                    lost[old] -= 1
                added[new] -= 1
            return True
        return False

    # 0.ª passagem: decomposição 亿+万 (job 149). Milhões e bilhões «quebrados» não têm UM
    # número reescalado: «R$ 750 milhões» vira 7亿5000万 — dois números novos (7 e 5000) que
    # juntos são o 750 ×100 (em 万) — e «R$ 3,89 bilhões» vira 38亿9000万 = 38.9亿, o 3.89 ×10.
    # O valor composto é que se compara (em 万 e em 亿; também ×1: 2亿5800万 = 2.58亿); num
    # par os dois componentes saem dos novos e a «perda» do original é desfeita.
    combo_spans: list[tuple[int, int]] = []
    for combo in _ZH_COMBO.finditer(translated):
        part_a, part_b = _normal(combo.group(1)), _normal(combo.group(2))
        try:
            in_wan = float(part_a) * 10000 + float(part_b)
        except ValueError:
            continue
        if added[part_a] <= 0 and added[part_b] <= 0:
            continue
        for value in (in_wan, in_wan / 10000):
            old = next((o for o in sorted(budget, key=lambda o: lost[o] <= 0)
                        if budget[o] > 0 and _zh_ratio_ok(value, o, (1.0, *_ZH_FACTORS))), None)
            if old is None:
                continue
            budget[old] -= 1
            if lost[old] > 0:
                lost[old] -= 1
            for part in (part_a, part_b):
                if added[part] > 0:
                    added[part] -= 1
            combo_spans.append(combo.span())
            break

    occurrences = Counter(
        _normal(m.group(1)) for m in _ZH_SCALED.finditer(translated)
        if not any(s <= m.start() < e for s, e in combo_spans))
    for new, count in occurrences.items():        # 1.ª: ocorrências que colidem com o original
        for _ in range(count - added[new]):
            if not pair(new, swap=True):
                break
    for new, count in occurrences.items():        # 2.ª: números novos reescalados
        for _ in range(min(count, added[new])):
            if not pair(new, swap=False):
                break


def _number_flaws(original: str, translated: str, language: str, where: str) -> list[str]:
    before, after = numbers(original), numbers(translated)
    repeated = Counter(_normal(m.group(1)) for m in _REPEATED.finditer(plain_text(original)))
    lost_all = (before - repeated) - after       # a repetição «de N em N» pode virar uma menção
    lost_all -= glossed_numbers(original)        # citação + tradução entre parênteses fundem-se
    # Números novos só são aceites se o original os escreve por extenso (meses, dois a doze).
    added_all = (after - before) - spelled_numbers(original)
    added_all -= ordinal_numbers(original, translated)   # «terceiro» → «3rd»/第3 (job 149)
    added_all -= unit_rate_numbers(original, translated)  # «por dólar» → 每投入1美元 (job 194)
    added_all -= scale_word_numbers(original, translated)  # «um milhão» → 100万 (job 218)
    added_all -= paren_gloss_numbers(original, translated)  # «November 7 (7/11)» (job 222)
    if added_all["1"]:
        implied = len(_ONE_IMPLIED.findall(plain_text(original)))
        added_all["1"] = max(0, added_all["1"] - implied)
        added_all = +added_all
    if language == "zh-CN":
        _zh_rescaled(lost_all, added_all, plain_text(translated), before)
        lost_all, added_all = +lost_all, +added_all
    if not lost_all and not added_all:
        return []
    lost = sorted(lost_all.elements())[:5]
    added = sorted(added_all.elements())[:5]
    return [f"números {where}diferentes do original (perdidos {lost}, novos {added})"]


def check_translation(original_html: str, translated_html: str, *, title: str = "",
                      excerpt: str = "", original_title: str = "", original_excerpt: str = "",
                      language: str = "") -> list[str]:
    """A tradução tem de preservar todos os links e todos os números do original."""
    problems: list[str] = check_plain(título=title, resumo=excerpt)
    if translated_html != sanitize_html(translated_html):
        problems.append("HTML não sanitizado")
    if sorted(links(original_html)) != sorted(links(translated_html)):
        missing = Counter(links(original_html)) - Counter(links(translated_html))
        extra = Counter(links(translated_html)) - Counter(links(original_html))
        problems.append(f"links diferentes do original (faltam {sum(missing.values())}, "
                        f"a mais {sum(extra.values())})")
    problems.extend(_number_flaws(original_html, translated_html, language, ""))
    # Uma data numérica que o original não tem é ambígua (1/10/2026 é 10 de janeiro para um
    # americano): a tradução escreve o mês por extenso (E14, 2026-10-07).
    new_dates = (Counter(m.group(0) for m in _NUMERIC_DATE.finditer(plain_text(translated_html)))
                 - Counter(m.group(0) for m in _NUMERIC_DATE.finditer(plain_text(original_html))))
    if new_dates:
        problems.append("datas numéricas ambíguas (escreva o mês por extenso): "
                        + ", ".join(sorted(new_dates)[:3]))
    if original_title or original_excerpt:
        problems.extend(_number_flaws(
            f"<p>{original_title}</p><p>{original_excerpt}</p>",
            f"<p>{title}</p><p>{excerpt}</p>", language, "do título ou do resumo "))
    # Em caracteres e com margem larga: o chinês diz o mesmo em muito menos caracteres.
    if len(plain_text(translated_html)) < len(plain_text(original_html)) * 0.15:
        problems.append("tradução muito mais curta do que o original")
    if len(plain_text(translated_html)) > len(plain_text(original_html)) * 2 + 500:
        problems.append("tradução muito mais longa do que o original")
    return problems


def new_number_context(original_html: str, translated_html: str, *, width: int = 20,
                       limit: int = 4) -> list[str]:
    """Trechos da tradução onde um número aparece mais vezes do que no original (diagnóstico).

    Só serve para quem for ver porque a trava de números bloqueou: é aproximado (não conta os
    meses por extenso nem a escala 万/亿, que `_number_flaws` já trata), e nunca decide nada.
    """
    before = numbers(original_html)
    plain = plain_text(translated_html)
    seen: Counter[str] = Counter()
    snippets: list[str] = []
    for found in _NUMBER.finditer(plain):
        number = _normal(found.group(0))
        seen[number] += 1
        if seen[number] > before[number]:
            start, end = max(0, found.start() - width), found.end() + width
            snippets.append(f"«{plain[start:end]}»")
            if len(snippets) == limit:
                break
    return snippets
