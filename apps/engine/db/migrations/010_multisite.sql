-- =========================================================================
--  ShortsBlog · Modelo multi-site (E4.1; base: spec §8.1 e §8.2)
--
--  Seis blogs, cinco posts por dia cada, quatro idiomas por post. A unidade de
--  trabalho é o `publication_job`: um slot de um site num dia. O texto original
--  e as traduções vivem em `job_translations` (uma linha por idioma).
--
--  Instantes em UTC (TIMESTAMPTZ); a data editorial (`local_date`) é a do fuso
--  do site. Nenhuma tabela guarda credenciais do WordPress (spec §8.1).
--
--  As tabelas do pipeline herdado (keywords, content_briefs, articles…) ficam
--  intactas até a E6 as substituir.
-- =========================================================================

-- Sites: espelho de content-config/sites/*.yaml (a fonte de verdade é o ficheiro;
-- `python -m app.core.migrate` sincroniza).
CREATE TABLE IF NOT EXISTS sites (
    site_key                 TEXT PRIMARY KEY CHECK (site_key ~ '^[a-z][a-z0-9-]{1,30}$'),
    name                     TEXT        NOT NULL,
    base_url                 TEXT        NOT NULL CHECK (base_url LIKE 'https://%'),
    wordpress_api_url        TEXT        NOT NULL,
    hosting_group            TEXT        NOT NULL DEFAULT 'default',
    timezone                 TEXT        NOT NULL DEFAULT 'America/Sao_Paulo',
    language                 TEXT        NOT NULL DEFAULT 'pt-BR',
    languages                TEXT[]      NOT NULL DEFAULT '{pt-BR,en,es,zh-CN}',
    enabled                  BOOLEAN     NOT NULL DEFAULT false,
    daily_target             INTEGER     NOT NULL DEFAULT 5 CHECK (daily_target BETWEEN 0 AND 5),
    slot_offset_minutes      INTEGER     NOT NULL DEFAULT 0
                             CHECK (slot_offset_minutes BETWEEN 0 AND 59),
    seo_plugin               TEXT        NOT NULL DEFAULT '',
    taxonomy_version         TEXT        NOT NULL DEFAULT '',
    editorial_policy_version TEXT        NOT NULL DEFAULT '',
    config                   JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Fontes RSS por site (validadas na E5).
CREATE TABLE IF NOT EXISTS sources (
    id              BIGSERIAL PRIMARY KEY,
    site_key        TEXT        NOT NULL REFERENCES sites(site_key) ON DELETE CASCADE,
    name            TEXT        NOT NULL,
    feed_url        TEXT        NOT NULL CHECK (feed_url LIKE 'https://%'),
    domain          TEXT        NOT NULL,
    source_type     TEXT        NOT NULL DEFAULT 'rss',
    enabled         BOOLEAN     NOT NULL DEFAULT true,
    usage_notes     TEXT        NOT NULL DEFAULT '',
    last_checked_at TIMESTAMPTZ,
    last_status     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (site_key, feed_url)
);

-- Ideias: descobertas nos feeds ou introduzidas à mão (as manuais têm prioridade).
CREATE TABLE IF NOT EXISTS ideas (
    id            BIGSERIAL PRIMARY KEY,
    site_key      TEXT        NOT NULL REFERENCES sites(site_key) ON DELETE CASCADE,
    origin        TEXT        NOT NULL CHECK (origin IN ('rss', 'manual')),
    topic         TEXT        NOT NULL,
    angle         TEXT        NOT NULL DEFAULT '',
    source_urls   TEXT[]      NOT NULL DEFAULT '{}',
    canonical_url TEXT,
    priority      INTEGER     NOT NULL DEFAULT 0,
    status        TEXT        NOT NULL DEFAULT 'new'
                  CHECK (status IN ('new', 'taken', 'used', 'discarded', 'expired')),
    expires_at    TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ideas_pick
    ON ideas (site_key, status, priority DESC, created_at);
-- A mesma notícia não entra duas vezes no mesmo blog.
CREATE UNIQUE INDEX IF NOT EXISTS uq_ideas_site_url
    ON ideas (site_key, canonical_url) WHERE canonical_url IS NOT NULL;

-- Um slot de um site num dia. Estados: E4.4 (D-006: aprovação humana sempre).
CREATE TABLE IF NOT EXISTS publication_jobs (
    id                 BIGSERIAL PRIMARY KEY,
    site_key           TEXT        NOT NULL REFERENCES sites(site_key),
    local_date         DATE        NOT NULL,
    slot_number        INTEGER     NOT NULL CHECK (slot_number BETWEEN 1 AND 5),
    due_at_utc         TIMESTAMPTZ NOT NULL,
    -- A produção pode começar a partir daqui (antecedência da D-033).
    produce_after_utc  TIMESTAMPTZ NOT NULL,
    status             TEXT        NOT NULL DEFAULT 'PLANNED' CHECK (status IN (
                           'PLANNED', 'QUEUED', 'CLAIMED', 'RESEARCHING', 'WRITING',
                           'VALIDATING', 'TRANSLATING', 'AWAITING_APPROVAL', 'APPROVED',
                           'PUBLISHING', 'VERIFY_REQUIRED', 'RETRY_WAIT',
                           'PUBLISHED_CONFIRMED', 'BLOCKED', 'FAILED', 'REJECTED', 'EXPIRED')),
    idea_id            BIGINT      REFERENCES ideas(id) ON DELETE SET NULL,
    attempt_count      INTEGER     NOT NULL DEFAULT 0,
    lease_until_utc    TIMESTAMPTZ,
    lease_owner        TEXT,
    retry_after_utc    TIMESTAMPTZ,
    blocked_reason     TEXT,
    -- Família do modelo que escreveu (para o revisor ser de outra — E3.5).
    writer_family      TEXT,
    wordpress_post_id  BIGINT,
    wordpress_url      TEXT,
    published_at_utc   TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (site_key, local_date, slot_number)
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON publication_jobs (status, due_at_utc);
CREATE INDEX IF NOT EXISTS idx_jobs_lease  ON publication_jobs (lease_until_utc)
 WHERE lease_until_utc IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_wp_post
    ON publication_jobs (site_key, wordpress_post_id) WHERE wordpress_post_id IS NOT NULL;

-- Conteúdo por idioma: o original (pt-BR) e as traduções.
CREATE TABLE IF NOT EXISTS job_translations (
    id                BIGSERIAL PRIMARY KEY,
    job_id            BIGINT      NOT NULL REFERENCES publication_jobs(id) ON DELETE CASCADE,
    language          TEXT        NOT NULL CHECK (language IN ('pt-BR', 'en', 'es', 'zh-CN')),
    is_original       BOOLEAN     NOT NULL DEFAULT false,
    title             TEXT        NOT NULL DEFAULT '',
    slug              TEXT        NOT NULL DEFAULT '',
    excerpt           TEXT        NOT NULL DEFAULT '',
    html              TEXT        NOT NULL DEFAULT '',
    status            TEXT        NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'ready', 'failed', 'published')),
    wordpress_post_id BIGINT,
    wordpress_url     TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id, language)
);

-- Fontes que sustentam cada artigo (spec §10.1: secção "Fontes e créditos").
CREATE TABLE IF NOT EXISTS article_evidence (
    id                  BIGSERIAL PRIMARY KEY,
    job_id              BIGINT      NOT NULL REFERENCES publication_jobs(id) ON DELETE CASCADE,
    source_url          TEXT        NOT NULL,
    source_name         TEXT        NOT NULL DEFAULT '',
    source_author       TEXT        NOT NULL DEFAULT '',
    source_published_at TIMESTAMPTZ,
    accessed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    claims_supported    TEXT        NOT NULL DEFAULT '',
    usage_rights_note   TEXT        NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_evidence_job ON article_evidence (job_id);

-- Imagens com prova de licença (D-012; preenchida na E7).
CREATE TABLE IF NOT EXISTS image_assets (
    id                 BIGSERIAL PRIMARY KEY,
    job_id             BIGINT      NOT NULL REFERENCES publication_jobs(id) ON DELETE CASCADE,
    role               TEXT        NOT NULL DEFAULT 'featured',
    provider           TEXT        NOT NULL,
    source_url         TEXT,
    license            TEXT        NOT NULL,
    license_proof_url  TEXT,
    author             TEXT        NOT NULL DEFAULT '',
    local_path         TEXT,
    alt_text           TEXT        NOT NULL DEFAULT '',
    wordpress_media_id BIGINT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_images_job ON image_assets (job_id);

-- Decisões humanas: um clique vale para os 4 idiomas (D-006).
CREATE TABLE IF NOT EXISTS approvals (
    id         BIGSERIAL PRIMARY KEY,
    job_id     BIGINT      NOT NULL REFERENCES publication_jobs(id) ON DELETE CASCADE,
    decision   TEXT        NOT NULL CHECK (decision IN ('approved', 'rejected')),
    decided_by TEXT        NOT NULL,
    channel    TEXT        NOT NULL CHECK (channel IN ('panel', 'email')),
    note       TEXT        NOT NULL DEFAULT '',
    decided_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_approvals_job ON approvals (job_id);

-- Registo de tudo o que acontece a um job (só se acrescenta).
CREATE TABLE IF NOT EXISTS publication_events (
    id            BIGSERIAL PRIMARY KEY,
    job_id        BIGINT      NOT NULL REFERENCES publication_jobs(id) ON DELETE CASCADE,
    event_type    TEXT        NOT NULL,
    from_status   TEXT,
    to_status     TEXT,
    timestamp_utc TIMESTAMPTZ NOT NULL DEFAULT now(),
    actor         TEXT        NOT NULL DEFAULT 'system',
    execution_id  TEXT,
    details       JSONB       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_events_job ON publication_events (job_id, id);

-- Resumo diário enviado ao operador (E10).
CREATE TABLE IF NOT EXISTS daily_digest (
    local_date        DATE        NOT NULL,
    recipient         TEXT        NOT NULL,
    status            TEXT        NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'sent', 'failed')),
    message_reference TEXT,
    sent_at_utc       TIMESTAMPTZ,
    error             TEXT,
    PRIMARY KEY (local_date, recipient)
);

-- O uso de IA passa a poder ser atribuído a um job.
ALTER TABLE provider_usage
    ADD COLUMN IF NOT EXISTS job_id BIGINT REFERENCES publication_jobs(id) ON DELETE SET NULL;
