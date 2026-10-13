.PHONY: test test-serial lint format typecheck

test:          ## full suite, tmux tests included, parallel across cores
	uv run pytest -n auto

test-serial:   ## full suite in one process (flake debugging)
	uv run pytest

lint:
	uv run ruff check

format:
	uv run ruff format

typecheck:
	uv run mypy
