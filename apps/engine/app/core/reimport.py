"""E18.3a: reimportar a configuração editorial do YAML para o banco, com diff e auditoria.

    python -m app.core.reimport            # só mostra o que mudaria
    python -m app.core.reimport --apply --fingerprint=<impressão do diff> [--sobrescrever-portal]

Em produção: `make reimport` / `make reimport-apply` (pelo one-shot `sb-engine-migrate`, que
monta o `content-config/`). É o caminho das sessões que editam o YAML no git: depois do commit,
reimportar — o deploy já não sobrescreve o banco (D-104).
"""
from __future__ import annotations

import getpass
import sys

from app.core.db import session_scope
from app.core.logging import setup_logging
from app.services import discovery, editorial, pauta, sites, terms


def _print(changes: list[editorial.Change]) -> None:
    if not changes:
        print("Nada a reimportar: o banco já está igual ao YAML.")
        return
    print(f"{len(changes)} mudança(s) do YAML para o banco:\n")
    for change in changes:
        flag = ("  ⚠ editado no portal depois da última importação"
                if change.edited_in_portal else "")
        print(f"- [{change.kind}] {change.target}{flag}")
        if change.note:
            print(f"    {change.note}")
        for name in change.fields:
            if name == "*":
                continue
            old, new = change.short(name)
            print(f"    {name}: {old}  →  {new}")
        for name, diff in change.text_diffs():
            print(f"    --- diff de {name} ---")
            for line in diff.splitlines()[2:60]:
                print(f"    {line}")


def _arg(argv: list[str], name: str) -> str:
    for item in argv:
        if item.startswith(f"--{name}="):
            return item.split("=", 1)[1].strip()
    return ""


def main(argv: list[str]) -> int:
    """Sem `--apply` só mostra o diff e a impressão dele. Com `--apply`, exige a impressão que o
    diff mostrou (`--fingerprint=…`): aplica exatamente o que foi revisto. Mudanças editadas no
    portal depois da última importação só são sobrescritas com `--sobrescrever-portal`
    (revisão intermediária da E18, #2)."""
    setup_logging()
    apply = "--apply" in argv
    expected = _arg(argv, "fingerprint")
    override = "--sobrescrever-portal" in argv
    try:
        with session_scope() as db:
            changes = editorial.plan(db)
            _print(changes)
            if not changes:
                return 0
            current = editorial.fingerprint(changes)
            edited = [c.target for c in changes if c.edited_in_portal]
            if not apply:
                print(f"\nImpressão deste diff: {current}")
                print(f"Nada foi gravado. Para aplicar: make reimport-apply FP={current}"
                      + (" SOBRESCREVER=1" if edited else ""))
                if edited:
                    print("⚠ sobrescreve edições do portal: " + ", ".join(edited))
                return 0
            if expected != current:
                print(f"\nRecusado: a impressão informada ({expected or 'nenhuma'}) não é a deste "
                      f"diff ({current}). Rode `make reimport`, revise e use FP={current}.",
                      file=sys.stderr)
                return 2
            if edited and not override:
                print("\nRecusado: estas mudanças sobrescrevem edições feitas no portal: "
                      + ", ".join(edited) + ". Exporte-as para o git primeiro, ou confirme com "
                      "SOBRESCREVER=1.", file=sys.stderr)
                return 2
            actor = f"cli:{getpass.getuser()}"
            count = editorial.apply(db, changes, actor)
            print(f"\nAplicadas {count} mudança(s) (auditoria: {editorial.IMPORT_ACTION}, "
                  f"por {actor}).")
    except (sites.SiteConfigError, discovery.SourceConfigError,
            pauta.NewsletterConfigError, terms.TermsConfigError) as exc:
        print(f"YAML inválido: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
