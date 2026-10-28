"""The Beads plugin: manifest and static tools, launcher environment, gate and real board."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from tests.support.beads_fakes import FakeClient
from tests.support.waiting import until
from theater.config import Config
from theater.mcp_plugins import McpLaunchContext, PluginCapability, validate_manifest
from theater.mcp_plugins import registry as mcp_registry
from theater.mcp_plugins.builtin.beads import retention, service
from theater.mcp_plugins.builtin.beads.backend import BeadsError, Board
from theater.mcp_plugins.builtin.beads.manifest import MANIFEST, plan
from theater.mcp_plugins.builtin.beads.retention import BoardRetention
from theater.mcp_plugins.builtin.beads.server import build
from theater.mcp_plugins.builtin.beads.service import BeadsService, board_lock
from theater.skills import discover


def test_beads_manifest_and_static_tools(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", "")
    validate_manifest("beads", MANIFEST)
    assert MANIFEST.capabilities == frozenset(
        {PluginCapability.PLAYS_READ, PluginCapability.JOBS_READ}
    )
    assert MANIFEST.plays == ("hierarchy",)
    assert MANIFEST.play_permissions == frozenset({"read", "write"})
    launch = plan(
        McpLaunchContext(
            participant_id="caller",
            cwd=tmp_path,
            state_dir=tmp_path / "state",
            config={"board_ttl_seconds": 0, "bd_path": "/tmp/bd"},
        )
    )
    assert launch.argv[:2] == ("-m", "theater.mcp_plugins.builtin.beads.server")
    assert launch.argv[-2:] == ("--bd-path", "/tmp/bd")
    tools = asyncio.run(build(tmp_path).list_tools())
    assert {tool.name for tool in tools} == {
        "list_tasks",
        "get_task",
        "create_task",
        "update_task",
        "set_dependency",
        "claim_task",
        "close_task",
        "link_task_job",
    }
    for tool in tools:
        assert not {"participant_id", "play_instance_id", "board_dir", "database"} & set(
            tool.input_schema["properties"]
        )
    close = next(tool for tool in tools if tool.name == "close_task")
    assert close.input_schema["properties"]["outcome_status"]["anyOf"][0]["enum"] == [
        "succeeded",
        "failed",
        "cancelled",
    ]
    assert "outcome_status" not in close.input_schema["required"]
    assert not (tmp_path / "plays").exists()


def test_shipped_hierarchy_defaults_and_both_opt_outs(tmp_path: Path) -> None:
    settings = Config()
    mcp_registry.install(settings, local_dir=tmp_path / "plugins")
    snapshot = mcp_registry.catalog()
    registry = discover(user_dir=tmp_path / "skills", plugin_plays=snapshot.registered_plays)
    play = registry.play("hierarchy")
    assert play is not None and play.provider == "beads"
    assert registry.load("hierarchy/master").provider == "beads"
    assert play.rules.plugin_permissions["beads"]["member"] == (
        "read",
        "write",
    )
    refused = discover(
        user_dir=tmp_path / "skills",
        plugin_plays=snapshot.registered_plays,
        disabled=("hierarchy",),
    )
    assert refused.play("hierarchy") is None
    assert all(skill.play != "hierarchy" for skill in refused.skills)
    mcp_registry.install(
        replace(settings, mcp=replace(settings.mcp, enabled=[])), local_dir=tmp_path / "plugins"
    )
    assert not mcp_registry.catalog().registered_plays


def test_npm_launcher_preserves_node_path_and_scrubs_routing(tmp_path: Path, monkeypatch) -> None:
    executable_dir = tmp_path / "bin"
    executable_dir.mkdir()
    node = executable_dir / "node"
    node.write_text('#!/bin/sh\nprintf "bd version test\\n"\n')
    node.chmod(0o755)
    launcher = executable_dir / "bd"
    launcher.write_text('#!/usr/bin/env node\nconsole.log("bd version test");\n')
    launcher.chmod(0o755)
    monkeypatch.setenv("PATH", str(executable_dir))
    monkeypatch.setenv("BEADS_DIR", "/other/board")
    monkeypatch.setenv("BEADS_DOLT_SERVER_HOST", "external")
    board = Board(tmp_path / "state", "instance-one", str(launcher))
    board.path.mkdir(parents=True)
    assert board._run("version", json_output=False) == "bd version test"
    assert board._env()["BEADS_DIR"] == str(board.path / ".beads")
    assert "BEADS_DOLT_SERVER_HOST" not in board._env()


def test_permission_gate_precedes_backend(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(service, "TheaterPluginClient", FakeClient)
    FakeClient.enabled = False
    adapter = BeadsService(tmp_path)
    with pytest.raises(BeadsError, match="not enabled"):
        asyncio.run(adapter.list_tasks())
    assert not (tmp_path / "plays").exists()
    FakeClient.enabled = True
    FakeClient.permissions = ["read"]
    with pytest.raises(BeadsError, match="write grant"):
        asyncio.run(adapter.create_task("No write"))
    assert not (tmp_path / "plays").exists()
    FakeClient.permissions = ["read", "write"]


def test_permission_is_rechecked_after_board_lock(tmp_path: Path, monkeypatch) -> None:
    FakeClient.reset()
    monkeypatch.setattr(service, "TheaterPluginClient", FakeClient)
    original = FakeClient.play_context
    calls = 0

    async def changed_context(client):
        nonlocal calls
        result = await original(client)
        calls += 1
        if calls == 2:
            result["play"]["permissions"] = ["read"]
        return result

    monkeypatch.setattr(FakeClient, "play_context", changed_context)
    monkeypatch.setattr(Board, "ensure", lambda _board: pytest.fail("backend was reached"))

    with pytest.raises(BeadsError, match="no Beads write grant"):
        asyncio.run(BeadsService(tmp_path).create_task("Permission changed"))
    assert calls == 2
    assert not (tmp_path / "plays").exists()


def test_cancelled_backend_keeps_board_lock_until_worker_finishes(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(service, "TheaterPluginClient", FakeClient)
    monkeypatch.setattr(Board, "ensure", lambda _board: None)
    started = threading.Event()
    release = threading.Event()
    adapter = BeadsService(tmp_path)

    def blocking(_board, _actor, _members):
        started.set()
        assert release.wait(5)
        return {}

    async def exercise():
        task = asyncio.create_task(adapter._operate("read", blocking))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()

        async def take_lock():
            async with board_lock(tmp_path, "instance-one"):
                return True

        contender = asyncio.create_task(take_lock())
        await asyncio.sleep(0.1)
        assert not contender.done()
        task.cancel()
        await asyncio.sleep(0.1)
        assert not contender.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await until(contender.done)
        assert await contender

    asyncio.run(exercise())


def test_retention_retries_after_client_credential_failure(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "plays" / "instance-one").mkdir(parents=True)
    attempts = 0
    ticks = 0

    class StopProbe(Exception):
        pass

    def failed_client():
        nonlocal attempts
        attempts += 1
        raise OSError("credential unavailable")

    async def fast_sleep(_seconds):
        nonlocal ticks
        ticks += 1
        if ticks == 2:
            raise StopProbe

    monkeypatch.setattr(retention, "TheaterPluginClient", failed_client)
    monkeypatch.setattr(retention.asyncio, "sleep", fast_sleep)
    with pytest.raises(StopProbe):
        asyncio.run(BoardRetention(tmp_path, 0).run())
    assert attempts == 2
