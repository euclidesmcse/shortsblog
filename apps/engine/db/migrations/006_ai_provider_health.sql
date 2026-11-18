-- =========================================================================
--  ShortsBlog · Saúde dos provedores de IA (E3.6)
--
--  Uma linha por provedor, escrita pelo probe (a cada 5 min, no scheduler) e
--  pelo resultado das chamadas reais. `not_authenticated` e `quota_exhausted`
--  tiram o provedor da cadeia até o probe o repor.
--  `source`: quem escreveu o estado — `probe` (verificação barata) ou `call`
--  (chamada real). Um mau estado vindo de uma chamada real só é reposto pelo
--  probe depois do período de espera (o probe barato não vê cota esgotada).
-- =========================================================================

CREATE TABLE IF NOT EXISTS ai_provider_health (
    provider_id BIGINT      PRIMARY KEY REFERENCES ai_providers(id) ON DELETE CASCADE,
    status      TEXT        NOT NULL
                CHECK (status IN ('ok', 'not_authenticated', 'quota_exhausted', 'error')),
    source      TEXT        NOT NULL CHECK (source IN ('probe', 'call')),
    detail      TEXT        NOT NULL DEFAULT '',
    checked_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
