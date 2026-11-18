"""Migrações + papel da aplicação — corre com o utilizador ADMIN (E2.8).

    python -m app.core.migrate

É o comando do serviço one-shot `sb-engine-migrate`. A API, o worker e o
scheduler ligam-se como `sb_app`, que só tem DML no esquema da app: não cria,
não altera, não apaga nem esvazia tabelas (spec §11.1).

Variáveis: DATABASE_URL (admin), SB_DB_APP_USER (padrão sb_app), SB_DB_APP_PASSWORD.
Sem SB_DB_APP_PASSWORD só aplica as migrações.
No fim semeia a configuração editorial de content-config/ (só adições, E18.3a).
"""
from __future__ import annotations

import os
import sys

from psycopg import sql
from sqlalchemy import text

from app.core.db import engine, run_migrations
from app.core.logging import get_logger, setup_logging

log = get_logger(__name__)

# DML apenas. Sem TRUNCATE, REFERENCES, TRIGGER — e sem ser dono de nada.
_TABLE_PRIVILEGES = "SELECT, INSERT, UPDATE, DELETE"
# Tabelas em que a aplicação tem menos do que DML completo.
_RESTRICTED = {
    "sites": "INSERT, UPDATE, DELETE",
    "publication_events": "UPDATE, DELETE",
    "approvals": "UPDATE, DELETE",
    # E18 (revisão intermediária #4): a trilha de auditoria só se acrescenta — o desfazer e o
    # histórico dependem dela.
    "audit_log": "UPDATE, DELETE",
}
# E18 (revisão intermediária #1, D-106): o portal edita estas colunas de `sites` (UPDATE por
# coluna). As técnicas — `base_url` e `wordpress_api_url` (o destino de publicação), fuso, idioma
# principal, versões — continuam fora do alcance da app: só o admin (migrate/reimport) as escreve.
SITES_APP_COLUMNS = ("name", "enabled", "daily_target", "slot_offset_minutes", "languages",
                     "config", "updated_at")


def ensure_app_role(role: str, password: str) -> None:
    """Cria (ou atualiza) o papel da aplicação com privilégios mínimos. Idempotente."""
    if not role.isidentifier():
        raise ValueError(f"Nome de papel inválido: {role!r}")
    ident = sql.Identifier(role)
    with engine.begin() as conn:
        database = conn.execute(text("SELECT current_database()")).scalar_one()
        exists = conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = :r"),
                              {"r": role}).first()
        cursor = conn.connection.cursor()
        verb = "ALTER" if exists else "CREATE"
        cursor.execute(sql.SQL(verb + " ROLE {} WITH LOGIN NOSUPERUSER NOCREATEDB "
                               "NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD {}")
                       .format(ident, sql.Literal(password)))
        statements = [
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
            "GRANT CONNECT ON DATABASE {db} TO {role}",
            "GRANT USAGE ON SCHEMA public TO {role}",
            "GRANT " + _TABLE_PRIVILEGES + " ON ALL TABLES IN SCHEMA public TO {role}",
            "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}",
            # Tabelas criadas por migrações futuras (pelo admin) herdam o mesmo.
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT " + _TABLE_PRIVILEGES
            + " ON TABLES TO {role}",
            "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES "
            "TO {role}",
            # O registo de migrações é só do admin.
            "REVOKE INSERT, UPDATE, DELETE ON TABLE schema_migrations FROM {role}",
        ]
        for statement in statements:
            cursor.execute(sql.SQL(statement).format(db=sql.Identifier(database), role=ident))
        # Revisão E4 #6. `sites` define o destino de publicação e só é escrita pela
        # sincronização (admin); o histórico e as aprovações só se acrescentam.
        for table, privileges in _RESTRICTED.items():
            if conn.execute(text("SELECT to_regclass(:t)"), {"t": table}).scalar() is not None:
                cursor.execute(sql.SQL("REVOKE {} ON TABLE {} FROM {}").format(
                    sql.SQL(privileges), sql.Identifier(table), ident))
        # O REVOKE da tabela leva também os privilégios de coluna: concede-os de novo a cada vez.
        if conn.execute(text("SELECT to_regclass('sites')")).scalar() is not None:
            cursor.execute(sql.SQL("GRANT UPDATE ({}) ON TABLE sites TO {}").format(
                sql.SQL(", ").join(sql.Identifier(c) for c in SITES_APP_COLUMNS), ident))
    log.info("Papel %s garantido com DML apenas", role)


def sync_sites() -> None:
    """E18.3a (D-104): semeia a configuração editorial a partir de `content-config/`.

    Até a E18.3a isto SOBRESCREVIA sites (com a política), feeds e newsletters de pauta a cada
    deploy. Agora o banco é a fonte da verdade: entram só as adições (blog, feed ou lista que
    ainda não existe); alterações e desligamentos do YAML ficam pendentes e só chegam ao banco
    com a reimportação explícita (`make reimport-apply` ou a tela Configuração), com diff e
    auditoria. O YAML continua validado aqui: um arquivo inválido trava o deploy como antes."""
    from app.core.db import session_scope
    from app.services import discovery, editorial, pauta, sites, terms

    try:
        with session_scope() as db:
            added, pending = editorial.seed(db)
    except (sites.SiteConfigError, discovery.SourceConfigError,
            pauta.NewsletterConfigError, terms.TermsConfigError) as exc:
        # Configuração inválida trava o arranque: as apps dependem deste serviço.
        log.error("Configuração editorial inválida: %s", exc)
        raise SystemExit(1) from None
    log.info("Configuração semeada: %d adição(ões)%s", len(added),
             f" — {editorial.summary(added)}" if added else "")
    if pending:
        log.warning("Configuração: %d mudança(s) do YAML esperam reimportação "
                    "(make reimport / tela Configuração): %s",
                    len(pending), editorial.summary(pending))


def main() -> int:
    setup_logging()
    applied = run_migrations()
    log.info("Migrações aplicadas: %s", ", ".join(applied) or "nenhuma (base em dia)")
    password = os.getenv("SB_DB_APP_PASSWORD", "")
    if password:
        ensure_app_role(os.getenv("SB_DB_APP_USER", "sb_app"), password)
    else:
        log.warning("SB_DB_APP_PASSWORD vazio — papel da aplicação não configurado")
    sync_sites()
    return 0


if __name__ == "__main__":
    sys.exit(main())
