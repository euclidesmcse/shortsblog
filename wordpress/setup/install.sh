#!/bin/sh
# =========================================================================
#  Instalação de um WordPress do ShortsBlog (E8.2–E8.8). Corre DENTRO do
#  container `cli`:   ./site.sh <site> run --rm cli sh /setup/install.sh
#  Pode repetir-se: cada passo verifica se já está feito.
# =========================================================================
set -eu
: "${SB_SITE_NAME:?}" "${SB_SITE_TAGLINE:?}" "${SB_SITE_HOST:?}"
# Padrões = a nossa instalação; o pacote (E19.2, deploy/package) passa os do instalador.
SB_WP_ADMIN_USER=${SB_WP_ADMIN_USER:-admin}
SB_WP_ADMIN_EMAIL=${SB_WP_ADMIN_EMAIL:-admin@example.com}
SB_SCHEME=${SB_SCHEME:-https}
SB_TZ=${SB_TZ:-America/Sao_Paulo}

if ! wp core is-installed 2>/dev/null; then
  : "${SB_WP_ADMIN_PASSWORD:?senha do administrador em falta}"
  # A senha vai por argumento dentro do container (nunca com --prompt: o WP-CLI ecoa o que lê
  # e o comando completo). A saída é calada para não a mostrar.
  wp core install --url="$SB_SCHEME://$SB_SITE_HOST" --title="$SB_SITE_NAME" \
    --admin_user="$SB_WP_ADMIN_USER" --admin_email="$SB_WP_ADMIN_EMAIL" \
    --admin_password="$SB_WP_ADMIN_PASSWORD" --skip-email \
    --quiet >/dev/null 2>&1 || { echo "ERRO: wp core install falhou"; exit 1; }
fi

wp language core install pt_BR es_ES zh_CN --quiet || true
wp site switch-language pt_BR --quiet || true
wp option update blogname "$SB_SITE_NAME" --quiet
wp option update blogdescription "$SB_SITE_TAGLINE" --quiet
wp option update timezone_string "$SB_TZ" --quiet
wp option update default_comment_status closed --quiet
wp option update default_ping_status closed --quiet
wp option update default_pingback_flag 0 --quiet
wp option update users_can_register 0 --quiet
wp option update posts_per_page 13 --quiet
wp option update blog_public 0 --quiet          # sem indexação até ao piloto (E14)
wp rewrite structure '/%postname%/' --quiet

# Conteúdo e plugins de exemplo do WordPress
wp post delete 1 2 3 --force --quiet 2>/dev/null || true
wp plugin delete hello akismet --quiet 2>/dev/null || true

wp plugin is-installed polylang || wp plugin install polylang --quiet
wp plugin is-active polylang || wp plugin activate polylang --quiet
wp language plugin install polylang pt_BR es_ES zh_CN --quiet 2>/dev/null || true
wp eval-file /setup/polylang.php
wp theme activate shortsblog --quiet
wp rewrite flush --quiet
echo "instalação concluída: $(wp option get home) · WordPress $(wp core version) · tema $(wp theme list --status=active --field=name)"
