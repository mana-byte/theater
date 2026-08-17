from __future__ import annotations

import json
from pathlib import Path

import pytest

from theater.client import DaemonClient
from theater.daemon.server import Daemon
from theater.daemon.store import Store
from theater.harness.builtin.plugins.claude import ClaudeCodeHarness, ClaudeCodeObserver
from theater.harness.source import TranscriptSource
from theater.protocol import RemoteError


def _transcript(root: Path, session_id: str, cwd: Path, *, project: str = "project") -> Path:
    path = root / project / f"{session_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "sessionId": session_id,
                "session_id": session_id,
                "cwd": str(cwd),
                "message": {"content": [], "stop_reason": "end_turn"},
            }
        )
        + "\n"
    )
    return path


def _spawn_claude(daemon, cwd: Path, *, pid: str, token: str = "tok"):
    participant = daemon.registry.create_spawned(harness="claude", cwd=str(cwd), pid=pid)
    daemon.store.set_receipt_token(participant.id, token)
    return participant


@pytest.fixture
async def claude_daemon(theater_home, tmp_path):
    root = tmp_path / "claude" / "projects"
    harness = ClaudeCodeHarness(root=root)
    d = Daemon(harnesses={"claude": harness})
    await d.start()
    yield d, root
    await d.aclose()


@pytest.fixture
async def claude_client(claude_daemon):
    c = DaemonClient(autostart=False)
    await c.connect()
    yield c
    await c.aclose()


async def test_claude_initial_receipt_records_exact_location(
    claude_daemon, claude_client, tmp_path
):
    daemon, root = claude_daemon
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _spawn_claude(daemon, cwd, pid="p-claude", token="secret")
    path = _transcript(root, "11111111-1111-4111-8111-111111111111", cwd)

    await claude_client.call(
        "claude.receipt",
        id="p-claude",
        token="secret",
        session_id=path.stem,
        transcript_path=str(path),
    )

    got = daemon.store.get_participant("p-claude")
    assert got.session_id == path.stem
    assert got.session_correlation == "exact"
    assert got.transcript_location == str(path.resolve())
    assert got.transcript_domain == str(root.resolve())


async def test_claude_receipt_updates_after_compaction_rotation(
    claude_daemon, claude_client, tmp_path
):
    daemon, root = claude_daemon
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _spawn_claude(daemon, cwd, pid="p-claude", token="secret")
    first = _transcript(root, "11111111-1111-4111-8111-111111111111", cwd)
    second = _transcript(root, "22222222-2222-4222-8222-222222222222", cwd)

    for path in (first, second):
        await claude_client.call(
            "claude.receipt",
            id="p-claude",
            token="secret",
            session_id=path.stem,
            transcript_path=str(path),
        )

    got = daemon.store.get_participant("p-claude")
    assert got.session_id == second.stem
    assert got.transcript_location == str(second.resolve())


async def test_claude_duplicate_receipt_is_idempotent(claude_daemon, claude_client, tmp_path):
    daemon, root = claude_daemon
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _spawn_claude(daemon, cwd, pid="p-claude", token="secret")
    path = _transcript(root, "11111111-1111-4111-8111-111111111111", cwd)

    for _ in range(2):
        await claude_client.call(
            "claude.receipt",
            id="p-claude",
            token="secret",
            session_id=path.stem,
            transcript_path=str(path),
        )

    got = daemon.store.get_participant("p-claude")
    assert got.session_id == path.stem
    assert got.transcript_location == str(path.resolve())


