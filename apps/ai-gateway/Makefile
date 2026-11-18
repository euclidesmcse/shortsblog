# =========================================================================
#  ShortsBlog — AI Gateway
#  Os testes correm num container efémero (`docker run --rm`) SEM REDE e sem
#  volumes: usam CLIs falsas, nunca chamam um provedor real nem gastam cota.
#  O teste de isolamento corre contra o container de produção sb-ai-gateway.
# =========================================================================
SHELL := /bin/bash
IMAGE := sb-ai-gateway-test:local
RUN := docker run --rm --network none --read-only --tmpfs /tmp:exec --tmpfs /work:uid=10001,gid=10001 \
       -e COVERAGE_FILE=/tmp/.coverage -e PYTHONDONTWRITEBYTECODE=1 \
       -v $(CURDIR):/app:ro -w /app $(IMAGE)

.DEFAULT_GOAL := help
.PHONY: help image test lint typecheck check isolation

help: ## Lista os comandos
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	 | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

image:
	@docker build -q --target test -t $(IMAGE) . >/dev/null

test: image ## pytest com CLIs falsas (sem rede)
	@$(RUN) pytest $(ARGS)

lint: image ## ruff
	@$(RUN) ruff check --no-cache app tests

typecheck: image ## mypy (estrito)
	@$(RUN) mypy

check: lint typecheck test ## lint + tipos + testes

isolation: ## isolamento do container em produção (24 verificações)
	@tests/isolation.sh
