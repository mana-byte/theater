"""Shipped package-manifest harnesses loaded through the public plugin path."""

from __future__ import annotations

from pathlib import Path


def plugin_dir() -> Path:
    """Where the shipped plugins live, as a real directory on disk.

    A plain filesystem path, not `importlib.resources`: the loader reads files
    by path, and Theater is installed from source or as a wheel that unpacks to
    a directory, never from a zipimport. If that ever changes this is the one
    function that has to learn about it.
    """
    return Path(__file__).resolve().parent / "plugins"
