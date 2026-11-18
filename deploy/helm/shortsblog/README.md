# ShortsBlog — Helm chart (E19.4)

O mesmo conteúdo do pacote compose (`deploy/package/`), para Kubernetes (AKS, EKS, GKE ou outro
cluster ≥ 1.27): motor (API/portal, 2 workers, agendador), Postgres, Redis, SearXNG, AI Gateway e
um blog WordPress (com MariaDB), num release. Um blog por release.

## O que você precisa trazer

- Um cluster com uma **storage class** padrão (ou `persistence.storageClass`) e um
  **Ingress controller** (`ingress.className`). Para o TLS: cert-manager (anotação em
  `ingress.annotations`) ou Secrets TLS com os nomes de `ingress.tls.*SecretName`.
- **Dois nomes** no DNS apontando para o Ingress: o do portal e o do blog. O motor publica no blog
  pelo endereço público (`https://<site.host>/wp-json`): o cluster tem de resolvê-lo e alcançá-lo.
- Saída para a internet (feeds, provedores de IA, e a 1.ª instalação do WordPress baixa o Polylang
  e os idiomas).
- **Uma IA** (chave de API) e, opcional, um SMTP — cadastrados depois, no portal.
- Enquanto as imagens forem **privadas** (até a E19.6): um Secret de pull do GHCR.

```bash
kubectl create namespace shortsblog
kubectl -n shortsblog create secret docker-registry ghcr \
  --docker-server=ghcr.io --docker-username=<usuário> --docker-password=<token read:packages>
```

## Instalar

```bash
helm install sb ./deploy/helm/shortsblog -n shortsblog \
  --set panel.host=painel.seudominio.com --set site.host=blog.seudominio.com \
  --set admin.email=voce@seudominio.com \
  --set ingress.className=nginx \
  --set-string 'ingress.annotations.cert-manager\.io/cluster-issuer=letsencrypt' \
  --set 'imagePullSecrets[0].name=ghcr' \
  --wait --wait-for-jobs --timeout 15m
helm test sb -n shortsblog
```

`helm test` confere: o motor vê o banco e o Redis e tem a credencial do blog; o gateway responde;
o WordPress está instalado e a credencial do motor entra na REST API.

O que acontece no 1.º install:

1. o Secret `<release>-secrets` nasce com todas as senhas internas (e fica mesmo com
   `helm uninstall`: `helm.sh/resource-policy: keep`);
2. o Job `<release>-migrate-<revisão>` aplica as migrações, cria o papel `sb_app` e semeia a
   configuração do blog (ConfigMap `<release>-config`, gerado de `deploy/package/config-template`);
   os pods do motor esperam todas as migrações da imagem estarem no banco;
3. o pod do blog copia o tema e os mu-plugins da imagem `shortsblog-wp-assets`, e o sidecar
   `setup-cron` instala o WordPress, cria as categorias e grava a **credencial do motor** com a
   senha `wpAppPassword` do Secret (o motor lê a mesma). Depois roda o WP-Cron de 5 em 5 minutos.

**Guarde uma cópia do Secret fora do cluster** (`kubectl get secret sb-secrets -o yaml`): o
`secretKey` abre o cofre do portal (chaves de IA, SMTP…).

## Primeiro acesso

O mesmo do pacote compose (`deploy/package/README.md`): entre no portal com `admin.email` e a
senha inicial (`kubectl -n shortsblog get secret sb-secrets -o jsonpath='{.data.panelPassword}' | base64 -d`),
troque-a e siga os **Primeiros passos** que o portal abre (IA, e-mail, ligar o blog). Os posts nascem como **rascunho** e esperam a sua
aprovação (`wpPublishMode=live` publica direto).

## Valores principais

| Valor | Padrão | Para quê |
|---|---|---|
| `panel.host`, `site.host`, `admin.email` | — | obrigatórios |
| `site.key`, `site.name`, `site.tagline` | `exemplo`… | o blog (`site.key`: minúsculas, dígitos e hífen) |
| `image.registry`, `image.tag` | `ghcr.io/euclidesmcse`, `v<appVersion>` | de onde vêm as 3 imagens |
| `existingSecret` | — | traga o seu Secret (GitOps: `helm template` não guarda as senhas geradas) |
| `ingress.*` | ligado, TLS ligado | classe, anotações, nomes dos Secrets TLS |
| `networkPolicy.enabled` | `true` | bancos, Redis, SearXNG e gateway só aceitam o motor; MariaDB só o WordPress |
| `persistence.*` | 8/1/4/4 Gi… | tamanhos e storage class |
| `persistence.mediaAccessMode` | `ReadWriteOnce` | RWO põe os pods do motor no mesmo nó (afinidade); com RWX, use `ReadWriteMany` |
| `scheme` | `https` | `http` só para conhecer o portal (o motor não publica em http) |

## Operação

```bash
kubectl -n shortsblog get pods
kubectl -n shortsblog logs deploy/sb-worker -f
helm upgrade sb ./deploy/helm/shortsblog -n shortsblog --reuse-values --set image.tag=v0.2.0
# Login das assinaturas por CLI (opcional; o caminho do pacote é a chave de API):
kubectl -n shortsblog exec -it deploy/sb-ai-gateway -- claude
```

Backup: os PVCs (`data-sb-postgres-0`, `data-sb-mariadb-0`, `sb-wp-html`, `sb-media`) e o Secret.
Os PVCs do gateway, da mídia e do blog também ficam com `helm uninstall`; os dos StatefulSets
(bancos) o Kubernetes nunca apaga sozinho.

## Limites desta versão

- Um blog por release; Postgres, Redis e MariaDB dentro do release (1 réplica cada).
- O motor publica pelo endereço público do blog (como no compose); a configuração editorial é a
  semente — depois, a fonte da verdade é o banco (tela Configuração do portal).
- Imagens ainda privadas (licença e distribuição: E19.6).
