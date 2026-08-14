.PHONY: help install lint format fix test pre-commit

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## install dependencies
	uv sync

lint: ## lint check
	uv run ruff check src/ tests/

format: ## format code
	uv run ruff format src/ tests/

fix: ## auto-fix lint issues
	uv run ruff check --fix src/ tests/

test: ## run tests
	uv run pytest -v

pre-commit: ## run all checks (format, lint, test)
	uv run ruff format --check src/ tests/
	uv run ruff check src/ tests/
	uv run pytest -v
