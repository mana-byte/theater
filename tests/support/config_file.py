"""Write the isolated Theater home's config.toml."""

from __future__ import annotations

from theater import paths


def write_config(text: str) -> None:
    paths.config_path().write_text(text, encoding="utf-8")
