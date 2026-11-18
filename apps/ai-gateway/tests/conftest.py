"""CLIs falsas: scripts que imitam a saída real de cada CLI (formatos da E3.2).

Nenhum teste chama um provedor verdadeiro; o container de testes nem tem rede.
"""

from __future__ import annotations

import stat
import textwrap
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.providers.base import Provider
from app.providers.cli import AgyProvider, ClaudeProvider, CodexProvider

FakeCLI = Callable[[str], str]


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr(settings, "workdir", str(work))
    return work


@pytest.fixture
def fake_cli(tmp_path: Path, workdir: Path) -> FakeCLI:
    """Cria um executável Python com o corpo dado e devolve o caminho."""
    counter = iter(range(1000))

    def make(body: str) -> str:
        path = tmp_path / f"fake-cli-{next(counter)}"
        path.write_text(
            "#!/usr/bin/env python3\nimport json, os, sys, time\n" + textwrap.dedent(body)
        )
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    return make


@pytest.fixture
def client(workdir: Path) -> TestClient:
    return TestClient(app)


def install(name: str, provider: Provider) -> None:
    app.state.providers[name] = provider


@pytest.fixture(autouse=True)
def _restore_providers() -> object:
    saved = dict(app.state.providers)
    yield None
    app.state.providers.clear()
    app.state.providers.update(saved)


__all__ = ["AgyProvider", "ClaudeProvider", "CodexProvider", "FakeCLI", "install"]
