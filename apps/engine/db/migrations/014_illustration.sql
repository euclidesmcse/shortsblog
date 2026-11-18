-- E7: imagem de cada artigo, com prova de licença (D-012, R10).
ALTER TABLE image_assets
    ADD COLUMN IF NOT EXISTS sha256       TEXT,
    ADD COLUMN IF NOT EXISTS width        INTEGER,
    ADD COLUMN IF NOT EXISTS height       INTEGER,
    ADD COLUMN IF NOT EXISTS title        TEXT        NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS credit       TEXT        NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS ai_generated BOOLEAN     NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS prompt       TEXT        NOT NULL DEFAULT '',
    -- idioma (pt-BR, en, es, zh-CN) → texto alternativo / legenda
    ADD COLUMN IF NOT EXISTS alt_texts    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS captions     JSONB       NOT NULL DEFAULT '{}'::jsonb;
CREATE INDEX IF NOT EXISTS idx_images_source ON image_assets (source_url);
