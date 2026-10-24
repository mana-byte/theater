.PHONY: test test-unit test-serial lint format typecheck compat suite canaries

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

suite:         ## live conformance suite on the installed CLI (HARNESS=vibe ARGS="-- -k mcp")
	uv run python -m scripts.harness_compat suite $(HARNESS) $(ARGS)
canaries:      ## the live suite must notice deliberately broken plugins (slow; HARNESS=vibe to narrow)
	HARNESS_COMPAT_CANARIES=1 uv run pytest tests/compat/test_canaries.py $(if $(HARNESS),-k $(HARNESS)) -n auto
compat:        ## qualify new upstream CLI releases locally, dry run (HARNESS=codex VERSION=0.162.1)
	uv run python -m scripts.harness_compat run $(if $(HARNESS),--harness $(HARNESS)) $(if $(VERSION),--version $(VERSION))
