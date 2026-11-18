-- =========================================================================
--  ShortsBlog · Provedores de IA e cadeia de prioridade por papel (E3.5)
--
--  A configuração vive aqui, no motor; o AI Gateway não guarda nada (D-030).
--  `subscription` = CLI de assinatura no gateway (claude, codex, agy).
--  `api`          = serviço compatível com a OpenAI; a chave fica cifrada em
--                   `api_key_enc` (Fernet, derivada de SB_SECRET_KEY) e nunca
--                   é devolvida por nenhuma rota.
--  `family` é a família do MODELO (anthropic, openai, google…), não a da CLI:
--  é o que sustenta a regra "redator e revisor de famílias diferentes".
-- =========================================================================

CREATE TABLE IF NOT EXISTS ai_providers (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT        NOT NULL UNIQUE,
    kind        TEXT        NOT NULL CHECK (kind IN ('subscription', 'api')),
    family      TEXT        NOT NULL CHECK (family <> ''),
    model       TEXT        NOT NULL DEFAULT '',
    base_url    TEXT,
    api_key_enc TEXT,
    enabled     BOOLEAN     NOT NULL DEFAULT true,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK (kind = 'subscription' OR (base_url IS NOT NULL AND model <> ''))
);

CREATE TABLE IF NOT EXISTS ai_role_chain (
    role        TEXT    NOT NULL
                CHECK (role IN ('writer', 'reviewer', 'translator', 'researcher')),
    position    INTEGER NOT NULL CHECK (position >= 1),
    provider_id BIGINT  NOT NULL REFERENCES ai_providers(id) ON DELETE CASCADE,
    PRIMARY KEY (role, position),
    UNIQUE (role, provider_id)
);
