"""Harness contract, compiler, loading, channel and MCP-policy modules import no runtime layer."""

from __future__ import annotations

import ast

import pytest

from tests.support.paths import REPO_ROOT

_RUNTIME_LAYERS = (
    "theater.daemon",
    "theater.regie",
    "theater.tmux",
    "theater.harness.builtin",
)

_MODULES = {
    "manifests": (
        "theater/harness/contracts/manifest.py",
        "theater/harness/contracts/callbacks.py",
        "theater/harness/contracts/channels.py",
        "theater/harness/manifests/compiler.py",
        "theater/harness/manifests/validation.py",
        "theater/harness/manifests/strategies.py",
    ),
    "loading": (
        "theater/harness/loading/models.py",
        "theater/harness/loading/discovery.py",
        "theater/harness/loading/importer.py",
        "theater/harness/loading/__init__.py",
    ),
    "composite": (
        "theater/harness/channels/__init__.py",
        "theater/harness/channels/composite.py",
        "theater/harness/channels/health.py",
    ),
    "mcp_policy": (
        "theater/harness/registry/mcp.py",
        "theater/mcp/server.py",
    ),
}


def _imported(path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    imported.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    return imported


@pytest.mark.parametrize("group", sorted(_MODULES))
def test_modules_do_not_import_runtime_layers(group: str) -> None:
    for relative in _MODULES[group]:
        path = REPO_ROOT / relative
        assert not any(name.startswith(_RUNTIME_LAYERS) for name in _imported(path)), path


def test_runtime_contracts_do_not_import_daemon_internals() -> None:
    path = REPO_ROOT / "theater/harness/contracts/runtime.py"
    assert not any(name.startswith("theater.daemon") for name in _imported(path)), path
