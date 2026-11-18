-- D-101 (pauta de resgate): quando o slot ia bloquear, o editor propõe temas
-- intemporais e eles entram como ideias `origin = 'rescue'`, sem ligação — as
-- fontes vêm depois, pela busca (D-063), e todos os gates correm como sempre.

ALTER TABLE ideas DROP CONSTRAINT ideas_origin_check;
ALTER TABLE ideas ADD CONSTRAINT ideas_origin_check
    CHECK (origin IN ('rss', 'manual', 'newsletter', 'rescue'));
