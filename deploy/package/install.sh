#!/usr/bin/env bash
# =========================================================================
#  ShortsBlog — instalação do pacote (E19.2). Rode a partir desta pasta:
#
#      ./install.sh
#
#  Pergunta (ou lê do ambiente) o endereço do portal e do blog, o e-mail do administrador e o nome
#  do blog; gera as senhas internas; monta ./config; sobe a stack; instala o WordPress e cria a
#  credencial que o motor usa para publicar. Pode repetir: cada passo confere se já está feito e
#  nunca troca uma senha que já existe.
#
#  Variáveis aceitas (modo sem perguntas): SB_PANEL_HOST, SB_SITE_HOST, SB_ADMIN_EMAIL,
#  SB_SITE_KEY, SB_SITE_NAME, SB_SITE_TAGLINE, SB_TZ, SB_SCHEME (https|http), SB_HTTP_PORT,
#  SB_HTTPS_PORT, COMPOSE_PROJECT_NAME. SB_NO_UP=1 só gera os arquivos (não sobe nada).
#
#  Imagens (E19.3): sem SB_IMAGE_TAG, o motor e o gateway são construídos a partir do repositório.
#  Com SB_IMAGE_TAG=<versão> (ex.: v0.1.0), são baixadas de SB_REGISTRY (padrão
#  ghcr.io/euclidesmcse) — enquanto as imagens forem privadas, antes: docker login ghcr.io.
# =========================================================================
set -euo pipefail
cd "$(dirname "$0")"
umask 077

say() { printf '\n== %s\n' "$*"; }
die() { printf 'ERRO: %s\n' "$*" >&2; exit 1; }
rand() { head -c "$1" /dev/urandom | od -An -tx1 | tr -d ' \n'; }

command -v docker >/dev/null || die "docker não encontrado"
docker compose version >/dev/null 2>&1 || die "falta o plugin 'docker compose' (v2)"

ask() {  # ask VAR "pergunta" [padrão]
  local var=$1 prompt=$2 def=${3:-} val=${!1:-}
  if [ -z "$val" ]; then
    if [ -t 0 ]; then
      read -r -p "$prompt${def:+ [$def]}: " val
    fi
    val=${val:-$def}
  fi
  [ -n "$val" ] || die "$var é obrigatório"
  printf -v "$var" '%s' "$val"
}

# --- 1. .env: endereços e senhas (gerado uma vez; nunca reescrito) ---------------------------
if [ -f .env ]; then
  say ".env já existe: mantido (as senhas não mudam)"
