# =========================================================================
#  ShortsBlog — motor de conteúdo
#  Os alvos de operação agem nos containers da stack sb-engine
#  (deploy/stacks/engine, E2.7). `make test` usa a stack efémera sb-engine-test.
# =========================================================================
SHELL := /bin/bash
API := sb-engine-api
PROD_COMPOSE := docker compose -f ../../deploy/stacks/engine/docker-compose.yml

.DEFAULT_GOAL := help
.PHONY: help health migrate reimport reimport-apply vault-status vault-import shell test \
        lint typecheck check

help: ## Lista os comandos
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	 | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

health: ## Checa as dependências (/health/ready)
	@docker exec $(API) python -c "import urllib.request,json; \
	print(json.dumps(json.load(urllib.request.urlopen('http://127.0.0.1:8000/health/ready')), \
	indent=2, ensure_ascii=False))"

migrate: ## Migrações + papel sb_app pelo one-shot sb-engine-migrate (admin; as apps são sb_app)
	$(PROD_COMPOSE) up sb-engine-migrate

reimport: ## E18.3a — mostra o que o YAML de content-config/ mudaria no banco (não grava)
	$(PROD_COMPOSE) run --rm --no-deps sb-engine-migrate python -m app.core.reimport

reimport-apply: ## E18.3a — aplica o diff revisto: make reimport-apply FP=<impressão> [SOBRESCREVER=1]
	@test -n "$(FP)" || { echo "Informe FP=<impressão> mostrada por make reimport"; exit 2; }
	$(PROD_COMPOSE) run --rm --no-deps sb-engine-migrate python -m app.core.reimport --apply \
	  --fingerprint=$(FP) $(if $(SOBRESCREVER),--sobrescrever-portal,)

vault-status: ## E18.3b — origem de cada segredo (cofre, ambiente ou faltando), mascarado
	docker exec $(API) python -m app.core.vault_cli status

vault-import: ## E18.3b — ambiente → cofre, um item: make vault-import ITEM=<item> [APLICAR=1]
	@test -n "$(ITEM)" || { echo "Informe ITEM=<item> (veja make vault-status)"; exit 2; }
	docker exec $(API) python -m app.core.vault_cli import-env $(ITEM) $(if $(APLICAR),--apply,)

shell: ## Shell dentro do container da API
	docker exec -it $(API) bash

# --- Testes (stack efémera sb-engine-test, ver deploy/stacks/engine/compose.test.yml)
TEST_COMPOSE := docker compose -f ../../deploy/stacks/engine/compose.test.yml

test: ## Corre a suíte pytest em Postgres/Redis efémeros e destrói a stack no fim
	@$(TEST_COMPOSE) build tests
	@$(TEST_COMPOSE) run --rm tests pytest $(ARGS); status=$$?; \
	 $(TEST_COMPOSE) down -v --remove-orphans >/dev/null 2>&1; exit $$status

lint: ## ruff (config em ruff.toml) — não precisa de Postgres/Redis
	@$(TEST_COMPOSE) run --rm --no-deps tests ruff check --no-cache app tests; status=$$?; \
	 $(TEST_COMPOSE) down -v --remove-orphans >/dev/null 2>&1; exit $$status

typecheck: ## mypy (config em mypy.ini; estrito em services/notify)
	@$(TEST_COMPOSE) run --rm --no-deps tests mypy; status=$$?; \
	 $(TEST_COMPOSE) down -v --remove-orphans >/dev/null 2>&1; exit $$status

check: lint typecheck test ## lint + tipos + testes (o que a revisão da E2 pede)
