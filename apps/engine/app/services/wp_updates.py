"""R47: atualizações de plugins e temas à espera nos blogs.

Só o core se atualiza sozinho (`WP_AUTO_UPDATE_CORE=minor`); o Polylang e o tema esperam
mão humana (uma atualização pode quebrar o i18n da REST). O motor não tem como ler isto — o
usuário dele nos blogs é «autor» —, então o servidor confere com o wp-cli
(`scripts/wp-updates-check.sh`) e entrega o JSON aqui:

    docker exec -i sb-engine-api python -m app.services.wp_updates < relatorio.json

O motor guarda em `settings` (`wp_updates`) e o resumo diário lista o que há para aplicar
(e avisa se a conferência parou). Nunca atualiza nada.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.db import get_setting, session_scope

KEY = "wp_updates"
# Conferência mais velha que isto é dada como parada no resumo.
STALE_S = 3 * 86_400


def _entries(items: Any, kind: str) -> list[dict[str, str]]:
    out = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("update") != "available":
            continue
        out.append({"kind": kind, "name": str(item.get("name", ""))[:80],
                    "status": str(item.get("status", ""))[:20],
                    "version": str(item.get("version", ""))[:20],
                    "update_version": str(item.get("update_version", ""))[:20]})
    return out


def parse(report: Any) -> dict[str, Any]:
    """Do JSON do script (`{site: {plugins, themes} | {error}}`) ao que se guarda."""
    if not isinstance(report, dict):
        raise ValueError("o relatório tem de ser um objeto {site: …}")
    sites: dict[str, Any] = {}
    for site, data in report.items():
        if not isinstance(data, dict):
            continue
        if "error" in data:
            sites[str(site)[:40]] = {"error": str(data["error"])[:200]}
            continue
        sites[str(site)[:40]] = {"updates": [*_entries(data.get("plugins"), "plugin"),
                                             *_entries(data.get("themes"), "tema")]}
    return sites


def record(report: Any, now: float | None = None) -> dict[str, Any]:
    value = {"checked_at": float(now if now is not None else time.time()),
             "sites": parse(report)}
    with session_scope() as db:
        db.execute(
            text("""INSERT INTO settings (key, value, description)
                    VALUES (:k, CAST(:v AS JSONB), :d)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()"""),
            {"k": KEY, "v": json.dumps(value),
             "d": "Atualizações de plugins e temas à espera nos blogs (R47)"})
    return value


def summary(db: Session, now: float | None = None) -> dict[str, Any] | None:
    """Para o resumo diário: o que há para atualizar, agrupado por item, e se a conferência
    está velha ou falhou nalgum blog. None = o servidor nunca entregou (sem secção)."""
    value = get_setting(db, KEY)
    if not isinstance(value, dict) or "checked_at" not in value:
        return None
    moment = time.time() if now is None else now
    grouped: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    failed: list[str] = []
    for site, data in sorted((value.get("sites") or {}).items()):
        if "error" in data:
            failed.append(site)
            continue
        for item in data.get("updates", []):
            key = (item["kind"], item["name"], item["version"], item["update_version"])
            entry = grouped.setdefault(key, {**item, "sites": []})
            entry["sites"].append(site)
    # Ativos primeiro (o que está a servir páginas), depois os inativos.
    items = sorted(grouped.values(), key=lambda i: (i["status"] == "inactive", i["kind"],
                                                    i["name"]))
    return {"items": items, "failed": failed,
            "stale": moment - float(value["checked_at"]) > STALE_S,
            "checked_at": float(value["checked_at"])}


def main(stdin: str) -> int:
    try:
        stored = record(json.loads(stdin))
    except (json.JSONDecodeError, ValueError) as exc:
        print(f"relatório inválido: {exc}", file=sys.stderr)
        return 2
    pending = sum(len(s.get("updates", [])) for s in stored["sites"].values())
    print(f"wp_updates: {len(stored['sites'])} blog(s), {pending} atualização(ões) à espera")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.stdin.read()))
