-- D-118: ideias que o teto diário por feed (`feed_max_ideas_day`) deixou de fora, por dia local
-- e por fonte. A descoberta soma a cada leitura; o resumo diário mostra o total por blog e os
-- feeds mais cortados (secção «Entrada de pautas»). Só contagens: nenhum título fica guardado.
CREATE TABLE IF NOT EXISTS feed_capped (
    local_date DATE   NOT NULL,
    source_id  BIGINT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    ideas      INTEGER NOT NULL DEFAULT 0 CHECK (ideas >= 0),
    PRIMARY KEY (local_date, source_id)
);
