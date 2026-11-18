"""E7 — a imagem de cada artigo, com licença comprovada.

Ordem: foto pública com licença permitida (Openverse e Wikimedia Commons, só CC0 e domínio
público, D-012) que faça sentido com o tema; se não houver, ilustração gerada por IA
(Cloudflare Workers AI) a partir de um conceito abstrato — nunca pessoas, marcas, nomes
próprios nem factos noticiosos realistas. Sem nenhuma das duas, o artigo fica sem imagem
(o tema mostra um gradiente) e nada bloqueia.

Regras que atravessam tudo:
- os pedidos a terceiros passam pelo fetcher seguro (E6.2: só https, sem IPs internos);
- a licença vem da resposta da API e fica guardada com o endereço de prova;
- "sem autoria visível" não é licença: sem licença permitida, a imagem não entra;
- a imagem é decodificada, medida, recortada e convertida em WebP pelo próprio motor;
- o texto alternativo e a legenda existem nos 4 idiomas.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError
from sqlalchemy import text

from app.core.config import settings
from app.core.db import session_scope
from app.core.logging import get_logger
from app.services import fetcher
from app.services.ai import chain, quota
from app.services.ai.prompting import External

log = get_logger(__name__)

LANGS = ("pt-BR", "en", "es", "zh-CN")
AI_CAPTION = {"pt-BR": "Imagem gerada por IA", "en": "AI-generated image",
              "es": "Imagen generada por IA", "zh-CN": "AI生成的图片"}
TARGET = (1600, 900)                       # 16:9, o formato dos cartões e da capa
OPENVERSE = "https://api.openverse.org/v1/images/"
COMMONS = "https://commons.wikimedia.org/w/api.php"

_transport: httpx.BaseTransport | None = None      # os testes trocam por MockTransport


class Refused(Exception):
    """A imagem ou o pedido não cumpre uma regra (licença, tamanho, prompt)."""


@dataclass(frozen=True)
class Candidate:
    provider: str              # openverse | commons
    image_url: str             # ficheiro a descarregar
    landing_url: str           # página de origem (prova)
    license: str               # cc0 | pdm
    license_url: str
    author: str
    title: str
    tags: tuple[str, ...] = ()
    width: int = 0
    height: int = 0


@dataclass
class Illustration:
    provider: str
    license: str
    license_url: str
    source_url: str
    author: str
    title: str
    credit: str
    ai_generated: bool
    prompt: str
    alt_texts: dict[str, str]
    captions: dict[str, str]
    webp: bytes = field(repr=False, default=b"")
    width: int = 0
    height: int = 0
    sha256: str = ""
    path: str = ""


# --- Licenças -------------------------------------------------------------------------------

def normalise_license(raw: str) -> str | None:
    """'cc0' ou 'pdm' se a licença é uma das permitidas; senão None."""
    value = re.sub(r"[\s_-]+", " ", raw.strip().lower())
    if value in ("cc0", "cc0 1.0", "cc zero", "creative commons zero"):
        return "cc0"
    if value in ("pdm", "public domain", "public domain mark", "pd", "pdm 1.0"):
        return "pdm"
    return None


def _api_get(url: str) -> Any:
    page = fetcher.fetch(url, max_bytes=2 * 1024 * 1024, accept="application/json",
                         respect_robots=False)
    return json.loads(page.body)


# --- Bancos de imagens ----------------------------------------------------------------------

def search_openverse(query: str, limit: int = 15) -> list[Candidate]:
    url = OPENVERSE + "?" + urlencode({
        "q": query, "license": "cc0,pdm", "category": "photograph", "aspect_ratio": "wide",
        "size": "large", "mature": "false", "page_size": limit})
    out = []
    for item in _api_get(url).get("results", []):
        lic = normalise_license(str(item.get("license", "")))
        image = str(item.get("url") or "")
        if not lic or not image.startswith("https://"):
            continue
        out.append(Candidate(
            provider="openverse", image_url=image,
            landing_url=str(item.get("foreign_landing_url") or item.get("detail_url") or ""),
            license=lic, license_url=str(item.get("license_url") or ""),
            author=str(item.get("creator") or ""), title=str(item.get("title") or "")[:200],
            tags=tuple(str(t.get("name", "")) for t in (item.get("tags") or [])[:8]),
            width=int(item.get("width") or 0), height=int(item.get("height") or 0)))
    return out


def _plain(html: str) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html or ""))).strip()


def search_commons(query: str, limit: int = 15) -> list[Candidate]:
    url = COMMONS + "?" + urlencode({
        "action": "query", "format": "json", "generator": "search", "gsrnamespace": 6,
        "gsrsearch": f"{query} filetype:bitmap", "gsrlimit": limit, "prop": "imageinfo",
        "iiprop": "url|size|mime|extmetadata", "iiurlwidth": TARGET[0]})
    out = []
    for page in (_api_get(url).get("query", {}).get("pages") or {}).values():
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata") or {}
        lic = normalise_license(str((meta.get("LicenseShortName") or {}).get("value", "")))
        image = str(info.get("thumburl") or info.get("url") or "")
        if not lic or not image.startswith("https://") or "image" not in str(info.get("mime", "")):
            continue
        out.append(Candidate(
            provider="commons", image_url=image,
            landing_url=str(info.get("descriptionurl") or ""), license=lic,
            license_url=str((meta.get("LicenseUrl") or {}).get("value", "")),
            author=_plain(str((meta.get("Artist") or {}).get("value", "")))[:120],
            title=_plain(str((meta.get("ObjectName") or {}).get("value", "")))[:200]
            or str(page.get("title", ""))[:200],
            tags=(), width=int(info.get("thumbwidth") or info.get("width") or 0),
            height=int(info.get("thumbheight") or info.get("height") or 0)))
    return out


# Títulos/etiquetas de imagens com texto, logotipos ou composição: não servem de capa.
_NOT_A_PHOTO = re.compile(
    r"\b(map|maps|infographic|infographics|diagram|chart|graph|poster|flyer|banner|logo|logos|"
    r"screenshot|collage|composite|illustration|clipart|vector|slide|presentation|"
    r"cartography|schematic)\b", re.I)


def find_candidates(queries: list[str]) -> list[Candidate]:
    """Junta os resultados dos dois bancos; um banco em baixo não impede o outro."""
    seen: set[str] = set()
    found: list[Candidate] = []
    with session_scope() as db:
        used = set(db.execute(text("SELECT source_url FROM image_assets "
                                   "WHERE source_url IS NOT NULL")).scalars())
    for query in queries[:3]:
        for search in (search_openverse, search_commons):
            try:
                results = search(query)
            except (fetcher.FetchError, ValueError, KeyError, AttributeError) as exc:
                log.warning("Busca de imagens (%s) falhou: %s", search.__name__, exc)
                continue
            for cand in results:
                if (cand.image_url in seen or cand.image_url in used
                        or _NOT_A_PHOTO.search(f"{cand.title} {' '.join(cand.tags)}")
                        or (cand.width and cand.width < settings.illustration_min_width)):
                    continue
                seen.add(cand.image_url)
                found.append(cand)
    return found[:20]


# --- Imagem: validar e converter --------------------------------------------------------------

def prepare(raw: bytes) -> tuple[bytes, int, int, str]:
    """Decodifica, mede, recorta em 16:9 e devolve (webp, largura, altura, sha256 do original)."""
    if len(raw) > settings.illustration_max_bytes:
        raise Refused("ficheiro grande demais")
    try:
        with Image.open(io.BytesIO(raw)) as probe:
            if probe.width * probe.height > 60_000_000:
                raise Refused("imagem com pixéis a mais")
            if getattr(probe, "is_animated", False):
                raise Refused("imagem animada")
            if probe.width < settings.illustration_min_width or probe.height < 500:
                raise Refused(f"imagem pequena ({probe.width}x{probe.height})")
            image = ImageOps.exif_transpose(probe).convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise Refused(f"não é uma imagem válida: {exc}") from None
    image = ImageOps.fit(image, TARGET, Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    image.save(buf, format="WEBP", quality=82, method=6)
    return buf.getvalue(), image.width, image.height, hashlib.sha256(raw).hexdigest()


def download(cand: Candidate) -> bytes:
    # O endereço vem da API do banco, não do artigo; o robots.txt dos CDNs de imagem
    # não se aplica a downloads que a própria API oferece.
    page = fetcher.fetch(cand.image_url, max_bytes=settings.illustration_max_bytes,
                         accept="image/*", respect_robots=False)
    if not page.content_type.lower().startswith("image/"):
        raise Refused(f"o endereço não devolveu uma imagem ({page.content_type})")
    return page.body


# --- Geração por IA --------------------------------------------------------------------------

_BANNED = re.compile(
    r"\b(person|people|man|men|woman|women|child|children|kid|boy|girl|human|crowd|face|faces|"
    r"portrait|selfie|politician|president|minister|senator|candidate|mayor|king|queen|"
    r"celebrity|logo|logos|brand|trademark|flag|flags|banner|poster|screenshot|photorealistic|"
    r"realistic photo|news|breaking|headline|explosion|fire|crash|riot|protest|war|"
    r"soldier|weapon|gun|blood|text|letters|word|words|caption|watermark|nude|naked)\b", re.I)
STYLE = ("Abstract editorial illustration, flat vector style, clean geometric shapes, "
         "soft gradients, no people, no text, no logos: ")


def check_ai_prompt(concept: str) -> str:
    """Valida o conceito e devolve o prompt final. Levanta `Refused`.

    O conceito tem de ser uma cena abstrata em inglês: sem pessoas, marcas, nomes próprios
    (qualquer palavra com maiúscula, a primeira incluída: "Lula giving…" não se distingue de
    "Cubes giving…" e o pedido ao modelo é para escrever tudo em minúsculas) nem factos
    noticiosos (E7.3).
    """
    clean = re.sub(r"\s+", " ", concept).strip().rstrip(".")
    if not 12 <= len(clean) <= 300:
        raise Refused("conceito vazio ou longo demais")
    if _BANNED.search(clean):
        raise Refused(f"conceito com termo proibido: {_BANNED.search(clean).group(0)!r}")  # type: ignore[union-attr]
    words = re.findall(r"[^\W\d_]+(?:['’-][^\W\d_]+)*", clean, flags=re.UNICODE)
    for word in words:
        if word[0].isupper():
            raise Refused(f"conceito com nome próprio: {word!r}")
    if re.search(r"[^\x00-\x7F]", clean):
        raise Refused("conceito tem de estar em inglês simples")
    return STYLE + clean


def generate_ai(concept: str, *, site: str | None = None, job_id: int | None = None) -> bytes:
    """Gera a imagem pelo Codex, no gateway (D-056). Levanta `Refused` se o conceito não passa
    ou se o provedor não está disponível; o artigo vale sem capa. `job_id` liga a tentativa ao
    job em `provider_usage` — fora do pipeline (ex.: `scripts/ai_cover.py`) ninguém mais o faz."""
    prompt = check_ai_prompt(concept)
    try:
        if job_id is not None:
            with quota.for_job(job_id):
                return chain.run_image(prompt, site=site)
        return chain.run_image(prompt, site=site)
    except chain.ImageUnavailable as exc:
        raise Refused(f"geração por IA indisponível ({exc})") from None


# --- Escolha (LLM) ----------------------------------------------------------------------------

_PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "queries": {"type": "array", "items": {"type": "string"}},
        "concept": {"type": "string"},
    },
    "required": ["queries", "concept"], "additionalProperties": False,
}
_ALT = {"type": "object",
        "properties": {lang: {"type": "string"} for lang in LANGS},
        "required": list(LANGS), "additionalProperties": False}
_PICK_SCHEMA = {
    "type": "object",
    "properties": {"pick": {"type": "integer"}, "alt": _ALT, "alt_ai": _ALT},
    "required": ["pick", "alt", "alt_ai"], "additionalProperties": False,
}
# P4 do ensaio E14 (3/11): plano e escolha numa só chamada.
_MERGED_SCHEMA = {
    "type": "object",
    "properties": {"pick": {"type": "integer"}, "alt": _ALT, "alt_ai": _ALT,
                   "queries": {"type": "array", "items": {"type": "string"}},
                   "concept": {"type": "string"}},
    "required": ["pick", "alt", "alt_ai", "queries", "concept"], "additionalProperties": False,
}

# As regras são as mesmas no caminho de duas chamadas (plan + pick) e na chamada fundida.
_QUERIES_RULES = (
    "- `queries`: 3 pesquisas curtas EM INGLÊS (2 a 4 palavras) para um banco de fotos, do "
    "mais concreto ao mais geral (ex.: 'server room', 'data center', 'network cables'). "
    "Descrevem o que se VÊ numa foto genérica, nunca o título nem nomes de pessoas, "
    "empresas ou produtos.\n")
_CONCEPT_RULES = (
    "- `concept`: uma cena abstrata EM INGLÊS simples (uma frase, até 25 palavras) para uma "
    "ilustração, caso nenhuma foto sirva. Sem pessoas, sem marcas, sem nomes próprios, sem "
    "texto, sem notícias nem acidentes; só objetos, formas e metáforas visuais, tudo em "
    "minúsculas (ex.: 'interlocking blue cubes forming a protective shield over a network "
    "of glowing nodes').")
_PICK_RULES = (
    "Escolhe a foto que melhor ilustra o artigo. Só serve se mostrar algo coerente com o "
    "tema (um objeto, lugar ou conceito certo); na dúvida, devolve `pick` = -1. Nunca "
    "escolhas fotos de pessoas identificáveis nem com texto/logotipos visíveis, a julgar "
    "pelo título e etiquetas; nem mapas, gráficos, cartazes ou imagens cheias de texto. "
    "`alt` é o texto alternativo da foto escolhida (descreve o "
    "que se vê, até 125 caracteres) nos 4 idiomas; o português é o do BRASIL "
    "(pt-BR: \"gerenciar\", \"grade\", \"flutuando\", \"arquivo\"), nunca o de Portugal "
    "(nada de \"a flutuar\", \"grelha\", \"ficheiro\"); se `pick` = -1, repete a descrição do "
    "conceito.")


def _listing(candidates: list[Candidate]) -> str:
    return "\n".join(
        f"{i}: {c.title or '(sem título)'} | etiquetas: {', '.join(c.tags) or '—'} | {c.provider}"
        for i, c in enumerate(candidates)) or "(nenhuma foto encontrada)"


def _clean_queries(data: dict[str, Any]) -> list[str]:
    queries = [re.sub(r"\s+", " ", str(q)).strip()[:80] for q in data.get("queries", [])]
    return [q for q in queries if q]


def plan(title: str, excerpt: str, *, site: dict[str, Any], job_id: int) -> dict[str, Any]:
    task = ("Vais escolher a imagem de capa de um artigo de blog. Devolve:\n"
            + _QUERIES_RULES + _CONCEPT_RULES)
    result = chain.run("researcher", task, schema=_PLAN_SCHEMA, purpose="illustration_plan",
                       site=site["site_key"], system="És diretor de arte de um blog.",
                       external=[External("artigo", f"TÍTULO: {title}\nRESUMO: {excerpt}")])
    data = result.data or {}
    return {"queries": _clean_queries(data), "concept": str(data.get("concept", ""))}


def pick(title: str, excerpt: str, candidates: list[Candidate], concept: str, *,
         site: dict[str, Any], job_id: int) -> tuple[int, dict[str, str], dict[str, str]]:
    """Devolve (índice escolhido ou -1, alt da foto escolhida, alt da ilustração de reserva)."""
    task = (_PICK_RULES +
            " `alt_ai` descreve a ilustração do conceito de reserva, nos 4 idiomas: "
            f"«{concept}».")
    result = chain.run("researcher", task, schema=_PICK_SCHEMA, purpose="illustration_pick",
                       site=site["site_key"], system="És diretor de arte de um blog.",
                       external=[External(
                           "artigo e fotos",
                           f"TÍTULO: {title}\nRESUMO: {excerpt}\n\nFOTOS:\n"
                           f"{_listing(candidates)}")])
    data = result.data or {}
    index = data.get("pick")
    if not isinstance(index, int) or not 0 <= index < len(candidates):
        index = -1
    return index, _clean_alt(data.get("alt")), _clean_alt(data.get("alt_ai"))


def plan_pick(title: str, excerpt: str, candidates: list[Candidate], *,
              site: dict[str, Any], job_id: int) -> dict[str, Any]:
    """Plano e escolha numa só chamada (P4 do ensaio E14, 3/11).

    As fotos vieram das palavras do título em inglês (`_keyword_queries`), sem gastar chamada.
    O modelo escolhe uma delas (ou -1) e devolve, na mesma resposta, as pesquisas para uma
    segunda leva e o conceito de reserva para a ilustração por IA."""
    task = (
        "Vais tratar da imagem de capa de um artigo de blog, numa só resposta. O bloco "
        "«artigo e fotos» traz o artigo e as fotos de uma primeira busca (pode não haver "
        "nenhuma). " + _PICK_RULES + " Devolve também, sempre:\n"
        + _QUERIES_RULES
        + "Usam-se só se devolveres `pick` = -1: uma segunda busca procura fotos melhores.\n"
        + _CONCEPT_RULES +
        "\n- `alt_ai`: descreve a ilustração desse conceito, nos 4 idiomas, como o `alt`.")
    result = chain.run("researcher", task, schema=_MERGED_SCHEMA, purpose="illustration",
                       site=site["site_key"], system="És diretor de arte de um blog.",
                       external=[External(
                           "artigo e fotos",
                           f"TÍTULO: {title}\nRESUMO: {excerpt}\n\nFOTOS:\n"
                           f"{_listing(candidates)}")])
    data = result.data or {}
    index = data.get("pick")
    if not isinstance(index, int) or not 0 <= index < len(candidates):
        index = -1
    return {"index": index, "alt": _clean_alt(data.get("alt")),
            "alt_ai": _clean_alt(data.get("alt_ai")),
            "queries": _clean_queries(data), "concept": str(data.get("concept", ""))}


# Palavras do título em inglês que não descrevem o que se vê numa foto.
_STOPWORDS = frozenset((  # noqa: SIM905 - uma palavra por lista literal seria ilegível
    "a an the and or but of in on at to for with from by as is are was were be been being "
    "after before over under between into its it this that these those their his her he she "
    "they we you your not no new old how why what when where who which while will would can "
    "could may might must should shall about against amid among more most than then also say "
    "says said up down out off do does did done have has had get gets got").split())


def _keyword_queries(title_en: str) -> list[str]:
    """Pesquisas de banco de fotos tiradas do título em inglês, sem chamada de IA (P4).

    São só a primeira leva: se nenhuma foto servir, o modelo devolve pesquisas melhores
    na mesma chamada em que escolhe (`plan_pick`)."""
    words = [w for w in re.findall(r"[a-z][a-z'-]*", title_en.lower())
             if w not in _STOPWORDS and len(w) > 2]
    queries = [" ".join(words[:4]), " ".join(words[:2]), " ".join(words[2:5])]
    return list(dict.fromkeys(q for q in queries if q))


def _clean_alt(value: Any) -> dict[str, str]:
    data = value if isinstance(value, dict) else {}
    clean: dict[str, str] = {}
    for lang in LANGS:
        alt = re.sub(r"\s+", " ", str(data.get(lang, ""))).strip()
        if len(alt) > 125:
            cut = alt[:125]
            # Corta em fim de palavra; em chinês não há espaços e o corte seco serve.
            alt = cut.rsplit(" ", 1)[0] if " " in cut else cut
        clean[lang] = alt
    return clean


# --- Montagem -----------------------------------------------------------------------------------

_CREDIT = {  # idioma → (rótulo, autor desconhecido, CC0, domínio público)
    "pt-BR": ("Foto", "autor não indicado", "CC0", "domínio público"),
    "en": ("Photo", "author not stated", "CC0", "public domain"),
    "es": ("Foto", "autor no indicado", "CC0", "dominio público"),
    "zh-CN": ("图片", "作者未注明", "CC0", "公共领域"),
}


def credit_line(cand: Candidate, lang: str = "pt-BR") -> str:
    label, unknown, cc0, pdm = _CREDIT[lang]
    where = "Wikimedia Commons" if cand.provider == "commons" else "Openverse"
    return f"{label}: {cand.author or unknown} / {where} / {cc0 if cand.license == 'cc0' else pdm}"


def _photo(cand: Candidate, alt: dict[str, str], job_id: int) -> Illustration | None:
    """Descarrega e valida a foto escolhida; None (com aviso no log) se ela não serve."""
    try:
        webp, width, height, digest = prepare(download(cand))
    except (fetcher.FetchError, Refused) as exc:
        log.warning("Job %d: foto %s recusada (%s)", job_id, cand.image_url[:120], exc)
        return None
    return Illustration(
        provider=cand.provider, license=cand.license,
        license_url=cand.license_url or cand.landing_url,
        source_url=cand.image_url, author=cand.author, title=cand.title,
        credit=credit_line(cand), ai_generated=False, prompt="", alt_texts=alt,
        captions={lang: credit_line(cand, lang) for lang in LANGS}, webp=webp,
        width=width, height=height, sha256=digest)


def _choose(title: str, excerpt: str, candidates: list[Candidate], concept: str, *,
            site: dict[str, Any], job_id: int, rounds: int = 3,
            alt_ai: dict[str, str] | None = None) -> tuple[Illustration | None, dict[str, str]]:
    """Devolve (foto escolhida ou None, alt da ilustração de reserva).

    Até `rounds` escolhas: uma foto recusada no download (pequena, 404…) sai da lista e o
    modelo escolhe outra, em vez de se cair logo na IA."""
    alt_ai = alt_ai or {}
    for _ in range(rounds):
        index, alt, alt_ai = pick(title, excerpt, candidates, concept, site=site, job_id=job_id)
        if index < 0:
            break
        photo = _photo(candidates[index], alt, job_id)
        if photo is not None:
            return photo, alt_ai
        candidates.pop(index)
    return None, alt_ai


def build(title: str, excerpt: str, *, site: dict[str, Any], job_id: int,
          title_en: str = "") -> Illustration | None:
    """Escolhe ou gera a imagem do artigo. None se nada serve.

    Com `title_en` (o pipeline o tem, da tradução), plano e escolha saem numa só chamada
    (P4 do ensaio E14): a primeira leva de fotos vem das palavras do título, de graça, e as
    pesquisas do modelo só se gastam numa segunda leva se nenhuma servir. Sem `title_en`
    (scripts avulsos), vale o caminho antigo de duas chamadas."""
    if title_en:
        candidates = find_candidates(_keyword_queries(title_en))
        merged = plan_pick(title, excerpt, candidates, site=site, job_id=job_id)
        concept, alt_ai = merged["concept"], merged["alt_ai"]
        photo = (_photo(candidates[merged["index"]], merged["alt"], job_id)
                 if merged["index"] >= 0 else None)
        if photo is None and merged["queries"]:
            # Segunda leva, com as pesquisas do modelo (equivale ao caminho antigo).
            tried = candidates[merged["index"]].image_url if merged["index"] >= 0 else ""
            more = [c for c in find_candidates(merged["queries"]) if c.image_url != tried]
            if more:
                photo, alt_ai = _choose(title, excerpt, more, concept, site=site,
                                        job_id=job_id, rounds=2, alt_ai=alt_ai)
    else:
        steps = plan(title, excerpt, site=site, job_id=job_id)
        candidates = find_candidates(steps["queries"]) if steps["queries"] else []
        concept = steps["concept"]
        photo, alt_ai = _choose(title, excerpt, candidates, concept, site=site, job_id=job_id)
    if photo is not None:
        return photo
    try:
        raw = generate_ai(concept, site=site["site_key"], job_id=job_id)
        webp, width, height, digest = prepare(raw)
    except (Refused, httpx.HTTPError, ValueError) as exc:
        log.warning("Job %d: sem ilustração por IA (%s)", job_id, exc)
        return None
    return Illustration(
        provider="codex-image", license="ai-generated", license_url="", source_url="",
        author="", title="", credit=AI_CAPTION["pt-BR"], ai_generated=True,
        prompt=check_ai_prompt(concept), alt_texts=alt_ai, captions=dict(AI_CAPTION),
        webp=webp, width=width, height=height, sha256=digest)


def save(job_id: int, art: Illustration) -> str:
    """Grava o ficheiro e a linha de `image_assets` (uma por job)."""
    directory = Path(settings.media_dir) / "illustrations"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"job-{job_id}-{art.sha256[:12]}.webp"
    path.write_bytes(art.webp)
    with session_scope() as db:
        db.execute(text("DELETE FROM image_assets WHERE job_id = :j AND role = 'featured'"),
                   {"j": job_id})
        db.execute(
            text("""INSERT INTO image_assets
                      (job_id, role, provider, source_url, license, license_proof_url, author,
                       local_path, alt_text, sha256, width, height, title, credit, ai_generated,
                       prompt, alt_texts, captions)
                    VALUES (:j, 'featured', :p, :src, :lic, :proof, :au, :path, :alt, :sha, :w,
                            :h, :ti, :cr, :ai, :pr, CAST(:alts AS JSONB), CAST(:caps AS JSONB))"""),
            {"j": job_id, "p": art.provider, "src": art.source_url or None, "lic": art.license,
             "proof": art.license_url or None, "au": art.author, "path": str(path),
             "alt": art.alt_texts.get("pt-BR", ""), "sha": art.sha256, "w": art.width,
             "h": art.height, "ti": art.title, "cr": art.credit, "ai": art.ai_generated,
             "pr": art.prompt, "alts": json.dumps(art.alt_texts, ensure_ascii=False),
             "caps": json.dumps(art.captions, ensure_ascii=False)})
    return str(path)


def illustrate(job_id: int, title: str, excerpt: str, *, site: dict[str, Any],
               title_en: str = "") -> str | None:
    """Ilustra o job. Nunca levanta por falha de imagem: o artigo vale sem ela."""
    if not settings.illustration_enabled:
        return None
    try:
        art = build(title, excerpt, site=site, job_id=job_id, title_en=title_en)
        return save(job_id, art) if art else None
    except chain.ChainExhausted as exc:
        log.warning("Job %d: sem IA para escolher a imagem (%s)", job_id, exc)
    except Exception:  # noqa: BLE001 - a imagem é opcional
        log.exception("Job %d: ilustração falhou", job_id)
    return None
