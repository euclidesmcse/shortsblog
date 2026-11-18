"""E18.3b — linha de comando do cofre (corre no `sb-engine-api`, que tem o ambiente e o banco).

    python -m app.core.vault_cli status
    python -m app.core.vault_cli import-env <item> [--apply] [--force]
    SB_SECRET_KEY_NEW=… python -m app.core.vault_cli rekey [--apply]

`import-env` copia UM item do ambiente para o cofre (migração item a item, D-109): sem `--apply` só
mostra o que faria e o resultado do teste de conexão; com `--apply` grava se o teste passar
(`--force` grava mesmo assim). `rekey` re-cifra o cofre e as API keys de IA para a chave nova; sem
`--apply` só confere que tudo abre. Nenhum comando imprime o valor de um segredo.
"""
from __future__ import annotations

import os
import sys

from sqlalchemy import text

from app.core.db import session_scope
from app.core.logging import setup_logging
from app.services import integrations, vault

REKEY_EFFECTS = (
    "Depois de trocar o SB_SECRET_KEY no Infisical e recriar a stack:",
    "  - todas as sessões do painel caem (todos entram de novo);",
    "  - links de aprovação já enviados por e-mail deixam de valer (aprovar pelo painel);",
    "  - links de confirmação/descadastro da newsletter já enviados deixam de valer;",
    "  - entre o --apply e a recriação, o cofre não abre nos containers velhos (vale o ambiente)"
    " e as API keys de IA falham: faça numa janela sem produção.",
)


def _site_keys() -> list[str]:
    with session_scope() as db:
        return [r[0] for r in db.execute(text("SELECT site_key FROM sites ORDER BY site_key"))]


def _all_items() -> list[vault.Item]:
    items = list(vault.EMAIL_ITEMS) + list(vault.TOKEN_ITEMS) + list(vault.LIMIT_ITEMS)
    for key in _site_keys():
        items.extend(vault.wp_items(key))
    return items


def status() -> int:
    print(f"{'item':32} {'vale':10} {'no env':7} valor")
    for it in _all_items():
        d = vault.describe(it)
        flag = " (banco ilegível)" if d["unreadable"] else ""
        if d.get("grace_until"):
            flag += f" (anterior vale até {d['grace_until']:%d/%m %H:%M} UTC)"
        print(f"{it.key:32} {d['source'] or 'FALTANDO':10} {'sim' if d['in_env'] else 'não':7} "
              f"{d['shown']}{flag}")
    return 0


def _check(key: str, value: str) -> integrations.Check:
    it = vault.item(key)
    if it.group == "token":
        return integrations.Check(True, "token copiado igual ao do ambiente: nada muda para "
                                        "quem o usa")
    if it.group == "limit":
        try:
            vault.validate(it, value)
        except vault.VaultError as exc:
            return integrations.Check(False, str(exc))
        return integrations.Check(True, "parâmetro copiado igual ao do ambiente: nada muda")
    if it.group == "email":
        values = {i.key: vault.get(i.key) for i in vault.EMAIL_ITEMS}
        values[key] = value
        return integrations.check_email(values)
    site_key = key.split(".")[1]
    with session_scope() as db:
        site = db.execute(text("SELECT * FROM sites WHERE site_key = :k"),
                          {"k": site_key}).mappings().first()
    if not site:
        return integrations.Check(False, f"Blog desconhecido: {site_key}")
    user_key, password_key = (i.key for i in vault.wp_items(site_key))
    user = value if key == user_key else vault.get(user_key)
    password = value if key == password_key else vault.get(password_key)
    return integrations.check_wordpress(dict(site), user, password)


def import_env(key: str, apply: bool, force: bool) -> int:
    try:
        it = vault.item(key)
    except vault.VaultError as exc:
        print(exc)
        return 2
    value = vault.env_value(key)
    if not value:
        print(f"{key}: não há valor no ambiente — nada a importar.")
        return 2
    current, source = vault.effective(key)
    if source == "banco" and current == value:
        print(f"{key}: o cofre já tem o mesmo valor do ambiente.")
        return 0
    shown = vault.mask(value[-4:]) if it.secret else value
    check = _check(key, value)
    print(f"{key}: ambiente → cofre ({shown}); hoje vale: {source or 'nada'}")
    print(f"Teste de conexão: {'OK' if check.ok else 'FALHOU'} — {check.message}")
    if not apply:
        print("Só leitura. Para gravar: --apply")
        return 0
    if not check.ok and not force:
        print("Nada gravado: o teste falhou (--force grava mesmo assim).")
        return 2
    with session_scope() as db:
        vault.set_value(db, key, value, "cli:import-env")
    print(f"{key}: gravado no cofre. O ambiente fica como reserva até ser retirado.")
    return 0


def rekey(apply: bool) -> int:
    from app.core.config import settings

    new_key = os.environ.get("SB_SECRET_KEY_NEW", "")
    if len(new_key) < 32:
        print("Defina SB_SECRET_KEY_NEW (32+ caracteres) no ambiente do comando.")
        return 2
    if new_key == settings.sb_secret_key:
        print("A chave nova é igual à atual.")
        return 2
    with session_scope() as db:
        try:
            result = vault.rekey(db, settings.sb_secret_key, new_key)
        except vault.VaultError as exc:
            print(exc)
            return 2
        except Exception as exc:  # noqa: BLE001 — API key de IA ilegível, por exemplo
            print(f"Nada alterado: {type(exc).__name__}: {exc}")
            return 2
        print(f"Re-cifra: {result['vault_items']} item(ns) do cofre, "
              f"{result['ai_keys']} API key(s) de IA.")
        if not apply:
            db.rollback()
            print("Só conferência (tudo abre com a chave atual). Para gravar: --apply")
            return 0
    print("Gravado com a chave NOVA.")
    print("\n".join(REKEY_EFFECTS))
    return 0


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    args = list(sys.argv[1:] if argv is None else argv)
    flags = {a for a in args if a.startswith("--")}
    words = [a for a in args if not a.startswith("--")]
    if words[:1] == ["status"]:
        return status()
    if words[:1] == ["import-env"] and len(words) == 2:
        return import_env(words[1], "--apply" in flags, "--force" in flags)
    if words[:1] == ["rekey"]:
        return rekey("--apply" in flags)
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
