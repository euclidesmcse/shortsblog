-- =========================================================================
--  ShortsBlog (fork do Growth Engine) · Índice de cluster
--
--  A clusterização (uma keyword por intenção de busca) consulta
--  `cluster_id` a cada aprovação e a cada briefing. Sem índice, isso vira
--  sequential scan assim que a tabela passar de alguns milhares de linhas.
-- =========================================================================

CREATE INDEX IF NOT EXISTS idx_keywords_cluster
    ON keywords (cluster_id, score DESC)
 WHERE cluster_id IS NOT NULL;

-- Usado por `approve_top` para pular clusters já cobertos.
CREATE INDEX IF NOT EXISTS idx_keywords_cluster_status
    ON keywords (cluster_id, status)
 WHERE cluster_id IS NOT NULL;
