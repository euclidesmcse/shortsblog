# =========================================================================
#  ShortsBlog — motor de conteúdo (API / Worker / Scheduler)
#  Imagem única, o papel é definido pelo `command` no docker-compose.
#
#  Estágios:  deps → test (só para `make test`; o código entra por volume)
#                  → runtime (o último, alvo padrão do build)
# =========================================================================
# Base fixada por digest (builds reprodutíveis; revisão E2).
FROM python:3.12-slim@sha256:dddfd7e07f9d15aeeca61529320492139d21cac7f0070c00609243e51e4e0016 AS deps

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# libpq + fontes (Pillow escreve texto nas imagens de marca)
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libpq5 \
      fonts-dejavu-core \
      curl \
      ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Usuário sem privilégios
RUN useradd --create-home --uid 10001 sb \
 && mkdir -p /data/media \
 && chown -R sb:sb /app /data


# --- Testes: dependências de desenvolvimento; código montado em /app -------
FROM deps AS test
COPY requirements-dev.txt /tmp/requirements-dev.txt
RUN pip install --no-cache-dir -r /tmp/requirements-dev.txt
USER sb
CMD ["pytest"]


# --- Produção ---------------------------------------------------------------
FROM deps AS runtime

COPY --chown=sb:sb app /app/app
COPY --chown=sb:sb db /app/db
COPY --chown=sb:sb assets /app/assets
# E19.6 (D-112): AGPL-3.0 e os componentes de terceiros, junto da imagem.
COPY licenses /licenses
LABEL org.opencontainers.image.licenses="AGPL-3.0-only"
USER sb

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
