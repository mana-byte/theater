"""Shipped package-manifest harnesses loaded through the public plugin path."""

from __future__ import annotations

from pathlib import Path


def plugin_dir() -> Path:
    """Where the shipped plugins live, as a real directory on disk.

    Not `importlib.resources`: the loader reads by path and Theater never runs from a zipimport.
    """
    return Path(__file__).resolve().parent / "plugins"
