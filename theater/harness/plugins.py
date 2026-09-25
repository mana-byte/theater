"""Compatibility facade over the package-manifest loader; no legacy single-file execution."""

from __future__ import annotations

from theater.harness.loading import LOCAL, SHIPPED, LoadedPlugin, PluginError, scan

__all__ = ["LOCAL", "SHIPPED", "LoadedPlugin", "PluginError", "scan"]
