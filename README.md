# ShortsBlog

Motor de conteúdo para blogs WordPress com **aprovação humana**: descobre assuntos em feeds RSS
confiáveis, pesquisa, redige, revisa (com um modelo de IA de outra família) e traduz; cada post
traz as fontes e uma imagem com licença comprovada, e **nada é publicado sem uma pessoa aprovar**
no portal de operação.

Os blogs falam do mundo, em português do Brasil (com traduções para inglês, espanhol e chinês).

## O que vem no pacote

| Peça | Papel |
|---|---|
| `apps/engine` | motor (API e portal de operação, workers, agendador) — Python/FastAPI, RQ, PostgreSQL, Redis |
| `apps/ai-gateway` | ponte para os provedores de IA: qualquer API compatível com a OpenAI e o Codex CLI |
| `wordpress/` | tema e mu-plugins do blog |
| `content-config/` | exemplo de configuração editorial (blogs, políticas, feeds) |
| `deploy/package/` | **instalação com Docker Compose** (uma máquina, HTTPS automático) |
| `deploy/helm/shortsblog/` | **Helm chart** para Kubernetes (AKS, EKS, GKE…) |

## Instalar

- Uma máquina com Docker: [`deploy/package/README.md`](deploy/package/README.md) — `./install.sh`.
- Kubernetes: [`deploy/helm/shortsblog/README.md`](deploy/helm/shortsblog/README.md).

Depois do 1.º login, o portal abre os **Primeiros passos**: IA (chaves de API de dois provedores de
famílias diferentes), e-mail (opcional), credencial do WordPress e ligar o blog — sem editar
arquivos.

Imagens multi-arch (amd64 e arm64) no GHCR: `shortsblog-engine`, `shortsblog-ai-gateway` e
`shortsblog-wp-assets`.

## Licença

- Motor e AI Gateway: **GNU Affero General Public License v3.0** ([`LICENSE`](LICENSE)). Quem
  oferecer o sistema modificado como serviço precisa disponibilizar o código das modificações.
- Tema e mu-plugins do WordPress: **GPL-2.0-or-later** ([`wordpress/LICENSE`](wordpress/LICENSE)).
- Fonte Fraunces: SIL Open Font License 1.1.
- Componentes de terceiros: `apps/*/licenses/THIRD-PARTY-NOTICES.md` (também dentro das imagens,
  em `/licenses`).

A imagem do AI Gateway traz só o Codex CLI (Apache-2.0). As CLIs de assinatura proprietárias
(Claude Code, Antigravity) não são redistribuídas; quem quiser usá-las instala-as no próprio
ambiente, sob os termos de cada fornecedor.

Copyright (C) 2026 Euclides Silva. Para uma licença comercial (sem as obrigações da AGPL), fale
com o autor.
