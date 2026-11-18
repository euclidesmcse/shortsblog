# ShortsBlog — pacote de instalação (E19.2)

Um `docker compose` sobe a solução inteira numa máquina com Docker:

| Serviço | Papel |
|---|---|
| `caddy` | proxy com HTTPS automático (Let's Encrypt) para o portal e o blog |
| `sb-engine-api` | API e portal de operação (login próprio, sem Cloudflare Access) |
| `sb-engine-worker`, `sb-engine-worker-fast` | produção dos posts; e-mails, aprovações e publicação |
| `sb-engine-scheduler` | **os horários** (planejar o dia, entregar, publicar, ler os feeds…): sem n8n |
| `sb-engine-postgres`, `sb-engine-redis` | banco (com o cofre cifrado) e filas |
| `sb-engine-searxng` | busca de reforço para a pesquisa das pautas |
| `sb-ai-gateway` | ponte para os provedores de IA |
| `sb-wp`, `sb-wp-db`, `sb-wp-cron` | o blog WordPress (tema e plugins do ShortsBlog) |

Não depende de n8n, Infisical, Traefik, Cloudflare nem Brevo.

## O que você precisa trazer

- Uma máquina com Docker 24+ e o plugin `docker compose` v2, 4 GB de RAM livres, portas 80 e 443.
  O pacote cria 4 redes Docker: numa máquina que já tem muitas, o Docker pode ficar sem faixas de
  endereço ("all predefined address pools have been fully subnetted"); aumente `default-address-pools`
  no `daemon.json`.
- **Dois nomes** no seu DNS apontando para a máquina: o do portal e o do blog.
- **Uma IA:** pelo menos uma chave de API compatível com OpenAI (cadastrada depois, na tela de IA).
  A revisão tem de ser de uma família de modelo diferente da redação: o ideal são dois provedores.
- **E-mail (opcional):** um servidor SMTP, para os avisos de aprovação e a newsletter.

## Instalar

```bash
cd deploy/package
./install.sh
```

O script pergunta os dois endereços, o seu e-mail e o nome do blog, e então:

1. gera o `.env` com as senhas internas (permissão 600; nunca reescrito);
2. monta `./config` a partir de `config-template/` (o blog, a política editorial e os feeds);
3. constrói as imagens (ou baixa do registry, ver abaixo) e sobe a stack;
4. instala o WordPress e cria a credencial que o motor usa para publicar (`wp-credentials.env`).

Pode rodar de novo: cada passo confere o que já existe. Para responder sem perguntas, passe as
variáveis (`SB_PANEL_HOST`, `SB_SITE_HOST`, `SB_ADMIN_EMAIL`, `SB_SITE_NAME`…; lista no topo do
script).

### Imagens prontas do registry (E19.3)

O motor e o AI Gateway são publicados no GHCR para `linux/amd64` e `linux/arm64`
(`ghcr.io/euclidesmcse/shortsblog-engine` e `…/shortsblog-ai-gateway`). Para baixar em vez de
construir, passe a versão na primeira instalação:

```bash
docker login ghcr.io          # enquanto as imagens forem privadas (token com read:packages)
SB_IMAGE_TAG=v0.1.0 ./install.sh
```

O `.env` passa a ter `SB_ENGINE_IMAGE`, `SB_GATEWAY_IMAGE` e `SB_PULL_POLICY=missing`. Para
atualizar: mude a tag das duas imagens no `.env`, `docker pull` das duas e `docker compose up -d`.
`SB_REGISTRY` troca o registry (um espelho próprio, por exemplo).

**Guarde uma cópia do `.env` fora da máquina.** O `SB_SECRET_KEY` cifra o cofre do portal (chaves
de IA, SMTP, senhas dos blogs): sem ele, o que está no cofre não se recupera.

## Primeiro acesso

1. Abra o portal e entre com o seu e-mail e a senha `SB_PANEL_PASSWORD` do `.env`. O portal pede
   uma senha nova; esse usuário é o administrador.
2. Depois da senha, o portal abre os **Primeiros passos** (E19.5): uma página com o estado real de
   cada passo, sem editar arquivo nenhum — o nome e a descrição do blog; a **IA** (escolha o
   provedor, cole a chave e o modelo: o portal verifica na hora e põe o provedor à frente das
   cadeias; são precisos redator e revisor de **famílias diferentes**, o ideal são duas chaves);
   o **e-mail** (opcional); a credencial do WordPress (o instalador já a criou); e **ligar o
   blog**, que só libera com a IA e o WordPress prontos. A sala de controle avisa enquanto faltar
   algum passo.
3. Os posts nascem como **rascunho** e esperam a sua aprovação no portal; para publicar direto,
   mude `SB_WP_PUBLISH_MODE=live` no `.env` e rode `docker compose up -d`.
5. O WordPress nasce sem indexação (`blog_public=0`): ligue em Configurações → Leitura quando o blog
   estiver pronto.

## Regras que o pacote mantém

- **Nada é publicado sem aprovação humana.**
- **Cobertura mundial:** os blogs falam do mundo, em português; o código limita os posts sobre um
  mesmo país (D-110). Não é uma configuração.
- **Fontes:** o feed serve para descobrir e citar; o texto é sempre escrito de raiz. Sites cujos
  termos ou `robots.txt` vetam robôs de IA entram só como pauta (`config/sem-leitura.yaml`).
  Confira os termos de cada feed que acrescentar.

## Operação

```bash
docker compose ps                     # estado
docker compose logs -f sb-engine-worker
docker compose up -d                  # aplicar mudança no .env ou numa imagem nova
docker compose run --rm sb-wp-cli <comando wp>
```

Backup: os volumes `pgdata` (banco), `wp_db` e `wp_html` (blog) e `media`, mais o `.env`.
Um backup automático com destino externo está previsto (E19).

## Limites desta versão (E19.2)

- Imagens do registry ainda **privadas** (a licença e o modelo de distribuição são da E19.6);
  o tema e os plugins do WordPress ainda vêm de `../../wordpress`, então o pacote roda de dentro
  do repositório. Para Kubernetes: o Helm chart em `deploy/helm/shortsblog/` (E19.4), que traz
  esses arquivos numa imagem própria (`shortsblog-wp-assets`).
- Um blog por instalação.
- `SB_SCHEME=http` serve para conhecer o portal numa máquina local; a publicação exige HTTPS.
- Assinaturas por CLI (Claude, Codex, Gemini) exigem login dentro do container `sb-ai-gateway`
  e os termos de cada assinatura; o caminho do pacote é a chave de API.
