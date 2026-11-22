"""Limpeza dos volumes de estado das CLIs (R31).

As CLIs guardam estado e conversas nos mesmos volumes das credenciais: o `agy` grava um log
por sessão e a transcrição de cada chamada, o Codex deixa imagens geradas para trás. Nada
disso é preciso depois de alguns dias e tudo isso cresce sem limite (e mistura prompts com
as credenciais).

Regras:
- só se apaga o que está numa LISTA FECHADA de pastas (`RULES`), por idade (mtime);
- nunca credenciais (`auth.json`, `.credentials.json`, `.claude.json`), configuração nem os
  bancos sqlite das CLIs — esses não estão na lista;
- nunca se seguem ligações simbólicas, e nada fora da pasta de estado da CLI;
- a limpeza corre no próprio gateway, uma vez por dia (`housekeeping_interval_h`).

`sizes()` mede cada volume; o motor lê em `/health/storage` e avisa por e-mail acima do teto.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings

DAY = 86_400


@dataclass(frozen=True)
class Rule:
    cli: str              # claude, codex ou agy
    folder: str           # relativa à pasta de estado da CLI
    pattern: str          # glob dentro da pasta (só o primeiro nível)
    days: int             # idade mínima para apagar


RULES: tuple[Rule, ...] = (
    # agy: um log por sessão da CLI, transcrições e o «cérebro» de cada conversa.
    Rule("agy", "antigravity-cli/log", "*.log", 7),
    Rule("agy", "antigravity-cli/conversations", "*", 7),
    Rule("agy", "antigravity-cli/brain", "*", 7),
    Rule("agy", "antigravity-cli/annotations", "*", 7),
    # Codex: o gateway lê e apaga a imagem de cada pedido; isto apanha as que sobraram.
    Rule("codex", "generated_images", "*", 2),
    # Claude: cópias da configuração que a própria CLI faz, e bloqueios esquecidos.
    Rule("claude", "backups", ".claude.json.backup.*", 14),
    Rule("claude", ".", ".oauth_refresh.lock.owner.tmp.*", 7),
)


def roots() -> dict[str, Path]:
    return {"claude": Path(settings.claude_state_dir), "codex": Path(settings.codex_state_dir),
            "agy": Path(settings.agy_state_dir)}


def _size(path: Path) -> int:
    if path.is_symlink():
        return 0
    if path.is_file():
        return path.stat().st_size
    total = 0
    for base, dirs, files in os.walk(path, followlinks=False):
        for name in files:
            full = Path(base, name)
            if not full.is_symlink():
                try:
                    total += full.stat().st_size
                except OSError:
                    continue
        dirs[:] = [d for d in dirs if not Path(base, d).is_symlink()]
    return total


def _newest(path: Path) -> float:
    """O mtime mais recente da entrada (numa pasta, o de qualquer ficheiro dentro dela):
    uma conversa antiga que recebeu uma linha hoje não é velha."""
    if path.is_file() or path.is_symlink():
        return path.lstat().st_mtime
    newest = path.lstat().st_mtime
    for base, dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                newest = max(newest, Path(base, name).lstat().st_mtime)
            except OSError:
                continue
        dirs[:] = [d for d in dirs if not Path(base, d).is_symlink()]
    return newest


@dataclass
class Report:
    removed: int = 0
    freed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {"removed": self.removed, "freed_bytes": self.freed, "errors": self.errors[:10]}


def clean(now: float | None = None, *, dry_run: bool = False) -> Report:
    """Apaga o que as `RULES` permitem. Devolve quanto apagou (ou apagaria, em `dry_run`)."""
    moment = time.time() if now is None else now
    report = Report()
    for rule in RULES:
        root = roots()[rule.cli].resolve()
        folder = (root / rule.folder)
        if folder.is_symlink() or not folder.is_dir():
            continue
        folder = folder.resolve()
        if folder != root and root not in folder.parents:
            continue                          # nunca fora da pasta de estado da CLI
        for entry in folder.glob(rule.pattern):
            if entry.is_symlink() or entry.parent != folder:
                continue
            try:
                if moment - _newest(entry) < rule.days * DAY:
                    continue
                size = _size(entry)
                if not dry_run:
                    if entry.is_dir():
                        shutil.rmtree(entry)
                    else:
                        entry.unlink()
                report.removed += 1
                report.freed += size
            except OSError as exc:
                report.errors.append(f"{rule.cli}/{rule.folder}/{entry.name}: {exc.strerror}")
    return report


def sizes() -> dict[str, int]:
    """Bytes ocupados pela pasta de estado de cada CLI (0 se não existir)."""
    return {cli: (_size(path) if path.is_dir() else 0) for cli, path in roots().items()}


async def loop() -> None:
    """Limpeza periódica, em segundo plano no gateway. Falhar nunca derruba o gateway."""
    await asyncio.sleep(settings.housekeeping_first_delay_s)
    while True:
        # A limpeza é acessória: um erro nela nunca derruba o gateway; volta no dia seguinte.
        with contextlib.suppress(Exception):
            await asyncio.to_thread(clean)
        await asyncio.sleep(settings.housekeeping_interval_h * 3600)
