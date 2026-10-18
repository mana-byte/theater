"""Régie's boundary with Theater: public imports only, the client pool and the local catalog."""

from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest
from regie.client_pool import FrontendClientPool

from theater.frontend import FrontendClient


@pytest.mark.asyncio
async def test_real_client_pool_clones_every_concurrent_lane_and_each_action() -> None:
    root = FrontendClient(
        "/tmp/theater.sock",
        client_id="regie-test",
        required_capabilities=("state.v1",),
        request_timeout=7.5,
    )
    pool = FrontendClientPool(root)

    named = (
        pool.state,
        pool.catalog,
        pool.usage,
        pool.bus,
        pool.animation_bus,
        pool.trajectory_query,
        pool.trajectory_follow,
        pool.transcripts,
        pool.controls,
        pool.resume,
    )
    first_action = pool.action_client()
    second_action = pool.action_client()
    first_transcript = pool.transcript_client()
    second_transcript = pool.transcript_client()

    isolated = (*named, first_action, second_action, first_transcript, second_transcript)
    assert len({id(client) for client in (*isolated, root)}) == 15
    assert len({client.config.client_id for client in isolated}) == 14
    assert first_action.config.client_id == "regie-test:action-1"
    assert second_action.config.client_id == "regie-test:action-2"
    assert first_transcript.config.client_id == "regie-test:transcript-1"
    assert second_transcript.config.client_id == "regie-test:transcript-2"
    for client in isolated:
        assert client.config.socket_path == root.config.socket_path
        assert client.config.role == root.config.role
        assert client.config.channel == root.config.channel
        assert client.config.required_capabilities == root.config.required_capabilities
    assert {client.config.client_id: client.config.request_timeout for client in isolated} == {
        "regie-test:state": 35.0,
        "regie-test:catalog": 7.5,
        "regie-test:usage": 7.5,
        "regie-test:bus": 35.0,
        "regie-test:animation_bus": 35.0,
        "regie-test:trajectory_query": 7.5,
        "regie-test:trajectory_follow": 35.0,
        "regie-test:transcripts": 35.0,
        "regie-test:controls": 7.5,
        "regie-test:resume": 7.5,
        "regie-test:action-1": 35.0,
        "regie-test:action-2": 35.0,
        "regie-test:transcript-1": 7.5,
        "regie-test:transcript-2": 7.5,
    }

    await first_action.close()
    await second_action.close()
    await first_transcript.close()
    await second_transcript.close()
    await pool.close()


def test_local_catalog_adapts_the_installed_registry_without_daemon_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = importlib.import_module("theater.frontend.local_catalog")
    discovered: list[bool] = []
    monkeypatch.setattr(
        catalog,
        "_configured_registry_rows",
        lambda: (
            discovered.append(True)
            or [
                {
                    "name": "claude",
                    "icon": "◆",
                    "binary": "claude",
                    "binaries": ["claude-code"],
                    "installed": True,
                    "approvals": ["manual", "edits"],
                    "error": None,
                },
                {
                    "name": "missing",
                    "icon": "?",
                    "binary": "missing",
                    "binaries": [],
                    "installed": False,
                    "approvals": ["manual"],
                    "error": None,
                },
            ]
        ),
    )

    available, missing = catalog.local_harness_catalog()

    assert discovered == [True]
    assert (available.name, available.icon, available.launch_available) == (
        "claude",
        "◆",
        True,
    )
    assert available.approvals == ("manual", "edits")
    assert missing.launch_available is False
    assert missing.reason == "not_installed"


SOURCE_ROOT = Path(__file__).parents[1] / "src" / "regie"


def test_regie_production_imports_only_the_public_frontend_surface() -> None:
    violations: list[str] = []
    source_text: list[str] = []
    for path in SOURCE_ROOT.rglob("*.py"):
        text = path.read_text()
        source_text.append(text)
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                names = [node.module]
            else:
                continue
            for name in names:
                if _private_theater_import(name):
                    violations.append(f"{path.relative_to(SOURCE_ROOT)}: {name}")
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            if not _is_dynamic_import(node.func):
                continue
            target = node.args[0]
            if (
                isinstance(target, ast.Constant)
                and isinstance(target.value, str)
                and _private_theater_import(target.value)
            ):
                violations.append(f"{path.relative_to(SOURCE_ROOT)}: {target.value}")

    assert violations == []
    assert "frontend.participants.tree" not in "\n".join(source_text)


def _private_theater_import(name: str) -> bool:
    return name == "theater" or (
        name.startswith("theater.") and not name.startswith("theater.frontend")
    )


def _is_dynamic_import(function: ast.expr) -> bool:
    return (isinstance(function, ast.Name) and function.id in {"__import__", "import_module"}) or (
        isinstance(function, ast.Attribute)
        and function.attr == "import_module"
        and isinstance(function.value, ast.Name)
        and function.value.id == "importlib"
    )
