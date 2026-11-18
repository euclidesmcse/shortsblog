-- =========================================================================
--  ShortsBlog (fork do Growth Engine) · Seed — settings de runtime
--  Idempotente (ON CONFLICT DO NOTHING).
--  Programas de afiliado, sequência de e-mail, pesos de scoring de keywords
--  e benchmarks de CTR do upstream saíram na E2.2 (fora do escopo, D-016).
-- =========================================================================

INSERT INTO settings (key, value, description) VALUES
 ('pipeline.enabled_phases',
  '{"editorial":true,"production":true,"publishing":true}'::jsonb,
  'Liga/desliga fases individuais do pipeline sem redeploy'),
 ('cost.price_table',
  '{"gpt-5":{"in":1.25,"out":10.0},"gpt-4.1":{"in":2.0,"out":8.0},"claude-opus-5":{"in":5.0,"out":25.0},"text-embedding-3-small":{"in":0.02,"out":0.0},"gpt-image-1":{"per_image":0.04}}'::jsonb,
  'Preço USD por 1M tokens (ou por imagem) — base do cálculo de custo')
ON CONFLICT (key) DO NOTHING;
