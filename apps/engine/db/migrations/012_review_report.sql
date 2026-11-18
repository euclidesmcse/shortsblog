-- =========================================================================
--  ShortsBlog · Parecer do revisor guardado no job (E6.6, D-037)
--
--  O revisor classifica cada reparo: os graves bloqueiam o artigo; os menores
--  não bloqueiam e seguem como notas para quem aprova. Ficam aqui, com a nota e
--  quem reviu, para o ecrã de aprovação (E10) os mostrar.
-- =========================================================================

ALTER TABLE publication_jobs
    ADD COLUMN IF NOT EXISTS review_report JSONB NOT NULL DEFAULT '{}'::jsonb;
