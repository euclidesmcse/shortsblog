-- =========================================================================
--  ShortsBlog · Descoberta de ideias (E6.1)
--
--  `sources` passa a espelhar content-config/feeds/<site>.yaml (só feeds validados
--  na E5) e guarda o estado da última leitura. `ideas` ganha o necessário para a
--  deduplicação: chave do título normalizado, data da notícia, fonte, e a ideia
--  principal de que esta é repetição (a mesma notícia noutro feed serve de
--  corroboração, não de segundo artigo).
-- =========================================================================

ALTER TABLE sources
    ADD COLUMN IF NOT EXISTS tier        TEXT NOT NULL DEFAULT 'veiculo',
    ADD COLUMN IF NOT EXISTS lang        TEXT NOT NULL DEFAULT 'pt',
    ADD COLUMN IF NOT EXISTS fail_count  INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS last_items  INTEGER;

ALTER TABLE ideas
    ADD COLUMN IF NOT EXISTS source_id     BIGINT REFERENCES sources(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS published_at  TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS title_key     TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS duplicate_of  BIGINT REFERENCES ideas(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS created_by    TEXT;

CREATE INDEX IF NOT EXISTS idx_ideas_recent ON ideas (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ideas_url    ON ideas (canonical_url) WHERE canonical_url IS NOT NULL;
