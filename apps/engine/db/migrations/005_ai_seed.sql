-- =========================================================================
--  ShortsBlog · Seed dos provedores de IA (E3.5). Idempotente.
--
--  Padrão do plano: Claude → Codex → Antigravity. No papel de revisor o Codex
--  vem primeiro para que o caso normal (Claude escreve) já tenha um revisor de
--  outra família à cabeça; em execução, o revisor salta sempre a família de
--  quem escreveu o artigo, seja qual for a ordem.
--  O agy é fixado num modelo Gemini (também serve modelos Claude — ADR 0003).
-- =========================================================================

INSERT INTO ai_providers (name, kind, family, model) VALUES
 ('claude', 'subscription', 'anthropic', ''),
 ('codex',  'subscription', 'openai',    ''),
 ('agy',    'subscription', 'google',    'gemini-3.1-pro-high')
ON CONFLICT (name) DO NOTHING;

INSERT INTO ai_role_chain (role, position, provider_id)
SELECT c.role, c.position, p.id
  FROM (VALUES
        ('writer',     1, 'claude'), ('writer',     2, 'codex'),  ('writer',     3, 'agy'),
        ('reviewer',   1, 'codex'),  ('reviewer',   2, 'claude'), ('reviewer',   3, 'agy'),
        ('translator', 1, 'claude'), ('translator', 2, 'codex'),  ('translator', 3, 'agy'),
        ('researcher', 1, 'claude'), ('researcher', 2, 'codex'),  ('researcher', 3, 'agy')
       ) AS c(role, position, name)
  JOIN ai_providers p ON p.name = c.name
ON CONFLICT DO NOTHING;
