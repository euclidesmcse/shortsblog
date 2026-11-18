-- =========================================================================
--  ShortsBlog · Custos e quotas dos provedores de IA (E3.8)
--
--  `provider_usage`: uma linha por chamada bem-sucedida feita pela cadeia.
--  Limites por provedor (em `ai_providers`):
--    daily_call_limit    — assinaturas: chamadas por dia, para não esgotar a
--                          cota que o Euclides usa no dia a dia (R19).
--    daily/monthly_budget_usd — provedores pagos (API key): teto de custo.
--    price_*_usd_mtok    — preço por milhão de tokens; sem preço o custo é 0
--                          e o orçamento não trava nada.
--  `ai_site_budgets`: teto de custo por blog nos provedores pagos. O blog é o
--  identificador do site (a tabela de sites chega na E4).
--  NULL = sem limite. Ao atingir um limite, o provedor sai da cadeia até ao
--  dia (ou mês) seguinte, no fuso do motor.
-- =========================================================================

CREATE TABLE IF NOT EXISTS provider_usage (
    id            BIGSERIAL PRIMARY KEY,
    provider      TEXT        NOT NULL,
    kind          TEXT        NOT NULL,
    model         TEXT        NOT NULL DEFAULT '',
    role          TEXT        NOT NULL,
    purpose       TEXT        NOT NULL DEFAULT 'generic',
    site          TEXT,
    article_id    BIGINT      REFERENCES articles(id) ON DELETE SET NULL,
    input_tokens  BIGINT      NOT NULL DEFAULT 0,
    output_tokens BIGINT      NOT NULL DEFAULT 0,
    cost_usd      NUMERIC(12,6) NOT NULL DEFAULT 0,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_provider_usage_provider ON provider_usage (provider, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_provider_usage_site ON provider_usage (site, created_at DESC)
 WHERE site IS NOT NULL;

ALTER TABLE ai_providers
    ADD COLUMN IF NOT EXISTS daily_call_limit    INTEGER       CHECK (daily_call_limit >= 0),
    ADD COLUMN IF NOT EXISTS daily_budget_usd    NUMERIC(10,2) CHECK (daily_budget_usd >= 0),
    ADD COLUMN IF NOT EXISTS monthly_budget_usd  NUMERIC(10,2) CHECK (monthly_budget_usd >= 0),
    ADD COLUMN IF NOT EXISTS price_in_usd_mtok   NUMERIC(10,4) CHECK (price_in_usd_mtok >= 0),
    ADD COLUMN IF NOT EXISTS price_out_usd_mtok  NUMERIC(10,4) CHECK (price_out_usd_mtok >= 0);

CREATE TABLE IF NOT EXISTS ai_site_budgets (
    site               TEXT PRIMARY KEY CHECK (site <> ''),
    daily_budget_usd   NUMERIC(10,2) CHECK (daily_budget_usd >= 0),
    monthly_budget_usd NUMERIC(10,2) CHECK (monthly_budget_usd >= 0),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Ponto de partida prudente para as assinaturas (ajustável no painel): 30 posts
-- por dia são ~180 chamadas repartidas pelos três provedores.
UPDATE ai_providers SET daily_call_limit = 150
 WHERE kind = 'subscription' AND daily_call_limit IS NULL;