else
  say "Configuração inicial"
  ask SB_PANEL_HOST "Endereço do portal (ex.: painel.seudominio.com)"
  ask SB_SITE_HOST "Endereço do blog (ex.: blog.seudominio.com)"
  ask SB_ADMIN_EMAIL "E-mail do administrador"
  ask SB_SITE_KEY "Identificador do blog (minúsculas, dígitos e hífen)" "exemplo"
  ask SB_SITE_NAME "Nome do blog" "Tecnologia pelo Mundo"
  ask SB_SITE_TAGLINE "Descrição curta do blog" "Tecnologia, IA e segurança, do mundo todo, em português."
  ask SB_TZ "Fuso horário" "America/Sao_Paulo"
  # O mesmo padrão do motor (sites.Site); na credencial o hífen vira `_` (SB_WP_<SITE>_…, R71).
  [[ $SB_SITE_KEY =~ ^[a-z][a-z0-9-]{1,30}$ ]] || die "identificador inválido (minúsculas, dígitos e hífen): $SB_SITE_KEY"
  case "${SB_SCHEME:-https}" in https|http) ;; *) die "SB_SCHEME tem de ser https ou http" ;; esac
  [[ ${SB_IMAGE_TAG:-} =~ ^[A-Za-z0-9._-]{0,64}$ ]] || die "SB_IMAGE_TAG inválida: $SB_IMAGE_TAG"
  for v in SB_SITE_NAME SB_SITE_TAGLINE; do
    [[ ${!v} != *'"'* && ${!v} != *'@@'* ]] || die "$v não pode ter aspas duplas"
  done
  tmp=$(mktemp .env.XXXXXX)
  {
    echo "# Gerado por install.sh em $(date -u +%FT%TZ). GUARDE UMA CÓPIA FORA DESTA MÁQUINA:"
    echo "# sem o SB_SECRET_KEY o cofre do portal (chaves de IA, SMTP…) fica ilegível."
    echo "COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME:-shortsblog}"
    echo "SB_SCHEME=${SB_SCHEME:-https}"
    echo "SB_HTTP_PORT=${SB_HTTP_PORT:-80}"
    echo "SB_HTTPS_PORT=${SB_HTTPS_PORT:-443}"
    echo "SB_PANEL_HOST=$SB_PANEL_HOST"
    echo "SB_SITE_HOST=$SB_SITE_HOST"
    echo "SB_ADMIN_EMAIL=$SB_ADMIN_EMAIL"
    echo "SB_SITE_KEY=$SB_SITE_KEY"
    echo "SB_SITE_NAME=\"$SB_SITE_NAME\""
    echo "SB_SITE_TAGLINE=\"$SB_SITE_TAGLINE\""
    echo "SB_TZ=$SB_TZ"
    echo "SB_WP_PUBLISH_MODE=draft"
    echo "SB_DB_ADMIN_PASSWORD=$(rand 24)"
    echo "SB_DB_APP_PASSWORD=$(rand 24)"
    echo "SB_API_TOKEN=$(rand 32)"
    echo "SB_SECRET_KEY=$(rand 32)"
    echo "SB_PANEL_PASSWORD=$(rand 12)"
    echo "SB_WP_DB_PASSWORD=$(rand 24)"
    echo "SB_WP_DB_ROOT_PASSWORD=$(rand 24)"
    echo "SB_WP_ADMIN_PASSWORD=$(rand 16)"
    if [ -n "${SB_IMAGE_TAG:-}" ]; then
      reg=${SB_REGISTRY:-ghcr.io/euclidesmcse}
      echo "SB_ENGINE_IMAGE=$reg/shortsblog-engine:$SB_IMAGE_TAG"
      echo "SB_GATEWAY_IMAGE=$reg/shortsblog-ai-gateway:$SB_IMAGE_TAG"
      echo "SB_PULL_POLICY=missing"
    fi
  } >"$tmp"
  mv "$tmp" .env
  echo ".env criado (permissão 600)"
fi
set -a; . ./.env; set +a

