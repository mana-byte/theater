"""Tests for the wrapper-renamed-binary and unknown-harness recovery fixes.

Covers:
- Fix 1: classify_node treats "unknown" as absence of evidence, not conflict.
- Fix 2: preflight verifies the creator's pane before the atomic claim.
- Fix 3: detect_harness / match_binary handle wrapper-renamed binaries.
- Fix 4: a still-live creator that fails verification does not blind-skip descendants.
"""

from __future__ import annotations

import json

import pytest

from theater.daemon.harness_detect import detect_harness, match_binary
from theater.daemon.recovery import (
    classify_node,
)
from theater.harness import HARNESSES
from theater.models import Participant, Status, Tier
from theater.protocol import RemoteError

# ---- helpers ----------------------------------------------------------------


def _prov(cwd: str = "/tmp", **kwargs) -> str:
    base = {
        "prompt": "",
        "approval": "yolo",
        "cwd_requested": cwd,
        "cwd_resolved": cwd,
        "model": None,
        "reasoning_effort": None,
        "worktree": False,
        "worktree_type": None,
        "worktree_name": None,
        "worktree_branch": None,
        "base_branch": None,
        "response_format": None,
        "resume_session_id": None,
        "tmux_session": None,
        "window_name": None,
    }
    base.update(kwargs)
    return json.dumps(base, sort_keys=True, separators=(",", ":"))


def _make_participant(
    daemon,
    *,
    pid: str,
    harness: str = "vibe",
    cwd: str = "/tmp",
    pane: str | None = None,
    parent_id: str | None = None,
    dead: bool = False,
    session_id: str | None = None,
    session_correlation: str | None = None,
    launch_provenance: str | None = None,
) -> Participant:
    p = Participant(
        id=pid,
        harness=harness,
        tier=Tier.SPAWNED,
        cwd=cwd,
        tmux_pane=pane,
        parent_id=parent_id,
        session_id=session_id,
        session_correlation=session_correlation,
        launch_provenance=launch_provenance,
    )
    daemon.store.upsert_participant(p)
    if dead:
        daemon.store.set_status(pid, Status.DEAD)
    return p


# ---- Fix 1: classify_node "unknown" semantics --------------------------------


def test_classify_unknown_non_shell_is_live():
    """Unknown harness detection with a non-shell command → live (trust DB).

    A wrapper-renamed binary (.claude-wrapped) produces "unknown" from
    detect_harness, but the participant is still running — absence of
    evidence is not evidence of a foreign harness.
    """
    recorded = {"participant_id": "p", "jobs": [], "session_id": None, "session_correlation": None}
    live = Participant(id="p", harness="claude", tier=Tier.SPAWNED, tmux_pane="%1")
    pane_info = {"pane_id": "%1", "harness": "unknown", "command": ".claude-wrapped"}
    cls, reason = classify_node(recorded, live, {}, pane_info=pane_info)
    assert cls == "live", f"unknown + non-shell → live; got {cls!r}"
    assert "trusting DB" in reason


def test_classify_unknown_shell_is_stale_live():
    """Unknown harness detection with a shell command → stale_live.

    The CLI exited and left a prompt: the pane shows a shell, not the
    harness. This is the "the CLI died" signal.
    """
    recorded = {"participant_id": "p", "jobs": [], "session_id": None, "session_correlation": None}
    live = Participant(id="p", harness="claude", tier=Tier.SPAWNED, tmux_pane="%1")
    pane_info = {"pane_id": "%1", "harness": "unknown", "command": "bash"}
    cls, reason = classify_node(recorded, live, {}, pane_info=pane_info)
    assert cls == "stale_live", f"unknown + shell → stale_live; got {cls!r}"
    assert "shell" in reason


def test_classify_recorded_harness_unknown_is_live():
    """When the participant's own recorded harness is "unknown", nothing to
    compare against — trust the DB row."""
    recorded = {"participant_id": "p", "jobs": [], "session_id": None, "session_correlation": None}
    live = Participant(id="p", harness="unknown", tier=Tier.SPAWNED, tmux_pane="%1")
    pane_info = {"pane_id": "%1", "harness": "claude", "command": "claude"}
    cls, _reason = classify_node(recorded, live, {}, pane_info=pane_info)
    assert cls == "live", f"recorded harness unknown → live; got {cls!r}"


def test_classify_genuine_harness_conflict_still_conflict():
    """A positively identified DIFFERENT harness → live_harness_conflict."""
    recorded = {"participant_id": "p", "jobs": [], "session_id": None, "session_correlation": None}
    live = Participant(id="p", harness="vibe", tier=Tier.SPAWNED, tmux_pane="%1")
    pane_info = {"pane_id": "%1", "harness": "claude", "command": "claude"}
    cls, _reason = classify_node(recorded, live, {}, pane_info=pane_info)
    assert cls == "live_harness_conflict", f"genuine mismatch → live_harness_conflict; got {cls!r}"