async def test_claude_receipt_rejects_invalid_token_path_and_harness(
    claude_daemon, claude_client, tmp_path
):
    daemon, root = claude_daemon
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _spawn_claude(daemon, cwd, pid="p-claude", token="secret")
    other = daemon.registry.create_spawned(harness="vibe", cwd=str(cwd), pid="p-vibe")
    daemon.store.set_receipt_token(other.id, "secret")
    path = _transcript(root, "11111111-1111-4111-8111-111111111111", cwd)

    with pytest.raises(RemoteError, match="token"):
        await claude_client.call(
            "claude.receipt",
            id="p-claude",
            token="wrong",
            session_id=path.stem,
            transcript_path=str(path),
        )
    with pytest.raises(RemoteError, match="match"):
        await claude_client.call(
            "claude.receipt",
            id="p-claude",
            token="secret",
            session_id="22222222-2222-4222-8222-222222222222",
            transcript_path=str(path),
        )
    with pytest.raises(RemoteError, match="Claude participant"):
        await claude_client.call(
            "claude.receipt",
            id="p-vibe",
            token="secret",
            session_id=path.stem,
            transcript_path=str(path),
        )


async def test_claude_receipt_rejects_out_of_domain_path(claude_daemon, claude_client, tmp_path):
    daemon, _root = claude_daemon
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _spawn_claude(daemon, cwd, pid="p-claude", token="secret")
    outside = tmp_path / "elsewhere" / "11111111-1111-4111-8111-111111111111.jsonl"
    outside.parent.mkdir()
    outside.write_text("")

    with pytest.raises(RemoteError, match="outside"):
        await claude_client.call(
            "claude.receipt",
            id="p-claude",
            token="secret",
            session_id=outside.stem,
            transcript_path=str(outside),
        )


async def test_claude_receipt_rejects_cross_participant_location(
    claude_daemon, claude_client, tmp_path
):
    daemon, root = claude_daemon
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _spawn_claude(daemon, cwd, pid="first", token="one")
    _spawn_claude(daemon, cwd, pid="second", token="two")
    path = _transcript(root, "11111111-1111-4111-8111-111111111111", cwd)

    await claude_client.call(
        "claude.receipt",
        id="first",
        token="one",
        session_id=path.stem,
        transcript_path=str(path),
    )
    with pytest.raises(RemoteError, match="another participant"):
        await claude_client.call(
            "claude.receipt",
            id="second",
            token="two",
            session_id=path.stem,
            transcript_path=str(path),
        )


def test_claude_receipt_survives_store_reopen(theater_home, tmp_path):
    from theater import paths
    from theater.daemon.registry import Registry

    store = Store(paths.db_path())
    cwd = tmp_path / "repo"
    cwd.mkdir()
    path = tmp_path / "claude" / "project" / "11111111-1111-4111-8111-111111111111.jsonl"
    path.parent.mkdir(parents=True)
    participant = store.get_participant("p-claude")
    assert participant is None

    registry = Registry(store)
    registry.create_spawned(harness="claude", cwd=str(cwd), pid="p-claude")
    store.set_receipt_token("p-claude", "secret")
    store.record_transcript_receipt(
        "p-claude",
        session_id=path.stem,
        transcript_domain=str(path.parent.parent),
        transcript_location=str(path),
    )
    store.close()

    reopened = Store(store.path)
    try:
        got = reopened.get_participant("p-claude")
        assert got.session_id == path.stem
        assert got.session_correlation == "exact"
        assert got.transcript_location == str(path)
        assert reopened.get_receipt_token("p-claude") == "secret"
    finally:
        reopened.close()


async def test_receipt_nudges_transcript_source_to_reattach(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    root = tmp_path / "claude" / "projects"
    first = _transcript(root, "11111111-1111-4111-8111-111111111111", cwd)
    second = _transcript(root, "22222222-2222-4222-8222-222222222222", cwd)
    source = TranscriptSource(
        ClaudeCodeObserver(root=root),
        cwd=str(cwd),
        session_id=first.stem,
        exact_session=True,
    )

    batch = await source.read()
    assert batch.attached is not None
    assert batch.attached.location == str(first)
    source.commit_attachment()

    source.admit_exact_location(location=str(second), session_id=second.stem)
    batch = await source.read()

    assert batch.attached is not None
    assert batch.attached.location == str(second)
    assert batch.attached.correlation == "exact"
