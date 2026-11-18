-- =========================================================================
--  ShortsBlog · Triagem de relevância das ideias (E6.1, D-040)
--
--  Antes de gastar uma pesquisa, uma chamada barata dá a cada título candidato
--  uma nota de 0 a 10 de pertinência para o blog. As de nota baixa são
--  descartadas; as outras ordenam a escolha. NULL = ainda não triada.
-- =========================================================================

ALTER TABLE ideas
    ADD COLUMN IF NOT EXISTS relevance INTEGER CHECK (relevance BETWEEN 0 AND 10);
