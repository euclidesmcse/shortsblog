-- D-110: cobertura mundial como regra do CÓDIGO, não só do prompt. Cada ideia passa a saber de
-- que país trata (código ISO de 2 letras; «XX» = alcance mundial ou vários países). A triagem
-- preenche; a escolha (`discovery.pick`) recusa o que estoura a cota por país do blog.
ALTER TABLE ideas ADD COLUMN IF NOT EXISTS country TEXT;
-- As ideias de feed ainda na fila foram triadas sem o país: voltam à triagem para recebê-lo.
UPDATE ideas SET relevance = NULL
 WHERE status = 'new' AND country IS NULL AND origin NOT IN ('manual', 'rescue');
