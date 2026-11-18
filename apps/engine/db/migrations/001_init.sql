-- =========================================================================
--  ShortsBlog (fork do Growth Engine) · Schema inicial
--  Tabelas de afiliados, social, leads, e-mail, SEO/GSC, backlinks e BI
--  saíram na E2.2 (fora do escopo, D-016). O esquema multi-site vem na E4.
--  Idempotente: pode ser reaplicado com segurança.
-- =========================================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- -------------------------------------------------------------------------
-- Configuração em runtime (permite ajustar o motor sem redeploy)
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       JSONB       NOT NULL,
    description TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- -------------------------------------------------------------------------
-- Fila de temas (herdada de `keywords`; a origem passa a RSS + ideias na E4/E5)
-- -------------------------------------------------------------------------

-- Keyword consolidada e pontuada.
CREATE TABLE IF NOT EXISTS keywords (
    id              BIGSERIAL PRIMARY KEY,
    term            TEXT        NOT NULL,
    normalized      TEXT        NOT NULL,
    locale          TEXT        NOT NULL DEFAULT 'pt-BR',
    geo             TEXT        NOT NULL DEFAULT 'BR',
    intent          TEXT        NOT NULL DEFAULT 'informational',
                    -- informational | commercial | transactional | navigational
    volume          INTEGER     NOT NULL DEFAULT 0,
    cpc_usd         NUMERIC(10,2) NOT NULL DEFAULT 0,
    difficulty      NUMERIC(6,2)  NOT NULL DEFAULT 50,   -- 1..100
    commercial_score NUMERIC(6,3) NOT NULL DEFAULT 1.0,  -- multiplicador de intenção
    trend_score     NUMERIC(6,3)  NOT NULL DEFAULT 0,
    score           NUMERIC(14,4) NOT NULL DEFAULT 0,    -- (vol * cpc * intenção) / dificuldade
    sources         TEXT[]      NOT NULL DEFAULT '{}',
    status          TEXT        NOT NULL DEFAULT 'discovered',
                    -- discovered | approved | briefed | in_production | published | rejected
    rejection_reason TEXT,
    cluster_id      BIGINT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_keyword UNIQUE (normalized, locale, geo)
);
CREATE INDEX IF NOT EXISTS idx_keywords_score  ON keywords (score DESC);
CREATE INDEX IF NOT EXISTS idx_keywords_status ON keywords (status, score DESC);
CREATE INDEX IF NOT EXISTS idx_keywords_trgm   ON keywords USING gin (normalized gin_trgm_ops);

-- -------------------------------------------------------------------------
-- FASE 2 — Planejamento editorial
-- -------------------------------------------------------------------------

