"""Location reserved for Theater-shipped MCP-server plugin packages."""

from __future__ import annotations

from pathlib import Path


def plugin_dir() -> Path:
    """Return the shipped MCP-server package root."""
    return Path(__file__).parent


__all__ = ["plugin_dir"]
