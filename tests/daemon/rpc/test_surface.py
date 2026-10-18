"""The daemon's RPC surface: its method set and the basic hello, ping and error replies."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

from theater.protocol import RemoteError


async def test_ping(client):
    assert (await client.call("ping"))["pong"] is True


async def test_hello_then_list(client):
    me = await client.call("hello", harness="vibe", cwd="/tmp")
    assert me["tier"] == "external"
    assert me["addressable"] is False

    rows = await client.call("participants.list")
    assert [r["id"] for r in rows] == [me["id"]]


async def test_hello_is_idempotent_per_claimed_identity(client):
    a = await client.call("hello", id="participant-a", harness="vibe", cwd="/tmp")
    b = await client.call("hello", id="participant-a", harness="vibe", cwd="/tmp")
    assert a["id"] == b["id"]
    assert len(await client.call("participants.list")) == 1


async def test_external_is_not_addressable(client):
    me = await client.call("hello", harness="vibe", cwd="/tmp")
    assert me["tier"] == "external"
    assert me["addressable"] is False


async def test_unknown_method_is_a_structured_error(client):
    with pytest.raises(RemoteError) as exc:
        await client.call("nope")
    assert exc.value.code == "unknown_method"


async def test_get_missing_participant(client):
    with pytest.raises(RemoteError) as exc:
        await client.call("participants.get", id="ghost")
    assert exc.value.code == "not_found"


EXPECTED_RPC_METHODS = {
    "adopt",
    "bus.tail",
    "config.reload",
    "gc",
    "harnesses",
    "harness.event",
    "hello",
    "jobs.await",
    "jobs.cancel",
    "jobs.status",
    "models",
    "participant.adopt",
    "participant.controls",
    "participant.interrupt",
    "participant.kill",
    "participant.queue_followup",
    "participant.rename",
    "participant.settings.update",
    "participant.steer",
    "participant.update",
    "providers.get",
    "providers.list",
    "plugin.call",
    "participant.status",
    "participants.get",
    "participants.list",
    "participants.recent_dead",
    "participants.tree",
    "participants.unmanaged",
    "ping",
    "read_transcript",
    "recall",
    "recall_read",
    "scratchpad.delete",
    "scratchpad.get",
    "scratchpad.write",
    "send",
    "skills.list",
    "skills.load",
    "shutdown",
    "spawn",
    "stats",
    "trajectory.close",
    "trajectory.follow",
    "trajectory.locate",
    "trajectory.search",
    "trajectory.snapshot",
    "transcript.bind",
    "transcript.candidates",
    "transcript.receipt",
    "usage_by_harness",
    "usage_summary",
    "usage_totals",
    "controls.transfer",
    "workspaces.cleanup",
    "workspaces.get",
    "workspaces.list",
}


_PROBE = "import json, theater.daemon.server as server; print(json.dumps(sorted(server.METHODS)))"


def test_the_daemon_exposes_exactly_these_rpc_methods(tmp_path):
    """A cold production import must register every RPC handler."""
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert set(json.loads(result.stdout)) == EXPECTED_RPC_METHODS
