-- E18.2: pauta manual marcada como «tema intemporal» (guia, explicação). Na busca de reforço,
-- como a culinária e a pauta de resgate (D-101), aceita páginas de referência de qualquer data
-- em vez do filtro de frescor das notícias (`searxng_max_age_days`).
ALTER TABLE ideas ADD COLUMN IF NOT EXISTS timeless BOOLEAN NOT NULL DEFAULT false;
