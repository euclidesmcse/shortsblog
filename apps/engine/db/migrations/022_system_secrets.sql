-- E18.3b (D-104, D-109): cofre do sistema — segredos e integrações editados pelo portal.
-- O valor vai SEMPRE cifrado (Fernet derivado de SB_SECRET_KEY, propósito próprio); `last4` só
-- para mostrar mascarado. O banco tem precedência sobre o ambiente: sem linha aqui, vale o env.
-- `previous_*` guarda o valor anterior de um token durante a carência da troca (entrega 2).
CREATE TABLE IF NOT EXISTS system_secrets (
    key             TEXT PRIMARY KEY,
    value_enc       TEXT NOT NULL,
    last4           TEXT NOT NULL DEFAULT '',
    previous_enc    TEXT,
    previous_until  TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_by      TEXT NOT NULL DEFAULT ''
);
