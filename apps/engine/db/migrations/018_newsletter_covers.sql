-- D-099: a newsletter reutiliza a capa já publicada no WordPress (sem recriar imagem).
-- O publicador grava aqui a URL pública que o upload da mídia devolve, por idioma do motor
-- (ex.: {"pt-BR": "https://cloud.shortsblog.com/wp-content/uploads/.../sb-168-pt.webp", ...}).
ALTER TABLE image_assets ADD COLUMN wordpress_media_urls JSONB;
