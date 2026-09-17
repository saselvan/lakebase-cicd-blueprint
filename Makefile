## Lakebase CI/CD reference — one-command entrypoint.
## Run `make help` to see targets. Set PROFILE/INSTANCE/HOST/PGUSER/WAREHOUSE_ID in your env.

.DEFAULT_GOAL := help
.PHONY: help validate fmt seed deploy branch

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  Config:  edit config/tables.json (the tables) + terraform/terraform.tfvars"
	@echo "  Env:     PROFILE INSTANCE HOST PGUSER  (WAREHOUSE_ID for seed)"

validate: ## Local checks, no cloud: terraform validate/fmt, bash syntax, tables.json JSON
	cd terraform && terraform init -backend=false >/dev/null && terraform validate && terraform fmt -check
	bash -n scripts/*.sh
	python3 -c "import json; json.load(open('config/tables.json')); print('config/tables.json: valid JSON')"

fmt: ## Auto-format Terraform
	cd terraform && terraform fmt

seed: ## One-time: seed the demo Delta source table + UC schemas
	./scripts/seed_source.sh

deploy: ## Run the pipeline: terraform apply -> per table wait-for-ONLINE -> liquibase -> verify
	./scripts/deploy.sh

branch: ## Ephemeral branch test: make branch PROJECT=<id> NAME=pr-123
	@test -n "$(PROJECT)" || { echo "set PROJECT=<project-id> (and optionally NAME=<branch>)"; exit 1; }
	./scripts/branch_test.sh $(PROJECT) $(or $(NAME),pr-test)