# ---- Fix 3: detect_harness / match_binary wrapper normalisation --------------


def test_match_binary_wrapper_name_resolves():
    """.claude-wrapped resolves to the claude harness."""
    result = match_binary(".claude-wrapped", HARNESSES)
    assert result == "claude", f".claude-wrapped → claude; got {result!r}"


def test_match_binary_wrapper_with_path_resolves():
    """A full path with a wrapper basename still resolves."""
    result = match_binary("/nix/store/.../bin/.claude-wrapped", HARNESSES)
    assert result == "claude"


def test_match_binary_plain_name_still_works():
    """Normal binary names are not broken by the normalisation."""
    assert match_binary("claude", HARNESSES) == "claude"
    assert match_binary("vibe", HARNESSES) == "vibe"


def test_match_binary_unknown_command_returns_none():
    """An unrecognised command returns None."""
    assert match_binary("some-random-program", HARNESSES) is None


def test_detect_harness_wrapper_name_resolves():
    """detect_harness with a wrapper name and an unreachable pid → claude.

    The descendant walk fails (pid does not exist), but the direct match
    on the wrapper-renamed basename succeeds.
    """
    result = detect_harness(".claude-wrapped", 999_999)
    assert result == "claude", f"detect_harness(.claude-wrapped) → claude; got {result!r}"


def test_detect_harness_plain_name_still_works():
    """Normal binary names still resolve through detect_harness."""
    assert detect_harness("vibe", 999_999) == "vibe"
    assert detect_harness("claude", 999_999) == "claude"


def test_known_binaries_includes_wrapper_names():
    """known_binaries includes plugin-declared wrapper aliases."""
    from theater.harness import known_binaries

    kb = known_binaries()
    assert ".claude-wrapped" in kb


# ---- Fix 4: still-live creator does not blind-skip descendants ---------------


async def test_live_creator_with_wrapper_command_self_restore(client, daemon, fake_tmux):
    """The exact reported scenario: a live claude creator with .claude-wrapped
    pane command and two live idle children. Self-restore must give
    creator=REUSED_LIVE and children=REUSED_LIVE, restore_state=restored.
    """
    # Override the default panes: creator runs .claude-wrapped, children run vibe.
    fake_tmux.add_pane("%10", command=".claude-wrapped", pid=10001)
    fake_tmux.add_pane("%11", command="vibe", pid=10002)
    fake_tmux.add_pane("%12", command="vibe", pid=10003)

    _make_participant(daemon, pid="creator", harness="claude", pane="%10")
    _make_participant(daemon, pid="child1", harness="vibe", pane="%11", parent_id="creator")
    _make_participant(daemon, pid="child2", harness="vibe", pane="%12", parent_id="creator")

    created = await client.call("checkpoint.create", caller_id="creator", name="cp")

    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="creator",
    )

    assert result["restore_state"] == "restored", (
        f"Expected restored, got {result['restore_state']!r}: {result}"
    )
    assert result["creator"]["action"] == "reused_live", (
        f"Creator should be reused_live, got {result['creator']['action']!r}"
    )
    assert result["creator"]["original_participant_id"] == "creator"
    # Both children should be reconciled independently (reused_live).
    descendants = {r["original_participant_id"]: r for r in result["descendants"]}
    assert len(descendants) == 2
    for child_id in ("child1", "child2"):
        assert descendants[child_id]["action"] == "reused_live", (
            f"{child_id} should be reused_live, got {descendants[child_id]['action']!r}"
        )


async def test_live_creator_genuine_conflict_refused_by_preflight(client, daemon, fake_tmux):
    """When the creator's pane runs a genuinely different harness, preflight
    refuses before the claim — the checkpoint stays ready and retryable."""
    fake_tmux.add_pane("%10", command="claude", pid=10001)
    fake_tmux.add_pane("%11", command="vibe", pid=10002)

    # Creator is recorded as "vibe" but the pane runs "claude" → harness conflict.
    _make_participant(daemon, pid="creator", harness="vibe", pane="%10")
    _make_participant(daemon, pid="child", harness="vibe", pane="%11", parent_id="creator")

    created = await client.call("checkpoint.create", caller_id="creator", name="cp")

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "checkpoint.restore",
            checkpoint_id=created["checkpoint_id"],
            approval="yolo",
            caller_id="creator",
        )
    assert exc.value.code == "bad_request"
    # The checkpoint must still be "ready" and retryable.
    cp = daemon.store.get_checkpoint(created["checkpoint_id"])
    assert cp is not None
    assert cp["restore_state"] == "ready", (
        f"Checkpoint should be ready (not consumed), got {cp['restore_state']!r}"
    )