-- Briefing editorial: título, outline H1..H4, FAQ, metadados.
CREATE TABLE IF NOT EXISTS content_briefs (
    id             BIGSERIAL PRIMARY KEY,
    keyword_id     BIGINT      NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
    title          TEXT        NOT NULL,
    h1             TEXT        NOT NULL,
    slug           TEXT        NOT NULL,
    meta_description TEXT      NOT NULL DEFAULT '',
    outline        JSONB       NOT NULL DEFAULT '[]'::jsonb,  -- [{level,heading,points[]}]
    faq            JSONB       NOT NULL DEFAULT '[]'::jsonb,  -- [{question,answer_hint}]
    target_words   INTEGER     NOT NULL DEFAULT 3500,
    secondary_keywords TEXT[]  NOT NULL DEFAULT '{}',
    angle          TEXT,
    status         TEXT        NOT NULL DEFAULT 'draft',  -- draft | ready | consumed
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_briefs_keyword ON content_briefs (keyword_id);
CREATE INDEX IF NOT EXISTS idx_briefs_status  ON content_briefs (status, created_at);

-- -------------------------------------------------------------------------
-- FASE 3/6 — Produção e publicação
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS articles (
    id              BIGSERIAL PRIMARY KEY,
    brief_id        BIGINT      REFERENCES content_briefs(id) ON DELETE SET NULL,
    keyword_id      BIGINT      REFERENCES keywords(id) ON DELETE SET NULL,
    title           TEXT        NOT NULL,
    slug            TEXT        NOT NULL UNIQUE,
    meta_description TEXT       NOT NULL DEFAULT '',
    markdown        TEXT        NOT NULL DEFAULT '',
    html            TEXT        NOT NULL DEFAULT '',
    word_count      INTEGER     NOT NULL DEFAULT 0,
    reading_minutes INTEGER     NOT NULL DEFAULT 0,
    faq             JSONB       NOT NULL DEFAULT '[]'::jsonb,
    schema_json     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    internal_links  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    tags            TEXT[]      NOT NULL DEFAULT '{}',
    category        TEXT,
    -- Governança
    status          TEXT        NOT NULL DEFAULT 'drafting',
                    -- drafting | reviewing | needs_work | approved | scheduled
                    -- | published | failed | rejected
    quality_score   NUMERIC(5,2),
    review_report   JSONB       NOT NULL DEFAULT '{}'::jsonb,
    similarity_max  NUMERIC(5,4),
    approved_by     TEXT,
    approved_at     TIMESTAMPTZ,
    -- Publicação
    wp_post_id      BIGINT,
    published_url   TEXT,
    published_at    TIMESTAMPTZ,
    scheduled_for   TIMESTAMPTZ,
    last_optimized_at TIMESTAMPTZ,
    error           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_articles_status    ON articles (status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles (published_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_wp        ON articles (wp_post_id);

-- Histórico: toda alteração de conteúdo/título fica registrada.
CREATE TABLE IF NOT EXISTS article_revisions (
    id          BIGSERIAL PRIMARY KEY,
    article_id  BIGINT      NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    reason      TEXT        NOT NULL,   -- generation | ai_review | ctr_rewrite | refresh
    diff_summary TEXT,
    payload     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_revisions_article ON article_revisions (article_id, created_at DESC);

-- -------------------------------------------------------------------------
-- FASE 5 — Imagens
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS article_images (
    id          BIGSERIAL PRIMARY KEY,
    article_id  BIGINT      NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
    kind        TEXT        NOT NULL,  -- cover | section | infographic | pinterest
    prompt      TEXT        NOT NULL DEFAULT '',
    alt_text    TEXT        NOT NULL DEFAULT '',
    local_path  TEXT,
    wp_media_id BIGINT,
    url         TEXT,
    width       INTEGER,
    height      INTEGER,
    position    INTEGER     NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_images_article ON article_images (article_id, position);

-- -------------------------------------------------------------------------
-- Custo de IA
-- -------------------------------------------------------------------------
-- Custo de API por chamada — alimenta o painel e o resumo diário.
CREATE TABLE IF NOT EXISTS llm_usage (
    id            BIGSERIAL PRIMARY KEY,
    provider      TEXT        NOT NULL,   -- openai | anthropic | image
    model         TEXT        NOT NULL,
    purpose       TEXT        NOT NULL,   -- brief | article | review | image ...
    article_id    BIGINT      REFERENCES articles(id) ON DELETE SET NULL,
    input_tokens  BIGINT      NOT NULL DEFAULT 0,
    output_tokens BIGINT      NOT NULL DEFAULT 0,
    images        INTEGER     NOT NULL DEFAULT 0,
    cost_usd      NUMERIC(12,6) NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_llm_usage_date ON llm_usage (created_at DESC);

-- -------------------------------------------------------------------------
-- Observabilidade do pipeline
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id          BIGSERIAL PRIMARY KEY,
    phase       TEXT        NOT NULL,  -- editorial | production | publishing | ...
    trigger     TEXT        NOT NULL DEFAULT 'n8n',  -- n8n | scheduler | manual | api
    status      TEXT        NOT NULL DEFAULT 'running',  -- running | success | partial | failed
    stats       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    error       TEXT,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_runs_phase ON pipeline_runs (phase, started_at DESC);

-- -------------------------------------------------------------------------
-- Gatilho genérico de updated_at
-- -------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DO $$
DECLARE t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY['keywords','content_briefs','articles'] LOOP
        EXECUTE format(
            'DROP TRIGGER IF EXISTS trg_touch_%1$s ON %1$s;
             CREATE TRIGGER trg_touch_%1$s BEFORE UPDATE ON %1$s
             FOR EACH ROW EXECUTE FUNCTION touch_updated_at();', t);
    END LOOP;
END $$;
