from __future__ import annotations

import asyncio
import io
import json
import stat
import time
from pathlib import Path

import pytest

from theater import cli, paths
from theater.cli.commands import identity as identity_mod
from theater.client import DaemonClient
from theater.daemon.server import Daemon
from theater.daemon.store import Store
from theater.harness.base import Event, EventKind
from theater.harness.builtin.plugins.claude import ClaudeCodeHarness, ClaudeCodeObserver
from theater.harness.source import Batch, TranscriptSource
from theater.protocol import RemoteError
from theater.provenance import TranscriptProvenance


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
    daemon.registry.attach_pane(participant.id, "%1", pane_pid=10001)
    daemon.store.set_receipt_token(participant.id, token)
    return participant


async def _receipt(client, *, pid: str, token: str, session_id: str, transcript_path: str):
    """Call the generic transcript.receipt RPC with a Claude-shaped payload."""
    return await client.call(
        "transcript.receipt",
        id=pid,
        token=token,
        payload={"session_id": session_id, "transcript_path": transcript_path},
    )


async def _until(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


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

    await _receipt(
        client=claude_client,
        pid="p-claude",
        token="secret",
        session_id=path.stem,
        transcript_path=str(path),
    )

    assert await _until(
        lambda: daemon.store.get_participant("p-claude").transcript_location == str(path.resolve())
    )
    got = daemon.store.get_participant("p-claude")
    assert got.session_id == path.stem
    assert got.session_correlation == str(TranscriptProvenance.EXACT)
    assert got.transcript_location == str(path.resolve())
    assert got.transcript_domain is None


async def test_claude_initial_receipt_waits_for_first_message(
    claude_daemon, claude_client, tmp_path
):
    daemon, root = claude_daemon
    daemon.observer.search = 0.01
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _spawn_claude(daemon, cwd, pid="p-claude", token="secret")
    session_id = "11111111-1111-4111-8111-111111111111"
    participant = daemon.store.get_participant("p-claude")
    participant.session_id = session_id
    participant.session_correlation = str(TranscriptProvenance.EXACT)
    daemon.store.upsert_participant(participant)
    path = root / "project" / f"{session_id}.jsonl"

    result = await _receipt(
        client=claude_client,
        pid=participant.id,
        token="secret",
        session_id=session_id,
        transcript_path=str(path),
    )

    assert result["admission"] == "staged"
    assert await _until(lambda: participant.id in daemon.observer._sources)
    await asyncio.sleep(0.1)
    assert not daemon.observer.transcript_identity_lost(participant.id)
    assert daemon.store.get_participant(participant.id).transcript_location is None

    _transcript(root, session_id, cwd)
    assert await _until(
        lambda: (
            daemon.store.get_participant(participant.id).transcript_location == str(path.resolve())
        )
    )
    assert not daemon.observer.transcript_identity_lost(participant.id)


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
        await _receipt(
            client=claude_client,
            pid="p-claude",
            token="secret",
            session_id=path.stem,
            transcript_path=str(path),
        )

    assert await _until(
        lambda: (
            daemon.store.get_participant("p-claude").session_id == second.stem
            and daemon.store.get_participant("p-claude").transcript_location
            == str(second.resolve())
        )
    )


async def test_claude_duplicate_receipt_is_idempotent(claude_daemon, claude_client, tmp_path):
    daemon, root = claude_daemon
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _spawn_claude(daemon, cwd, pid="p-claude", token="secret")
    path = _transcript(root, "11111111-1111-4111-8111-111111111111", cwd)

    for _ in range(2):
        await _receipt(
            client=claude_client,
            pid="p-claude",
            token="secret",
            session_id=path.stem,
            transcript_path=str(path),
        )

    assert await _until(
        lambda: daemon.store.get_participant("p-claude").transcript_location == str(path.resolve())
    )
    got = daemon.store.get_participant("p-claude")
    assert got.session_id == path.stem
    assert got.transcript_location == str(path.resolve())
    assert "p-claude" not in daemon.observer._reset_watch_state


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
        await _receipt(
            client=claude_client,
            pid="p-claude",
            token="wrong",
            session_id=path.stem,
            transcript_path=str(path),
        )
    with pytest.raises(RemoteError, match="match"):
        await _receipt(
            client=claude_client,
            pid="p-claude",
            token="secret",
            session_id="22222222-2222-4222-8222-222222222222",
            transcript_path=str(path),
        )
    # A non-Claude participant's observer is not registered in this daemon's
    # harnesses dict (only claude is injected), so core rejects before
    # reaching the hook.
    with pytest.raises(RemoteError, match="no observer registered"):
        await _receipt(
            client=claude_client,
            pid="p-vibe",
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
        await _receipt(
            client=claude_client,
            pid="p-claude",
            token="secret",
            session_id=outside.stem,
            transcript_path=str(outside),
        )


async def test_claude_receipt_rejects_dead_participant(claude_daemon, claude_client, tmp_path):
    daemon, root = claude_daemon
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _spawn_claude(daemon, cwd, pid="p-claude", token="secret")
    path = _transcript(root, "11111111-1111-4111-8111-111111111111", cwd)
    daemon.registry.mark_dead("p-claude")

    with pytest.raises(RemoteError, match="dead participant"):
        await _receipt(
            client=claude_client,
            pid="p-claude",
            token="secret",
            session_id=path.stem,
            transcript_path=str(path),
        )


async def test_claude_long_idle_live_receipt_accepts_and_renews_legacy_expired_token(
    claude_daemon, claude_client, tmp_path
):
    daemon, root = claude_daemon
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _spawn_claude(daemon, cwd, pid="p-claude", token="old")
    daemon.store.set_meta(
        "receipt_token:p-claude",
        json.dumps({"token": "secret", "token_path": None, "expires_at": time.time() - 8 * 86400}),
    )
    path = _transcript(root, "11111111-1111-4111-8111-111111111111", cwd)

    await _receipt(
        client=claude_client,
        pid="p-claude",
        token="secret",
        session_id=path.stem,
        transcript_path=str(path),
    )

    assert daemon.store.get_receipt_token("p-claude") == "secret"
    payload = json.loads(daemon.store.get_meta("receipt_token:p-claude"))
    assert "expires_at" not in payload


async def test_claude_receipt_rejects_dead_expired_token_and_cleans_it_up(
    claude_daemon, claude_client, tmp_path
):
    daemon, root = claude_daemon
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _spawn_claude(daemon, cwd, pid="p-claude", token="secret")
    token_file = tmp_path / "token"
    token_file.write_text("secret")
    daemon.store.set_meta(
        "receipt_token:p-claude",
        json.dumps(
            {"token": "secret", "token_path": str(token_file), "expires_at": time.time() - 1}
        ),
    )
    daemon.store.set_status("p-claude", "dead")
    path = _transcript(root, "11111111-1111-4111-8111-111111111111", cwd)

    with pytest.raises(RemoteError, match="dead participant"):
        await _receipt(
            client=claude_client,
            pid="p-claude",
            token="secret",
            session_id=path.stem,
            transcript_path=str(path),
        )

    assert daemon.store.get_meta("receipt_token:p-claude") is None
    assert not token_file.exists()


async def test_claude_receipt_rejects_cross_participant_location(
    claude_daemon, claude_client, tmp_path
):
    daemon, root = claude_daemon
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _spawn_claude(daemon, cwd, pid="first", token="one")
    path = _transcript(root, "11111111-1111-4111-8111-111111111111", cwd)

    await _receipt(
        client=claude_client,
        pid="first",
        token="one",
        session_id=path.stem,
        transcript_path=str(path),
    )
    assert await _until(
        lambda: daemon.store.get_participant("first").transcript_location == str(path.resolve())
    )
    _spawn_claude(daemon, cwd, pid="second", token="two")
    with pytest.raises(RemoteError, match="another participant"):
        await _receipt(
            client=claude_client,
            pid="second",
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
        transcript_location=str(path),
    )
    store.close()

    reopened = Store(store.path)
    try:
        got = reopened.get_participant("p-claude")
        assert got.session_id == path.stem
        assert got.session_correlation == str(TranscriptProvenance.EXACT)
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
        session_provenance=TranscriptProvenance.EXACT,
    )

    batch = await source.read()
    assert batch.attached is not None
    assert batch.attached.location == str(first)
    source.commit_attachment()

    assert source.admit_exact_location(location=str(second), session_id=second.stem) == "staged"
    batch = await source.read()

    assert batch.attached is not None
    assert batch.attached.location == str(second)
    assert batch.attached.correlation == str(TranscriptProvenance.EXACT)


async def test_missing_claude_receipt_waits_until_materialized_then_becomes_strict(tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    root = tmp_path / "claude" / "projects"
    root.mkdir(parents=True)
    session_id = "11111111-1111-4111-8111-111111111111"
    path = root / "project" / f"{session_id}.jsonl"
    source = ClaudeCodeObserver(root=root).open_source_for(
        participant_id="p-claude",
        cwd=str(cwd),
        session_id=session_id,
        session_provenance=TranscriptProvenance.EXACT,
    )

    assert source.admit_exact_location(location=str(path), session_id=session_id) == "staged"
    for _ in range(3):
        batch = await source.read()
        assert batch.waiting is True
        assert batch.error_code is None

    _transcript(root, session_id, cwd)
    attached = await source.read()
    assert attached.attached is not None
    assert attached.attached.location == str(path)
    assert attached.attached.correlation == str(TranscriptProvenance.EXACT)
    source.commit_attachment()

    path.unlink()
    first_missing = await source.read()
    confirmed_missing = await source.read()
    assert first_missing.waiting is True
    assert first_missing.error_code is None
    assert confirmed_missing.error_code == "transcript_identity_lost"


async def test_current_path_receipt_does_not_detach_or_reset_mid_turn(registry, tmp_path):
    from theater.daemon.jobs import JobManager
    from theater.daemon.observer import Observer, QuietClock, TurnAccumulator

    cwd = tmp_path / "repo"
    cwd.mkdir()
    root = tmp_path / "claude" / "projects"
    path = _transcript(root, "11111111-1111-4111-8111-111111111111", cwd)
    source = TranscriptSource(
        ClaudeCodeObserver(root=root),
        cwd=str(cwd),
        session_id=path.stem,
        session_provenance=TranscriptProvenance.EXACT,
    )
    await source.read()
    source.commit_attachment()

    p = registry.create_spawned(harness="claude", cwd=str(cwd), pid="p-claude")
    p.transcript_location = str(path)
    registry.store.upsert_participant(p)
    jobs = JobManager(registry.store)
    jobs.create(handle="h1", caller_id="caller", target_id=p.id, kind="send")
    observer = Observer(registry, harnesses={}, jobs=jobs)
    observer._sources[p.id] = source
    clock = QuietClock(quiet_since=1.0, screen_quiet_since=2.0, rescue_since=3.0)
    turns = TurnAccumulator()
    turns.say("partial")

    assert observer.transcript_receipt(p.id, location=str(path), session_id=path.stem) == "accepted"
    assert p.id not in observer._reset_watch_state
    assert (clock.quiet_since, clock.screen_quiet_since, clock.rescue_since) == (1.0, 2.0, 3.0)
    observer._apply(
        p.id,
        Batch(events=[Event(kind=EventKind.ASSISTANT, turn_end=True)]),
        clock,
        turns,
    )

    assert jobs.get("h1").result == "partial"


async def test_receipt_does_not_persist_until_source_admits(registry, tmp_path):
    from theater.daemon.observer import Observer

    cwd = tmp_path / "repo"
    cwd.mkdir()
    path = (
        tmp_path / "claude" / "projects" / "project" / "11111111-1111-4111-8111-111111111111.jsonl"
    )
    p = registry.create_spawned(harness="claude", cwd=str(cwd), pid="p-claude")
    observer = Observer(registry, harnesses={})

    assert observer.transcript_receipt(p.id, location=str(path), session_id=path.stem) == "staged"

    got = registry.store.get_participant(p.id)
    assert got.session_correlation is None
    assert got.transcript_location is None


async def test_staged_exact_receipt_rearms_quarantined_watcher(registry, tmp_path):
    from theater.daemon.observer import Observer

    cwd = tmp_path / "repo"
    cwd.mkdir()
    root = tmp_path / "claude" / "projects"
    old = _transcript(root, "11111111-1111-4111-8111-111111111111", cwd)
    replacement = _transcript(root, "22222222-2222-4222-8222-222222222222", cwd)
    source = TranscriptSource(
        ClaudeCodeObserver(root=root),
        cwd=str(cwd),
        session_id=old.stem,
        session_provenance=TranscriptProvenance.EXACT,
        known_location=str(old),
    )
    initial = await source.read()
    assert initial.attached is not None
    source.commit_attachment()
    participant = registry.create_spawned(harness="claude", cwd=str(cwd), pid="p-claude")
    participant.session_id = old.stem
    participant.session_correlation = "exact"
    participant.transcript_location = str(old)
    registry.store.upsert_participant(participant)
    observer = Observer(registry, harnesses={})
    observer._sources[participant.id] = source
    observer.mark_transcript_identity_lost(participant.id, "rotation evidence")

    admission = observer.transcript_receipt(
        participant.id,
        location=str(replacement),
        session_id=replacement.stem,
    )
    candidate = await source.read()

    assert admission == "staged"
    assert not observer.transcript_identity_lost(participant.id)
    assert candidate.attached is not None
    assert candidate.attached.location == str(replacement)
    assert candidate.attached.correlation == "exact"


async def test_same_cwd_competitor_cannot_claim_unbound_foreign_receipt(
    claude_daemon, claude_client, tmp_path
):
    daemon, root = claude_daemon
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _spawn_claude(daemon, cwd, pid="first", token="one")
    _spawn_claude(daemon, cwd, pid="second", token="two")
    path = _transcript(root, "33333333-3333-4333-8333-333333333333", cwd)

    with pytest.raises(RemoteError, match="shares its cwd"):
        await _receipt(
            client=claude_client,
            pid="first",
            token="one",
            session_id=path.stem,
            transcript_path=str(path),
        )


def test_live_receipt_tokens_ignore_legacy_expiry(registry, tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    token_file = tmp_path / "token"
    token_file.write_text("secret")
    p = registry.create_spawned(harness="claude", cwd=str(cwd), pid="p-claude")
    registry.store.set_meta(
        f"receipt_token:{p.id}",
        json.dumps(
            {"token": "secret", "token_path": str(token_file), "expires_at": time.time() - 1}
        ),
    )

    assert registry.store.get_receipt_token(p.id) == "secret"
    assert token_file.exists()


def test_mark_dead_removes_receipt_token_file(registry, tmp_path):
    cwd = tmp_path / "repo"
    cwd.mkdir()
    token_file = tmp_path / "token"
    token_file.write_text("secret")
    p = registry.create_spawned(harness="claude", cwd=str(cwd), pid="p-claude")
    registry.store.set_receipt_token(p.id, "secret", token_path=str(token_file))

    registry.mark_dead(p.id)

    assert registry.store.get_meta(f"receipt_token:{p.id}") is None
    assert not token_file.exists()


async def test_gc_removes_orphaned_receipt_tokens(store, tmp_path):
    from theater.config import RetentionSection
    from theater.daemon.gc import sweep

    token_file = tmp_path / "token"
    token_file.write_text("secret")
    store.set_receipt_token("missing", "secret", token_path=str(token_file))

    await sweep(store, RetentionSection())

    assert store.get_meta("receipt_token:missing") is None
    assert not token_file.exists()


async def test_gc_removes_dead_participant_receipt_tokens(registry, tmp_path):
    from theater.config import RetentionSection
    from theater.daemon.gc import sweep

    cwd = tmp_path / "repo"
    cwd.mkdir()
    token_file = tmp_path / "token"
    token_file.write_text("secret")
    p = registry.create_spawned(harness="claude", cwd=str(cwd), pid="p-claude")
    registry.store.set_receipt_token(p.id, "secret", token_path=str(token_file))
    registry.store.set_status(p.id, "dead")

    await sweep(registry.store, RetentionSection())

    assert registry.store.get_meta(f"receipt_token:{p.id}") is None
    assert not token_file.exists()


def test_private_token_file_is_restricted_even_when_preexisting(registry, tmp_path):
    from theater.daemon.spawner import Spawner
    from theater.harness.base import LaunchPlan

    token_file = paths.observation_dir("claude", "p-claude") / "receipt-token"
    token_file.parent.mkdir(parents=True, exist_ok=True)
    token_file.parent.chmod(0o755)
    token_file.write_text("old")
    token_file.chmod(0o644)

    Spawner(registry)._write_plan_files(
        LaunchPlan(argv=[], receipt_token="new", receipt_token_path=token_file)
    )

    assert stat.S_IMODE(token_file.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600


def test_claude_receipt_cli_is_quiet_and_does_not_autostart(monkeypatch, capsys, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("secret")
    args = cli._parser().parse_args(
        ["claude-receipt", "--id", "p-claude", "--token-file", str(token_file)]
    )
    monkeypatch.setattr("sys.stdin", type("Input", (), {"read": lambda self: "not-json"})())

    assert cli.cmd_claude_receipt(args) == 0
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == ""


def _receipt_args(token_file: Path):
    return cli._parser().parse_args(
        ["claude-receipt", "--id", "p-claude", "--token-file", str(token_file)]
    )


def _receipt_stdin(path: Path) -> io.StringIO:
    return io.StringIO(
        json.dumps(
            {
                "session_id": path.stem,
                "transcript_path": str(path),
            }
        )
    )


def test_claude_receipt_cli_uses_non_autostart_client(monkeypatch, tmp_path):
    token_file = tmp_path / "token"
    token_file.write_text("secret")
    transcript = tmp_path / "11111111-1111-4111-8111-111111111111.jsonl"
    seen = []

    class Client:
        def __init__(self, *, autostart=True):
            seen.append(autostart)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return None

        async def call(self, method, **params):
            return {"ok": True}

    monkeypatch.setattr(identity_mod, "DaemonClient", Client)
    monkeypatch.setattr("sys.stdin", _receipt_stdin(transcript))

    assert cli.cmd_claude_receipt(_receipt_args(token_file)) == 0
    assert seen == [False]


@pytest.mark.parametrize(
    "exc",
    [
        ConnectionError("no daemon"),
        RemoteError("bad_request", "rejected"),
    ],
)
def test_claude_receipt_cli_is_quiet_when_daemon_cannot_accept(monkeypatch, capsys, tmp_path, exc):
    token_file = tmp_path / "token"
    token_file.write_text("secret")
    transcript = tmp_path / "11111111-1111-4111-8111-111111111111.jsonl"

    async def reject(*args, **kwargs):
        raise exc

    monkeypatch.setattr(identity_mod, "_send_transcript_receipt", reject)
    monkeypatch.setattr("sys.stdin", _receipt_stdin(transcript))

    assert cli.cmd_claude_receipt(_receipt_args(token_file)) == 0
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err == ""


async def test_claude_receipt_rpc_alias_still_works(claude_daemon, claude_client, tmp_path):
    """The claude.receipt RPC alias forwards to transcript.receipt.

    Live Claude sessions have settings.json on disk invoking
    ``claude.receipt`` by that exact name. The alias must keep working.
    """
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

    assert await _until(
        lambda: daemon.store.get_participant("p-claude").transcript_location == str(path.resolve())
    )
