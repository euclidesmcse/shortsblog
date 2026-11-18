-- E18.1 (D-104): usuários próprios do portal e trilha de auditoria.
--
-- Senha só como hash argon2id. `session_version` entra no cookie da sessão: trocar a senha,
-- redefini-la ou desativar o usuário incrementa a versão e derruba as sessões abertas.
-- `must_change_password`: o admin criado a partir do PANEL_PASSWORD e quem recebe senha
-- provisória só navegam depois de escolher a própria senha.
CREATE TABLE IF NOT EXISTS users (
    id                   BIGSERIAL PRIMARY KEY,
    email                TEXT        NOT NULL UNIQUE CHECK (email = lower(email) AND email LIKE '%_@_%'),
    name                 TEXT        NOT NULL DEFAULT '',
    role                 TEXT        NOT NULL CHECK (role IN ('admin', 'operador')),
    password_hash        TEXT        NOT NULL,
    active               BOOLEAN     NOT NULL DEFAULT true,
    must_change_password BOOLEAN     NOT NULL DEFAULT false,
    session_version      INTEGER     NOT NULL DEFAULT 1,
    failed_logins        INTEGER     NOT NULL DEFAULT 0,
    locked_until         TIMESTAMPTZ,
    last_login_at        TIMESTAMPTZ,
    created_by           TEXT        NOT NULL DEFAULT '',
    created_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Quem fez o quê e quando. `details` nunca leva senhas nem valores de segredos (R68).
CREATE TABLE IF NOT EXISTS audit_log (
    id       BIGSERIAL PRIMARY KEY,
    at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor    TEXT        NOT NULL,
    action   TEXT        NOT NULL,
    target   TEXT        NOT NULL DEFAULT '',
    details  JSONB       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_audit_log_at ON audit_log (at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_target ON audit_log (target, at DESC);
