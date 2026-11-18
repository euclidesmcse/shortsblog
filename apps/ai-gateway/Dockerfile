# =========================================================================
#  ShortsBlog — AI Gateway (E3.2, ADR 0003)
#  CLIs de assinatura (Claude Code, Codex, Antigravity) + API interna.
#
#  Estágios:  clis (Node: instala as CLIs com versão fixa)
#           → deps → test → runtime (o último, alvo padrão do build)
#
#  Base glibc obrigatória: o Codex não publica build musl (ADR 0003).
#  Atualizar uma CLI = mudar o ARG, reconstruir e correr tests/isolation.sh.
# =========================================================================
FROM node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c AS clis

ARG CLAUDE_CODE_VERSION=2.1.288
ARG CODEX_VERSION=0.160.0
ARG AGY_VERSION=1.2.16
# Um tarball por arquitetura (multi-arch, E19.3). URL e sha512 dos manifestos públicos
# …/<versão>/linux-arm/manifest.json e …/linux-x64/manifest.json.
ARG AGY_BASE=https://storage.googleapis.com/antigravity-public/antigravity-cli/1.2.16-5594158052802560
ARG AGY_SHA512_ARM64=d17d8044736d31e24ce7c4cb3b75faef3ce5102ecaad31aef95785c0f3c13fa0615d7b11fa411eb73b848489adf9d411178446f3148e30353a2048cd3e731971
ARG AGY_SHA512_AMD64=c31f09265d4f6aef7c03b5105661ae98f9a8365c6c812c68adcc4a5e1f8b8f96c05fd2e4408a52f5e87e1b561ac350f4ffa7ad41a191893be7a3730ccf9d6553

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# E19.6 (D-112): Claude Code e agy são proprietários e NÃO podem ir numa imagem redistribuída.
# true  = a nossa produção (uso próprio): as três CLIs.
# false = a imagem publicada no GHCR: só o Codex (Apache-2.0) e os provedores por API.
ARG PROPRIETARY_CLIS=true

RUN pkgs="@openai/codex@${CODEX_VERSION}" \
 && if [ "$PROPRIETARY_CLIS" = true ]; then pkgs="$pkgs @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"; fi \
 && npm install --global --prefix /opt/clis --no-fund --no-audit $pkgs

# O instalador oficial do agy só instala a última versão; aqui o tarball é
# fixado por URL e verificado por sha512 (valores do manifesto público).
# A arquitetura vem do dpkg (funciona no builder clássico e no buildx/QEMU).
RUN if [ "$PROPRIETARY_CLIS" != true ]; then echo "agy: fora desta imagem (D-112)"; exit 0; fi \
 && arch="$(dpkg --print-architecture)" \
 && case "$arch" in \
      arm64) url="${AGY_BASE}/linux-arm/cli_linux_arm64.tar.gz"; sum="${AGY_SHA512_ARM64}" ;; \
      amd64) url="${AGY_BASE}/linux-x64/cli_linux_x64.tar.gz";   sum="${AGY_SHA512_AMD64}" ;; \
      *) echo "arquitetura sem agy: $arch" >&2; exit 1 ;; \
    esac \
 && curl -fsSL "$url" -o /tmp/agy.tar.gz \
 && echo "${sum}  /tmp/agy.tar.gz" | sha512sum -c - \
 && tar -xzf /tmp/agy.tar.gz -C /tmp antigravity \
 && install -m 0755 /tmp/antigravity /opt/clis/bin/agy \
 && rm -f /tmp/antigravity /tmp/agy.tar.gz


# Base fixada por digest, a mesma do motor (apps/engine/Dockerfile).
FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016 AS deps

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Node só para o lançador do Codex (bin/codex.js); o Claude e o agy são nativos.
COPY --from=clis /usr/local/bin/node /usr/local/bin/node
COPY --from=clis /opt/clis /opt/clis
ENV PATH=/opt/clis/bin:$PATH

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Utilizador sem privilégios. As pastas de estado existem na imagem com o dono
# certo para que os volumes nomeados nasçam graváveis pelo `sb` (o resto do FS
# é só-leitura no compose).
ENV HOME=/home/sb \
    CLAUDE_CONFIG_DIR=/state/claude \
    CODEX_HOME=/state/codex
RUN useradd --create-home --uid 10001 sb \
 && mkdir -p /state/claude /state/codex /home/sb/.gemini /work \
 && chown -R sb:sb /state /home/sb /work /app


# --- Testes: dependências de desenvolvimento; código montado em /app -------
FROM deps AS test
COPY requirements-dev.txt /tmp/requirements-dev.txt
RUN pip install --no-cache-dir -r /tmp/requirements-dev.txt
USER sb
CMD ["pytest"]


# --- Produção ---------------------------------------------------------------
FROM deps AS runtime

COPY --chown=sb:sb app /app/app
# E19.6 (D-112): AGPL-3.0, Apache-2.0 do Codex e os componentes de terceiros, junto da imagem.
COPY licenses /licenses
LABEL org.opencontainers.image.licenses="AGPL-3.0-only"
USER sb
WORKDIR /work

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--app-dir", "/app", "--host", "0.0.0.0", "--port", "8080"]
