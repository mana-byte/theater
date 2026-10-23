"""The Beads plugin: manifest and static tools, launcher environment, gate and real board."""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from tests.support.beads_fakes import FakeClient, fact
from tests.support.beads_fakes import caller as _caller
from tests.support.beads_fakes import instance as _instance
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
        pytest.skip("set THEATER_BEADS_TEST_BINARY to a checksum-verified bd binary")
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
            await adapter.update_task(root_id, status="closed")  # type: ignore[arg-type]
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
        await adapter.close_task(child_id, "failed; work handed back", "failed")
        closed_child = Board(tmp_path, "instance-one", binary).rows()[child_id]
        assert closed_child["metadata"]["theater_outcome_status"] == "failed"
        await adapter.close_task(root_id, "milestone accepted")
        with pytest.raises(BeadsError, match="reopen closed parents"):
            await adapter.update_task(child_id, status="blocked")
        await adapter.update_task(root_id, status="open")
        reopened = await adapter.update_task(child_id, status="blocked")
        assert reopened["outcome"] is None
        assert reopened["outcome_status"] is None
        assert reopened["outcome_history"] == ["failed; work handed back"]
        assert reopened["outcome_status_history"] == ["failed"]
        raw = Board(tmp_path, "instance-one", binary).rows()[child_id]
        assert "theater_outcome" not in raw["metadata"]
        assert "theater_outcome_status" not in raw["metadata"]
        assert json.loads(raw["metadata"]["theater_outcome_history"]) == [
            {"outcome": "failed; work handed back", "status": "failed"}
        ]
        assert (
            root_id in (await adapter.get_task(child_id))["deps"] or child["parent_id"] == root_id
        )

        work_handle = "tb-xzg-work-job-1"
        review_handle = "tb-xzg-review-job-1"
        now = time.time()
        FakeClient.facts = {
            work_handle: fact(work_handle, created_at=now + 100, finished_at=now + 200),
            review_handle: fact(
                review_handle,
                created_at=now + 300,
                contract_preset="review",
                review_verdict="approve",
                target_id="target-two",
            ),
        }
        FakeClient.facts_calls = 0
        linked = await adapter.link_task_job(child_id, work_handle, "work")
        assert linked == {
            "play_instance_id": "instance-one",
            "task_id": child_id,
            "handle": work_handle,
            "role": "work",
            "review_of": None,
            "generation": 1,
            "duplicate": False,
            "links_total": 1,
        }
        with pytest.raises(BeadsError, match="review_of must be an exact job handle"):
            await adapter.link_task_job(child_id, review_handle, "review")
        with pytest.raises(BeadsError, match="review_of must name a work job"):
            await adapter.link_task_job(child_id, review_handle, "review", "tb-xzg-unlinked-work")
        with pytest.raises(BeadsError, match="work link cannot name"):
            await adapter.link_task_job(child_id, work_handle, "work", work_handle)
        reviewed = await adapter.link_task_job(child_id, review_handle, "review", work_handle)
        assert reviewed["duplicate"] is False and reviewed["links_total"] == 2
        replay = await adapter.link_task_job(child_id, review_handle, "review", work_handle)
        assert replay["duplicate"] is True and replay["generation"] == 1
        with pytest.raises(BeadsError, match="changing a stored link"):
            await adapter.link_task_job(child_id, work_handle, "review", work_handle)
        assert FakeClient.facts_calls == 2
        progress = await adapter.get_task(child_id, view="progress")
        assert progress["generation"] == 1
        assert progress["current_work"]["handle"] == work_handle
        assert progress["latest_review"]["review_of"] == work_handle
        assert progress["latest_review"]["source_basis"] == "declared"
        assert progress["latest_review"]["artifact_relation"] == "matched"
        assert [entry["handle"] for entry in progress["job_links"]] == [
            work_handle,
            review_handle,
        ]
        assert FakeClient.facts_calls == 3
        detail = await adapter.get_task(child_id)
        assert "job_links" not in detail
        assert FakeClient.facts_calls == 3
        with pytest.raises(BeadsError, match="view must be"):
            await adapter.get_task(child_id, view="brief")  # type: ignore[arg-type]

        await adapter.close_task(child_id, "second outcome; links retained")
        closed_at = time.time()
        FakeClient.facts = {
            "tb-xzg-repair-job": fact("tb-xzg-repair-job", created_at=closed_at + 100)
        }
        FakeClient.facts_calls = 0
        with pytest.raises(BeadsError, match="after this task closed"):
            await adapter.link_task_job(child_id, "tb-xzg-repair-job", "work")
        assert FakeClient.facts_calls == 1
        reopened2 = await adapter.update_task(child_id, status="open")
        assert reopened2["outcome"] is None
        assert reopened2["outcome_history"] == [
            "failed; work handed back",
            "second outcome; links retained",
        ]
        assert reopened2["outcome_status_history"] == ["failed", None]
        raw = Board(tmp_path, "instance-one", binary).rows()[child_id]
        assert raw["metadata"]["theater_generation"] == 2
        assert isinstance(raw["metadata"]["theater_boundary"], float)
        assert "theater_closed_at" not in raw["metadata"]
        assert "theater_outcome_status" not in raw["metadata"]
        assert json.loads(raw["metadata"]["theater_outcome_history"]) == [
            {"outcome": "failed; work handed back", "status": "failed"},
            "second outcome; links retained",
        ]
        assert len(raw["metadata"]["theater_links"]) == 2
        assert raw["metadata"]["theater_links"][0]["handle"] == work_handle
        repair = await adapter.link_task_job(child_id, "tb-xzg-repair-job", "work")
        assert repair["generation"] == 2 and repair["links_total"] == 3
        FakeClient.facts = {}
        FakeClient.facts_calls = 0
        replay2 = await adapter.link_task_job(child_id, "tb-xzg-repair-job", "work")
        assert replay2["duplicate"] is True and replay2["links_total"] == 3
        assert FakeClient.facts_calls == 0

        # a stored link with an unknown version fails closed through real bd too
        corrupt = await adapter.create_task("Corrupt links")
        Board(tmp_path, "instance-one", binary).command(
            "update",
            corrupt["id"],
            "--metadata",
            json.dumps(
                {
                    "theater_links": [
                        {
                            "version": 999,
                            "handle": "tb-xzg-legacy-job",
                            "role": "work",
                            "review_of": None,
                            "generation": 0,
                            "created_at": 123.0,
                        }
                    ]
                }
            ),
            actor="author",
        )
        with pytest.raises(BeadsError, match="unsupported or corrupt"):
            await adapter.get_task(corrupt["id"], view="progress")
        FakeClient.facts = {"tb-xzg-legacy-job": fact("tb-xzg-legacy-job")}
        FakeClient.facts_calls = 0
        with pytest.raises(BeadsError, match="unsupported or corrupt"):
            await adapter.link_task_job(corrupt["id"], "tb-xzg-legacy-job", "work")
        assert FakeClient.facts_calls == 0  # refused before any facts read
        raw = Board(tmp_path, "instance-one", binary).rows()[corrupt["id"]]
        assert len(raw["metadata"]["theater_links"]) == 1  # never rewritten or dropped

        milestone = await adapter.create_task("Review milestone", kind="milestone")
        milestone_child = await adapter.create_task("Review", parent_id=milestone["id"])
        assert milestone_child["id"] in (await adapter.get_task(milestone["id"]))["blockers"]
        with pytest.raises(BeadsError):
            await adapter.close_task(milestone["id"], "premature")

        prereq = await adapter.create_task("Prerequisite")
        dependent = await adapter.create_task("Dependent", dependencies=[prereq["id"]])
        assert prereq["id"] in dependent["blockers"]
        await adapter.close_task(prereq["id"], "cancelled", "cancelled")
        assert prereq["id"] not in (await adapter.get_task(dependent["id"]))["blockers"]
        waiting = await adapter.get_task(dependent["id"])
        assert waiting["failed_prerequisites"] == [
            {"id": prereq["id"], "outcome_status": "cancelled"}
        ]
        progress_waiting = await adapter.get_task(dependent["id"], view="progress")
        assert progress_waiting["failed_prerequisites"] == [
            {"id": prereq["id"], "outcome_status": "cancelled"}
        ]
        closed_rows = await adapter.list_tasks(status="closed")
        assert [row["outcome_status"] for row in closed_rows["tasks"]] == ["cancelled"]
        await adapter.close_task(dependent["id"], "done", "succeeded")
        with pytest.raises(BeadsError, match="dependent"):
            await adapter.update_task(prereq["id"], status="in_progress")
        await adapter.update_task(dependent["id"], status="open")
        await adapter.update_task(prereq["id"], status="open")
        raw_dependent = Board(tmp_path, "instance-one", binary).rows()[dependent["id"]]
        assert json.loads(raw_dependent["metadata"]["theater_outcome_history"]) == [
            {"outcome": "done", "status": "succeeded"}
        ]

        # a stale status set on the open row by raw bd must not survive an untyped close
        stale = await adapter.create_task("Stale status")
        Board(tmp_path, "instance-one", binary).command(
            "update",
            stale["id"],
            "--set-metadata",
            "theater_outcome_status=succeeded",
            actor="author",
        )
        untyped = await adapter.close_task(stale["id"], "closed with no type")
        assert untyped["outcome_status"] is None
        raw_stale = Board(tmp_path, "instance-one", binary).rows()[stale["id"]]
        assert raw_stale["metadata"]["theater_outcome_status"] is None

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
