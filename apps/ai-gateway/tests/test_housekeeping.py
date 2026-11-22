"""R31: limpeza dos volumes das CLIs — só a lista fechada, por idade, nunca credenciais."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import housekeeping
from app.config import settings
from app.main import app

NOW = 2_000_000_000.0
OLD = NOW - 30 * housekeeping.DAY
FRESH = NOW - 3600


@pytest.fixture
def state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    found = {cli: tmp_path / cli for cli in ("claude", "codex", "agy")}
    for path in found.values():
        path.mkdir()
    monkeypatch.setattr(settings, "claude_state_dir", str(found["claude"]))
    monkeypatch.setattr(settings, "codex_state_dir", str(found["codex"]))
    monkeypatch.setattr(settings, "agy_state_dir", str(found["agy"]))
    return found


def put(path: Path, mtime: float, data: bytes = b"x" * 100) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))
    return path


def test_apaga_so_o_velho_da_lista_e_nunca_credenciais(state):
    agy, codex, claude = state["agy"], state["codex"], state["claude"]
    old_log = put(agy / "antigravity-cli/log/cli-1.log", OLD)
    new_log = put(agy / "antigravity-cli/log/cli-2.log", FRESH)
    old_talk = put(agy / "antigravity-cli/conversations/abc/t.json", OLD)
    os.utime(old_talk.parent, (OLD, OLD))
    image = put(codex / "generated_images/x/a.png", OLD)
    os.utime(image.parent, (OLD, OLD))
    backup = put(claude / "backups/.claude.json.backup.1", OLD)
    lock = put(claude / ".oauth_refresh.lock.owner.tmp.ab", OLD)
    # Fora da lista: velhos, mas intocáveis.
    keep = [put(codex / "auth.json", OLD), put(codex / "logs_2.sqlite", OLD),
            put(claude / ".credentials.json", OLD), put(claude / ".claude.json", OLD),
            put(agy / "antigravity-cli/bin/agy", OLD), put(codex / "cache/x.json", OLD)]

    report = housekeeping.clean(NOW)
    assert report.removed == 5 and report.freed == 500 and report.errors == []
    for gone in (old_log, old_talk.parent, image.parent, backup, lock):
        assert not gone.exists(), gone
    assert new_log.exists()
    assert all(path.exists() for path in keep)


def test_pasta_com_ficheiro_recente_nao_e_velha(state):
    talk = state["agy"] / "antigravity-cli/brain/conversa"
    put(talk / "a.json", OLD)
    put(talk / "b.json", FRESH)
    os.utime(talk, (OLD, OLD))
    assert housekeeping.clean(NOW).removed == 0
    assert talk.exists()


def test_simulacao_nao_apaga(state):
    log = put(state["agy"] / "antigravity-cli/log/cli-1.log", OLD)
    assert housekeeping.clean(NOW, dry_run=True).removed == 1
    assert log.exists()


def test_nao_segue_ligacoes_simbolicas(state, tmp_path):
    outside = tmp_path / "fora"
    precious = put(outside / "importante.txt", OLD)
    os.utime(outside, (OLD, OLD))
    folder = state["agy"] / "antigravity-cli/conversations"
    folder.mkdir(parents=True)
    (folder / "atalho").symlink_to(outside)
    # A própria pasta da regra como ligação para fora também é ignorada.
    (state["agy"] / "antigravity-cli/log").symlink_to(outside)
    assert housekeeping.clean(NOW).removed == 0
    assert precious.exists()


def test_tamanhos_por_volume_na_rota(state):
    put(state["codex"] / "a.bin", time.time(), b"x" * 2048)
    put(state["agy"] / "antigravity-cli/log/x.log", time.time(), b"y" * 10)
    body = TestClient(app).get("/health/storage").json()
    assert body == {"claude": 0, "codex": 2048, "agy": 10}
