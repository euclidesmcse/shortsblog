"""D-070 — sítios que o motor não lê para a IA (lista de content-config/sem-leitura.yaml)."""
from __future__ import annotations

import pytest

from app.core.config import settings
from app.services import terms


@pytest.fixture
def lista(tmp_path, monkeypatch):
    def write(body: str) -> None:
        path = tmp_path / "sem-leitura.yaml"
        path.write_text(body, encoding="utf-8")
        monkeypatch.setattr(settings, "no_fetch_config", str(path))
        terms._load.cache_clear()
    yield write
    terms._load.cache_clear()


def test_dominio_e_subdominios_ficam_sem_leitura(lista):
    lista("sites:\n- {domain: www.Exemplo.test, reason: proíbe robôs}\n"
          "- {domain: globo.test, reason: IA proibida}\n")
    assert not terms.readable("https://exemplo.test/a")
    assert not terms.readable("https://www.exemplo.test/a")
    assert not terms.readable("https://noticias.globo.test/x")
    assert terms.readable("https://outroexemplo.test/a")     # não é subdomínio
    assert terms.readable("https://globo.test.evil.example/a")


@pytest.mark.parametrize(("body", "fragment"), [
    ("sites:\n- {domain: 'https://a.test/x', reason: r}\n", "domínio inválido"),
    ("sites:\n- {domain: a.test}\n", "falta o motivo"),
])
def test_lista_invalida(lista, body, fragment):
    lista(body)
    with pytest.raises(terms.TermsConfigError, match=fragment):
        terms.no_fetch_domains()


def test_lista_real_e_valida_e_cobre_os_casos_da_revisao():
    terms._load.cache_clear()
    domains = terms.no_fetch_domains()
    for url in ("https://aws.amazon.com/blogs/aws/x", "https://www.theguardian.com/world/x",
                "https://www.bbc.com/news/x", "https://valorinveste.globo.com/x",
                "https://www.nytimes.com/2026/x"):
        assert not terms.readable(url), url
    assert terms.readable("https://www.gov.br/planalto/x")
    assert len(domains) >= 40
