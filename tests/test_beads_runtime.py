"""Real Beads v1.3.1 workflow proof for the play adapter."""

from __future__ import annotations

import asyncio
import contextvars
import os
import threading
from pathlib import Path
from typing import ClassVar

import pytest

from theater.mcp_plugins.builtin.beads import retention, service
from theater.mcp_plugins.builtin.beads.backend import BeadsError, Board
from theater.mcp_plugins.builtin.beads.retention import BoardRetention
from theater.mcp_plugins.builtin.beads.service import BeadsService, board_lock

_caller = contextvars.ContextVar("beads_test_caller", default="author")
_instance = contextvars.ContextVar("beads_test_instance", default="instance-one")


def test_npm_launcher_preserves_node_path_and_scrubs_routing(tmp_path: Path, monkeypatch) -> None:
    executable_dir = tmp_path / "bin"
    executable_dir.mkdir()
    node = executable_dir / "node"
    node.write_text('#!/bin/sh\nprintf "bd version 1.3.1\\n"\n')
    node.chmod(0o755)
    launcher = executable_dir / "bd"
    launcher.write_text('#!/usr/bin/env node\nconsole.log("bd version 1.3.1");\n')
    launcher.chmod(0o755)
    monkeypatch.setenv("PATH", str(executable_dir))
    monkeypatch.setenv("BEADS_DIR", "/other/board")
    monkeypatch.setenv("BEADS_DOLT_SERVER_HOST", "external")
    board = Board(tmp_path / "state", "instance-one", str(launcher))
    board.path.mkdir(parents=True)
    assert board._run("version", json_output=False) == "bd version 1.3.1"
    assert board._env()["BEADS_DIR"] == str(board.path / ".beads")
    assert "BEADS_DOLT_SERVER_HOST" not in board._env()


class FakeClient:
    enabled = True
    permissions: ClassVar[list[str]] = ["read", "write"]
    members: ClassVar[list[dict[str, str]]] = [
        {"participant_id": "author", "name": "Author", "position": "master"},
        {"participant_id": "peer", "name": "Peer", "position": "member"},
    ]
    live: ClassVar[dict[str, bool]] = {"instance-one": True, "instance-two": True}
    unavailable = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def play_context(self):
        return {
            "participant_id": _caller.get(),
            "plugin_name": "beads",
            "enabled": self.enabled,
            "play": {
                "instance_id": _instance.get(),
                "master_id": "author",
                "name": "hierarchy",
                "position": "master",
                "plugins": ["beads"],
                "permissions": self.permissions,
            },
        }

    async def play_members(self):
        return self.members

    async def play_instances_status(self, instance_ids):
        if self.unavailable:
            raise OSError("daemon unavailable")
        return [{"instance_id": key, "active": self.live.get(key, False)} for key in instance_ids]


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
        assert await asyncio.wait_for(contender, 5)

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


