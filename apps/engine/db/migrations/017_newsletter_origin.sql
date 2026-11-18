-- D-094 (opção A da D-OPEN-15): ideias vindas de newsletters de fabricantes.
-- A newsletter nunca é fonte (D-070): a ideia nasce «só pauta» e os fatos vêm
-- das páginas de destino; `min_sources` fica intacto.

ALTER TABLE ideas DROP CONSTRAINT ideas_origin_check;
ALTER TABLE ideas ADD CONSTRAINT ideas_origin_check
    CHECK (origin IN ('rss', 'manual', 'newsletter'));
