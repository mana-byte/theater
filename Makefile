.PHONY: test test-unit test-serial lint format typecheck compat

test:          ## full suite, tmux and integration tests included, parallel
	uv run pytest -n auto

test-unit:     ## inner dev loop: full suite minus real-binary integration tests
	uv run pytest -n auto -m "not integration"

test-serial:   ## full suite in one process (flake debugging)
	uv run pytest

lint:
	uv run ruff check

format:
	uv run ruff format

typecheck:
	uv run mypy

compat:        ## qualify new upstream CLI releases locally, dry run (HARNESS=codex VERSION=0.162.1)
	uv run python -m scripts.harness_compat run $(if $(HARNESS),--harness $(HARNESS)) $(if $(VERSION),--version $(VERSION))
