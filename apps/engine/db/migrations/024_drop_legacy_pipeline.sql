-- D-111 (E19): tabelas do pipeline herdado do Growth Engine, cujo código saiu.
-- Em produção, a 17/11: articles, article_revisions, article_images, content_briefs e keywords
-- vazias; pipeline_runs com 1 execução manual sem efeito (09/11); llm_usage com 3 linhas dos
-- smoke tests de 03/10, custo 0. O custo de IA de verdade vive em provider_usage.
--
-- provider_usage.article_id apontava para articles (sempre NULL no fluxo atual): a coluna fica,
-- só a chave estrangeira sai, para o DROP não depender de CASCADE.
ALTER TABLE provider_usage DROP CONSTRAINT IF EXISTS provider_usage_article_id_fkey;

DROP TABLE IF EXISTS llm_usage;
DROP TABLE IF EXISTS article_images;
DROP TABLE IF EXISTS article_revisions;
DROP TABLE IF EXISTS articles;
DROP TABLE IF EXISTS content_briefs;
DROP TABLE IF EXISTS keywords;
DROP TABLE IF EXISTS pipeline_runs;