# --- 2. ./config: blog, política, feeds (só o que falta) -------------------------------------
say "Configuração do blog em ./config"
fill() {  # fill origem destino
  [ -f "$2" ] && { echo "mantido: $2"; return; }
  mkdir -p "$(dirname "$2")"
  sed -e "s|@@SITE_KEY@@|$SB_SITE_KEY|g" -e "s|@@SITE_NAME@@|$SB_SITE_NAME|g" \
      -e "s|@@SITE_TAGLINE@@|$SB_SITE_TAGLINE|g" -e "s|@@SITE_HOST@@|$SB_SITE_HOST|g" \
      -e "s|@@TZ@@|$SB_TZ|g" "$1" >"$2"
  chmod 644 "$2"
  echo "criado: $2"
}
umask 022
fill config-template/sites/site.yaml "config/sites/$SB_SITE_KEY.yaml"
fill config-template/policies/_comum.md config/policies/_comum.md
fill config-template/policies/site.md "config/policies/$SB_SITE_KEY.md"
fill config-template/feeds/site.yaml "config/feeds/$SB_SITE_KEY.yaml"
fill config-template/wordpress-sites/site.json "config/wordpress-sites/$SB_SITE_KEY.json"
fill config-template/sem-leitura.yaml config/sem-leitura.yaml
fill config-template/newsletters.yaml config/newsletters.yaml
chmod 755 config config/*/

# --- 3. SearXNG: chave própria desta instalação ----------------------------------------------
if [ ! -f searxng/settings.yml ]; then
  mkdir -p searxng
  cat >searxng/settings.yml <<EOF
use_default_settings: true
general:
  instance_name: "shortsblog"
server:
  secret_key: "$(rand 32)"
  limiter: false
  public_instance: false
  image_proxy: false
search:
  safe_search: 0
  formats: [html, json]
EOF
  chmod 644 searxng/settings.yml   # o usuário do container precisa ler
  echo "searxng/settings.yml criado"
fi
umask 077
[ -f wp-credentials.env ] || : >wp-credentials.env

[ "${SB_NO_UP:-}" = 1 ] && { say "SB_NO_UP=1: arquivos prontos, nada foi iniciado"; exit 0; }

# --- 4. Subir a stack ---------------------------------------------------------------------------
if [ "${SB_PULL_POLICY:-build}" = build ]; then
  say "Construindo as imagens e subindo a stack (a 1.ª vez demora alguns minutos)"
  docker compose build
else
  # Os outros serviços do motor usam a mesma imagem com pull_policy never: baixar antes do up.
  say "Baixando as imagens e subindo a stack"
  docker pull "$SB_ENGINE_IMAGE" || die "não baixou $SB_ENGINE_IMAGE (imagem privada? docker login ghcr.io)"
  docker pull "$SB_GATEWAY_IMAGE" || die "não baixou $SB_GATEWAY_IMAGE"
fi
docker compose up -d

say "Esperando o WordPress"
for _ in $(seq 1 60); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q sb-wp)" 2>/dev/null)" = healthy ] && break
  sleep 5
done
[ "$(docker inspect -f '{{.State.Health.Status}}' "$(docker compose ps -q sb-wp)")" = healthy ] \
  || die "o WordPress não ficou saudável: docker compose logs sb-wp"

# --- 5. WordPress: instalação, categorias e a credencial do motor --------------------------------
wp() { docker compose run --rm -T sb-wp-cli "$@"; }
say "Instalando o WordPress"
wp sh /setup/install.sh
wp eval-file /setup/categories.php

SITE_UP=$(tr '[:lower:]-' '[:upper:]_' <<<"$SB_SITE_KEY")
if grep -q "^SB_WP_${SITE_UP}_APP_PASSWORD=." wp-credentials.env; then
  echo "credencial do motor já existe: mantida"
else
  wp user get sb-publisher --field=ID >/dev/null 2>&1 || \
    wp user create sb-publisher "publisher@$SB_SITE_HOST" --role=author \
       --display_name="Redação" --porcelain >/dev/null
  if wp user application-password list sb-publisher --field=name 2>/dev/null | grep -qx motor; then
    die "a Application Password 'motor' já existe no WordPress mas não em wp-credentials.env: apague-a (docker compose run --rm sb-wp-cli user application-password delete sb-publisher <uuid>) e rode de novo"
  fi
  pass=$(wp user application-password create sb-publisher motor --porcelain | tr -d '\r\n')
  [ -n "$pass" ] || die "a Application Password veio vazia"
  printf 'SB_WP_%s_APP_USER=sb-publisher\nSB_WP_%s_APP_PASSWORD=%s\n' "$SITE_UP" "$SITE_UP" "$pass" \
    >wp-credentials.env
  unset pass
  echo "credencial do motor criada (wp-credentials.env, permissão 600)"
  docker compose up -d   # recria o motor com a credencial
fi

# --- 6. Pronto -----------------------------------------------------------------------------------
say "Pronto"
cat <<EOF
Portal:  $SB_SCHEME://$SB_PANEL_HOST
Blog:    $SB_SCHEME://$SB_SITE_HOST

1. Entre no portal com o e-mail $SB_ADMIN_EMAIL e a senha inicial SB_PANEL_PASSWORD do arquivo
   .env (grep SB_PANEL_PASSWORD .env). O portal pede uma senha nova no 1.º acesso.
2. Em IA, cadastre pelo menos um provedor (chave de API) para redator e revisor de famílias
   diferentes; em Sistema, o SMTP para os avisos de aprovação.
3. Na tela do blog, ligue-o. Os posts chegam como rascunho para aprovação no portal.
4. Guarde uma cópia do .env fora desta máquina (o SB_SECRET_KEY abre o cofre).
EOF
