-- D-115: post de reserva por blog. Um artigo intemporal, produzido com folga e aprovado no lote
-- normal, fica guardado; se o post do dia não ficar pronto a tempo, a reserva publica no horário
-- do slot (a reconciliação faz a troca) e o motor produz outra.
--
-- A reserva é um job como os outros (mesma pesquisa, gates, revisão, tradução e aprovação), com
-- `slot_number = 0` (nasce reserva e fica marcada assim para sempre) e `reserve = true` enquanto
-- espera; ao cobrir um slot, `reserve` vira false, `due_at_utc` passa a ser a hora do slot e
-- `covers_job_id` aponta para o job que ela cobriu.
ALTER TABLE publication_jobs ADD COLUMN IF NOT EXISTS reserve BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE publication_jobs ADD COLUMN IF NOT EXISTS covers_job_id BIGINT
    REFERENCES publication_jobs(id) ON DELETE SET NULL;

ALTER TABLE publication_jobs DROP CONSTRAINT IF EXISTS publication_jobs_slot_number_check;
ALTER TABLE publication_jobs ADD CONSTRAINT publication_jobs_slot_number_check
    CHECK (slot_number >= 0 AND slot_number <= 5);
ALTER TABLE publication_jobs DROP CONSTRAINT IF EXISTS publication_jobs_reserve_slot_check;
ALTER TABLE publication_jobs ADD CONSTRAINT publication_jobs_reserve_slot_check
    CHECK (NOT reserve OR slot_number = 0);

-- Um slot só é coberto uma vez.
CREATE UNIQUE INDEX IF NOT EXISTS uq_jobs_covers ON publication_jobs (covers_job_id)
    WHERE covers_job_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_jobs_reserve ON publication_jobs (site_key, status) WHERE reserve;

-- As pautas da reserva têm origem própria: os slots do dia nunca as pegam (e vice-versa).
ALTER TABLE ideas DROP CONSTRAINT IF EXISTS ideas_origin_check;
ALTER TABLE ideas ADD CONSTRAINT ideas_origin_check
    CHECK (origin = ANY (ARRAY['rss', 'manual', 'newsletter', 'rescue', 'reserve']));
