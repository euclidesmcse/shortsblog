-- =========================================================================
--  ShortsBlog · Tentativas falhadas também contam para o limite diário
--  (revisão E3, achado #2)
--
--  Um timeout, uma resposta vazia ou um JSON inválido gastam cota da
--  assinatura na mesma. `outcome` guarda o resultado de cada tentativa que
--  chegou ao provedor: 'ok' ou o tipo de falha. O limite diário de chamadas
--  conta todas; o custo só existe nas 'ok' (as outras não trazem tokens).
-- =========================================================================

ALTER TABLE provider_usage
    ADD COLUMN IF NOT EXISTS outcome TEXT NOT NULL DEFAULT 'ok';