async def test_preflight_rejects_mismatched_creator_pane(client, daemon, fake_tmux):
    """A creator whose pane runs a genuinely different harness is refused by
    preflight, leaving the checkpoint ready and retryable."""
    fake_tmux.add_pane("%10", command="claude", pid=10001)

    _make_participant(daemon, pid="creator", harness="vibe", pane="%10")
    _make_participant(daemon, pid="child", harness="vibe", pane="%11", parent_id="creator")

    created = await client.call("checkpoint.create", caller_id="creator", name="cp")

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "checkpoint.restore",
            checkpoint_id=created["checkpoint_id"],
            approval="yolo",
            caller_id="creator",
        )
    assert exc.value.code == "bad_request"
    assert "runs" in str(exc.value) or "exited" in str(exc.value)

    # The checkpoint must still be "ready" and retryable.
    cp = daemon.store.get_checkpoint(created["checkpoint_id"])
    assert cp is not None
    assert cp["restore_state"] == "ready", (
        f"Checkpoint should be ready (not consumed), got {cp['restore_state']!r}"
    )


async def test_preflight_rejects_exited_creator_shell_pane(client, daemon, fake_tmux):
    """A creator whose CLI has exited (pane shows a shell) is refused by
    preflight, leaving the checkpoint ready."""
    fake_tmux.add_pane("%10", command="bash", pid=10001)

    _make_participant(daemon, pid="creator", harness="vibe", pane="%10")

    created = await client.call("checkpoint.create", caller_id="creator", name="cp")

    with pytest.raises(RemoteError) as exc:
        await client.call(
            "checkpoint.restore",
            checkpoint_id=created["checkpoint_id"],
            approval="yolo",
            caller_id="creator",
        )
    assert exc.value.code == "bad_request"
    assert "exited" in str(exc.value) or "shell" in str(exc.value)

    cp = daemon.store.get_checkpoint(created["checkpoint_id"])
    assert cp is not None
    assert cp["restore_state"] == "ready"


async def test_preflight_wrapper_command_not_rejected(client, daemon, fake_tmux):
    """A creator with a wrapper-renamed pane command passes preflight and
    restores normally (this is the whole point of Fix 2+3)."""
    fake_tmux.add_pane("%10", command=".claude-wrapped", pid=10001)
    fake_tmux.add_pane("%11", command="vibe", pid=10002)

    _make_participant(daemon, pid="creator", harness="claude", pane="%10")
    _make_participant(daemon, pid="child", harness="vibe", pane="%11", parent_id="creator")

    created = await client.call("checkpoint.create", caller_id="creator", name="cp")

    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="creator",
    )

    assert result["restore_state"] == "restored"
    assert result["creator"]["action"] == "reused_live"


async def test_dead_creator_failure_cascades_to_skip(client, daemon, fake_tmux):
    """When the creator is genuinely dead and reconstruction fails, descendants
    ARE cascade-skipped (the narrowed cascade still fires for real failures)."""
    from theater.daemon.schema import jobs as jobs_table

    # Dead creator with an open (running) job and no provenance → failed
    # (incomplete work, no way to restore).
    _make_participant(daemon, pid="creator", pane="%1", dead=True)
    daemon.store.conn.execute(
        jobs_table.insert().values(
            handle="h-open",
            caller_id="restorer",
            target_id="creator",
            kind="spawn",
            prompt="do work",
            state="running",
            result=None,
            error_code=None,
            created_at=1.0,
            finished_at=None,
        )
    )
    _make_participant(daemon, pid="child", pane="%2", parent_id="creator", dead=True)

    created = await client.call("checkpoint.create", caller_id="creator", name="cp")

    # Use a live restorer that is NOT the creator.
    _make_participant(daemon, pid="restorer", pane="%3")

    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="restorer",
    )

    # Creator is dead with no provenance and open work → failed. Children
    # cascade-skipped (the narrowed cascade still fires for real failures).
    assert result["creator"]["action"] == "failed", (
        f"Creator should be failed, got {result['creator']['action']!r}"
    )
    descendants = {r["original_participant_id"]: r for r in result["descendants"]}
    assert descendants["child"]["action"] == "skipped", (
        f"Child should be skipped, got {descendants['child']['action']!r}"
    )
    assert descendants["child"]["classification"] == "ancestor_skipped"
    assert result["restore_state"] == "failed"
