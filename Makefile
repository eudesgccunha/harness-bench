# Common commands. Run `make` for the list. Dev tools come from uv (ADR 0004);
# smoke and unit tests need only plain python3.
PYTHON ?= python3
UV ?= uv
UV_RUN = $(UV) run --locked
UNITTEST = -m unittest discover -s tests -p 'test_*.py'
BANDIT_TARGETS = harness_bench.py harness_bench_litellm.py hbench scripts

.DEFAULT_GOAL := help

.PHONY: help sync lock smoke unit test golden-update coverage lint format format-check typecheck \
	security actionlint docs docs-check precommit hooks check docker-e2e clean clean-venv

help: ## List targets
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*## "} {printf "  %-14s %s\n", $$1, $$2}'

sync: ## Install dev tools from uv.lock into .venv
	$(UV) sync --locked --group dev

lock: ## Re-resolve uv.lock
	$(UV) lock

smoke: ## End-to-end smoke test (plain python3)
	$(PYTHON) tests/smoke.py

unit: ## Unit and golden tests (plain python3)
	$(PYTHON) $(UNITTEST)

test: smoke unit ## Smoke + unit tests

golden-update: ## Regenerate goldens (ONLY for an intended behavior change)
	@echo "WARNING: regenerating goldens. Use only for an intended behavior change and say so in the PR."
	HB_UPDATE_GOLDEN=1 $(PYTHON) $(UNITTEST)

coverage: ## Smoke + unit under coverage (incl. subprocesses), report, html
	$(UV_RUN) coverage erase
	$(UV_RUN) coverage run tests/smoke.py
	$(UV_RUN) coverage run $(UNITTEST)
	$(UV_RUN) coverage combine
	$(UV_RUN) coverage report -m
	$(UV_RUN) coverage html

lint: ## ruff check
	$(UV_RUN) ruff check .

format: ## ruff format (rewrites files)
	$(UV_RUN) ruff format .

format-check: ## ruff format --check
	$(UV_RUN) ruff format --check .

typecheck: ## mypy
	$(UV_RUN) mypy

security: ## bandit + gitleaks (missing gitleaks fails only when CI is set)
	$(UV_RUN) bandit -q -c pyproject.toml -r $(BANDIT_TARGETS)
	@if command -v gitleaks >/dev/null 2>&1; then \
		gitleaks dir . --config .gitleaks.toml; \
	elif [ -n "$$CI" ] && [ -z "$$GITLEAKS_VIA_ACTION" ]; then \
		echo "gitleaks not on PATH and CI is set: install it or set GITLEAKS_VIA_ACTION=1"; \
		exit 1; \
	elif [ -n "$$GITLEAKS_VIA_ACTION" ]; then \
		echo "gitleaks runs in its own CI action"; \
	else \
		echo "gitleaks not installed: skipped locally (https://github.com/gitleaks/gitleaks)"; \
	fi

actionlint: ## Lint the GitHub workflow
	$(UV_RUN) actionlint .github/workflows/ci.yml

docs: ## Regenerate HARNESS_BENCH.md
	$(PYTHON) scripts/build_md.py

docs-check: ## Fail if HARNESS_BENCH.md is stale (regenerates it)
	@$(PYTHON) scripts/docs_check.py

precommit: ## Run all pre-commit hooks
	$(UV_RUN) pre-commit run --all-files

hooks: ## Install the git pre-commit hook
	$(UV_RUN) pre-commit install

check: lint format-check typecheck security actionlint coverage docs-check ## Full "Done when" (smoke + unit run inside coverage)

docker-e2e: ## Container end-to-end test (needs docker and the built image)
	$(PYTHON) tests/docker_e2e.py

clean: ## Remove caches and coverage output
	find . -name __pycache__ -type d -prune -not -path './.venv/*' -exec rm -rf {} +
	rm -rf .mypy_cache .ruff_cache htmlcov .coverage*

clean-venv: ## Remove the uv environment
	rm -rf .venv
