-- E17.1: newsletter dos posts publicados (D-062). Uma lista para os 6 blogs.
--
-- O e-mail fica em claro só enquanto a pessoa assina (pending/confirmed). Ao sair (unsubscribed) ou
-- quando o Brevo devolve bounce ou reclamação (suppressed), o e-mail é apagado e fica só o hash
-- (HMAC com SB_SECRET_KEY): serve para não voltar a escrever a quem saiu e para reinscrever.
-- Os logs nunca levam o e-mail.
CREATE TABLE IF NOT EXISTS subscribers (
    id               BIGSERIAL PRIMARY KEY,
    email            TEXT,
    email_hash       TEXT        NOT NULL UNIQUE,
    language         TEXT        NOT NULL CHECK (language IN ('pt-BR', 'en', 'es', 'zh-CN')),
    status           TEXT        NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'confirmed', 'unsubscribed', 'suppressed')),
    origin_site      TEXT        NOT NULL REFERENCES sites(site_key),
    -- Cada novo e-mail de confirmação muda a versão: os links anteriores deixam de valer.
    token_version    INTEGER     NOT NULL DEFAULT 1,
    ip_hash          TEXT,
    consent_version  TEXT        NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    confirmed_at     TIMESTAMPTZ,
    unsubscribed_at  TIMESTAMPTZ,
    suppressed_at    TIMESTAMPTZ,
    suppressed_reason TEXT,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((status IN ('pending', 'confirmed')) = (email IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS idx_subscribers_status ON subscribers (status);

-- Um envio por assinante e edição (dia em America/Sao_Paulo): reexecutar não duplica. `unknown` é o
-- SEND_UNKNOWN (não se sabe se saiu): nunca se repete sozinho; só `failed` volta a tentar.
CREATE TABLE IF NOT EXISTS newsletter_sends (
    edition_date  DATE        NOT NULL,
    subscriber_id BIGINT      NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    status        TEXT        NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'sent', 'failed', 'unknown')),
    posts         INTEGER     NOT NULL DEFAULT 0,
    message_ref   TEXT,
    error         TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at_utc   TIMESTAMPTZ,
    PRIMARY KEY (edition_date, subscriber_id)
);
CREATE INDEX IF NOT EXISTS idx_newsletter_sends_created ON newsletter_sends (created_at);