@pytest.mark.integration
def test_real_beads_board_workflow_and_retention(tmp_path: Path, monkeypatch) -> None:  # noqa: PLR0915
    binary = os.environ.get("THEATER_BEADS_TEST_BINARY")
    if not binary:
        pytest.skip("set THEATER_BEADS_TEST_BINARY to checksum-verified bd v1.3.1")
    monkeypatch.setattr(service, "TheaterPluginClient", FakeClient)
    monkeypatch.setattr(retention, "TheaterPluginClient", FakeClient)
    FakeClient.enabled = True
    FakeClient.permissions = ["read", "write"]
    FakeClient.members = [
        {"participant_id": "author", "name": "Author", "position": "master"},
        {"participant_id": "peer", "name": "Peer", "position": "member"},
    ]
    FakeClient.live = {"instance-one": True, "instance-two": True}
    FakeClient.unavailable = False
    adapter = BeadsService(tmp_path, bd_path=binary)
    native_command = Board.command

    def checked_command(board, *args, actor):
        if "--force" in args:
            assert len(args) == 5
            assert args[0] == "update" and args[2] == "--assignee" and args[4] == "--force"
        return native_command(board, *args, actor=actor)

    monkeypatch.setattr(Board, "command", checked_command)

    async def exercise():  # noqa: PLR0915
        root = await adapter.create_task("--root", kind="epic")
        root_id = root["id"]
        with pytest.raises(BeadsError, match="cannot close"):
            await adapter.update_task(root_id, status="closed")
        child = await adapter.create_task("Implement", parent_id=root_id, assignee="Peer")
        child_id = child["id"]
        assert child["assignee"] == "peer"
        assert child["parent_id"] == root_id
        assert child_id in (await adapter.get_task(root_id))["blockers"]
        with pytest.raises(BeadsError):
            await adapter.close_task(root_id, "premature")
        with pytest.raises(BeadsError, match="assigned to another"):
            await adapter.claim_task(child_id)
        token = _caller.set("peer")
        try:
            assert (await adapter.claim_task(child_id))["assignee"] == "peer"
        finally:
            _caller.reset(token)
        FakeClient.members = [FakeClient.members[0]]
        assert (await adapter.get_task(child_id))["assignee"] == "peer"
        with pytest.raises(BeadsError, match="not ready"):
            await adapter.claim_task(child_id)
        await adapter.update_task(child_id, assignee="author", status="open")
        assert (await adapter.claim_task(child_id))["assignee"] == "author"
        await adapter.close_task(child_id, "failed; work handed back")
        await adapter.close_task(root_id, "milestone accepted")
        with pytest.raises(BeadsError, match="reopen closed parents"):
            await adapter.update_task(child_id, status="blocked")
        await adapter.update_task(root_id, status="open")
        reopened = await adapter.update_task(child_id, status="blocked")
        assert reopened["outcome"] is None
        assert reopened["outcome_history"] == ["failed; work handed back"]
        raw = Board(tmp_path, "instance-one", binary).rows()[child_id]
        assert "theater_outcome" not in raw["metadata"]
        assert (
            root_id in (await adapter.get_task(child_id))["deps"] or child["parent_id"] == root_id
        )

        milestone = await adapter.create_task("Review milestone", kind="milestone")
        milestone_child = await adapter.create_task("Review", parent_id=milestone["id"])
        assert milestone_child["id"] in (await adapter.get_task(milestone["id"]))["blockers"]
        with pytest.raises(BeadsError):
            await adapter.close_task(milestone["id"], "premature")

        prereq = await adapter.create_task("Prerequisite")
        dependent = await adapter.create_task("Dependent", dependencies=[prereq["id"]])
        assert prereq["id"] in dependent["blockers"]
        await adapter.close_task(prereq["id"], "cancelled")
        assert prereq["id"] not in (await adapter.get_task(dependent["id"]))["blockers"]
        await adapter.close_task(dependent["id"], "done")
        with pytest.raises(BeadsError, match="dependent"):
            await adapter.update_task(prereq["id"], status="in_progress")
        await adapter.update_task(dependent["id"], status="open")
        await adapter.update_task(prereq["id"], status="open")

        candidate = await adapter.create_task("Claimable")

        async def claim_as(participant_id):
            token = _caller.set(participant_id)
            try:
                return await adapter.claim_task(candidate["id"])
            finally:
                _caller.reset(token)

        results = await asyncio.gather(claim_as("author"), claim_as("peer"), return_exceptions=True)
        assert sum(isinstance(value, dict) for value in results) == 1
        assert sum(isinstance(value, BeadsError) for value in results) == 1

        first_page = await adapter.list_tasks(limit=1)
        assert first_page["next_cursor"]
        second_page = await adapter.list_tasks(cursor=first_page["next_cursor"], limit=1)
        assert first_page["tasks"][0]["id"] != second_page["tasks"][0]["id"]

        token = _instance.set("instance-two")
        try:
            with pytest.raises(BeadsError, match="this play"):
                await adapter.get_task(root_id)
            other = await adapter.create_task("Other play")
            assert other["play_instance_id"] == "instance-two"
        finally:
            _instance.reset(token)

        cleaner = BoardRetention(tmp_path, 0)
        FakeClient.live["instance-two"] = False
        FakeClient.unavailable = True
        await cleaner.sweep()
        assert (tmp_path / "plays" / "instance-two").exists()
        FakeClient.unavailable = False
        await cleaner.sweep()
        assert not (tmp_path / "plays" / "instance-two").exists()
        assert (tmp_path / "plays" / "instance-one").exists()

    asyncio.run(exercise())
