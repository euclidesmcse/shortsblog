-- =========================================================================
--  ShortsBlog · Antigravity desligado por omissão (D-032; revisão E3 ronda 2, #3)
--
--  O `agy` executa a pesquisa web sem pedir permissão e não há opção para a
--  desligar (ADR 0003). O seed 005 cria-o ligado; sem esta migração, uma base
--  criada de raiz voltaria a tê-lo nas cadeias. Volta a ligar-se no painel, por
--  decisão do operador.
-- =========================================================================

UPDATE ai_providers SET enabled = false, updated_at = now() WHERE name = 'agy';
