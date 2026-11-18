"""Camada de banco: engine, sessão e aplicação das migrações SQL.

Usamos SQL puro versionado em `db/migrations/*.sql` em vez de Alembic:
o schema é o contrato do sistema e fica legível para quem for auditar,
inclusive fora do Python.
"""
from __future__ import annotations

import hashlib
import pathlib
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    pool_recycle=1800,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Sessão transacional: commit no sucesso, rollback em qualquer exceção."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_db() -> Iterator[Session]:
    """Dependência do FastAPI."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


_MIGRATION_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT PRIMARY KEY,
    checksum   TEXT NOT NULL,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def run_migrations() -> list[str]:
    """Aplica todos os `.sql` de migrations ainda não aplicados, em ordem.

    Retorna a lista de arquivos aplicados nesta execução.
    """
    directory = pathlib.Path(settings.migrations_dir)
    if not directory.is_dir():
        log.warning("Diretório de migrações não encontrado: %s", directory)
        return []

    applied: list[str] = []
    with engine.begin() as conn:
        conn.execute(text(_MIGRATION_TABLE))
        done = {
            row[0]: row[1]
            for row in conn.execute(text("SELECT filename, checksum FROM schema_migrations"))
        }

    for path in sorted(directory.glob("*.sql")):
        sql = path.read_text(encoding="utf-8")
        checksum = hashlib.sha256(sql.encode()).hexdigest()[:16]
        if path.name in done:
            if done[path.name] != checksum:
                # Migração já aplicada foi editada: avisa, mas não reaplica
                # automaticamente para não corromper dados em produção.
                log.warning(
                    "Migração %s mudou depois de aplicada (checksum %s -> %s). "
                    "Crie um novo arquivo em vez de editar o antigo.",
                    path.name, done[path.name], checksum,
                )
            continue
        log.info("Aplicando migração %s", path.name)
        with engine.begin() as conn:
            # SQL cru num cursor DBAPI, SEM parâmetros. Com `text()`, o
            # SQLAlchemy interpretaria `:` dentro de literais JSON
            # (ex.: '{"volume":1.0}') como bind ("A value is required for bind
            # parameter '1'"). E `exec_driver_sql` passa parâmetros vazios, o que
            # faz o psycopg ler o `%1$s` do `format()` em PL/pgSQL como
            # placeholder ("only '%s', '%b', '%t' are allowed") — numa base
            # vazia o 001_init nunca aplicava (apanhado pela suíte na E2.3).
            conn.connection.cursor().execute(sql)
            conn.execute(
                text(
                    "INSERT INTO schema_migrations (filename, checksum) "
                    "VALUES (:f, :c) ON CONFLICT (filename) DO NOTHING"
                ),
                {"f": path.name, "c": checksum},
            )
        applied.append(path.name)

    return applied


def get_setting(db: Session, key: str, default=None):
    """Lê um valor da tabela `settings` (configuração quente)."""
    row = db.execute(text("SELECT value FROM settings WHERE key = :k"), {"k": key}).first()
    return row[0] if row else default
