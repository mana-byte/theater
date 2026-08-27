"""Compatibility facade over the package-manifest loader.

Plugins are named directories containing ``manifest.py``, loaded by
:mod:`theater.harness.loading`. This module re-exports the public names so
existing imports continue to work. No legacy single-file execution remains.
"""

from __future__ import annotations

from theater.harness.loading import LOCAL, SHIPPED, LoadedPlugin, PluginError, scan

__all__ = ["LOCAL", "SHIPPED", "LoadedPlugin", "PluginError", "scan"]
