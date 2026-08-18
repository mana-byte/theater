"""Pane-harness verification across detection, classification, and restore.

Covers the shared ``compare_pane_harness`` decision function, the wrapper-
renamed-binary detection that motivated it, the preflight creator-pane check,
and the narrowed cascade that reconciles children of a still-live creator
that failed harness verification.
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

    Detection could not identify the harness and the foreground is not a
    shell — absence of evidence is not evidence of a foreign harness.
    """
    recorded = {"participant_id": "p", "jobs": [], "session_id": None, "session_correlation": None}
    live = Participant(id="p", harness="vibe", tier=Tier.SPAWNED, tmux_pane="%1")
    pane_info = {"pane_id": "%1", "harness": "unknown", "command": "some-tool", "pane_pid": 0}
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
    pane_info = {"pane_id": "%1", "harness": "unknown", "command": "bash", "pane_pid": 0}
    cls, reason = classify_node(recorded, live, {}, pane_info=pane_info)
    assert cls == "stale_live", f"unknown + shell → stale_live; got {cls!r}"
    assert "shell" in reason


def test_classify_recorded_harness_unknown_is_live():
    """When the participant's own recorded harness is "unknown", nothing to
    compare against — trust the DB row."""
    recorded = {"participant_id": "p", "jobs": [], "session_id": None, "session_correlation": None}
    live = Participant(id="p", harness="unknown", tier=Tier.SPAWNED, tmux_pane="%1")
    pane_info = {"pane_id": "%1", "harness": "claude", "command": "claude", "pane_pid": 0}
    cls, _reason = classify_node(recorded, live, {}, pane_info=pane_info)
    assert cls == "live", f"recorded harness unknown → live; got {cls!r}"


def test_classify_genuine_harness_conflict_still_conflict():
    """A positively identified DIFFERENT harness → live_harness_conflict."""
    recorded = {"participant_id": "p", "jobs": [], "session_id": None, "session_correlation": None}
    live = Participant(id="p", harness="vibe", tier=Tier.SPAWNED, tmux_pane="%1")
    pane_info = {"pane_id": "%1", "harness": "claude", "command": "claude", "pane_pid": 0}
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


# ---- compare_pane_harness unit tests ----------------------------------------


def test_compare_pane_harness_match():
    """Same harness (via wrapper normalisation) → match."""
    from theater.daemon.harness_detect import PaneHarnessVerdict, compare_pane_harness

    assert compare_pane_harness("claude", ".claude-wrapped", 999999) is PaneHarnessVerdict.MATCH


def test_compare_pane_harness_conflict():
    """Different positively-identified harness → conflict."""
    from theater.daemon.harness_detect import PaneHarnessVerdict, compare_pane_harness

    assert compare_pane_harness("vibe", "claude", 999999) is PaneHarnessVerdict.CONFLICT


def test_compare_pane_harness_harness_gone():
    """Unknown detection + shell foreground → harness_gone."""
    from theater.daemon.harness_detect import PaneHarnessVerdict, compare_pane_harness

    assert compare_pane_harness("vibe", "bash", 999999) is PaneHarnessVerdict.HARNESS_GONE


def test_compare_pane_harness_undetermined():
    """Unknown detection + non-shell foreground → undetermined."""
    from theater.daemon.harness_detect import PaneHarnessVerdict, compare_pane_harness

    assert compare_pane_harness("vibe", "some-tool", 999999) is PaneHarnessVerdict.UNDETERMINED


def test_compare_pane_harness_recorded_unknown_is_match():
    """When recorded harness is 'unknown', nothing to compare → match."""
    from theater.daemon.harness_detect import PaneHarnessVerdict, compare_pane_harness

    assert compare_pane_harness("unknown", "claude", 999999) is PaneHarnessVerdict.MATCH


# ---- Non-cascade race: creator fails verification inside restore_tree --------


async def test_live_creator_harness_race_children_reconciled(
    client, daemon, fake_tmux, monkeypatch
):
    """Children of a still-live creator that fails verification inside
    restore_tree are reconciled independently, not blind-skipped.

    This exercises the non-cascade ``pass`` branch: preflight sees a clean
    pane, but by the time ``restore_tree`` runs ``_get_pane_info`` the pane
    has changed to a conflicting harness — the race that branch exists for.
    We simulate it by making ``_get_pane_info`` return a conflict for the
    creator only.
    """
    import theater.daemon.recovery as recovery_mod

    fake_tmux.add_pane("%10", command="vibe", pid=10001)
    fake_tmux.add_pane("%11", command="vibe", pid=10002)

    _make_participant(daemon, pid="creator", harness="vibe", pane="%10")
    _make_participant(daemon, pid="child", harness="vibe", pane="%11", parent_id="creator")

    created = await client.call("checkpoint.create", caller_id="creator", name="cp")

    # Preflight calls _verify_creator_pane_harness which calls tmux.pane_info
    # directly (not _get_pane_info), so it sees the clean "vibe" pane.
    # But _get_pane_info (called by restore_tree) returns a conflict for the
    # creator only, simulating the pane changing between preflight and restore.
    real_get_pane_info = recovery_mod._get_pane_info

    async def _race_pane_info(daemon_arg, pane_id, snapshot=None):
        if pane_id == "%10":
            return {"pane_id": "%10", "harness": "claude", "command": "claude", "pane_pid": 10001}
        return await real_get_pane_info(daemon_arg, pane_id, snapshot)

    monkeypatch.setattr(recovery_mod, "_get_pane_info", _race_pane_info)

    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="creator",
    )

    # Creator failed harness verification inside restore_tree (the race).
    assert result["creator"]["action"] == "failed", (
        f"Creator should be failed (harness conflict), got {result['creator']['action']!r}"
    )
    assert (
        "live_harness_conflict" in result["creator"]["reason"]
        or "mismatched" in (result["creator"]["reason"])
    )

    # Child was reconciled independently, NOT ancestor-skipped.
    descendants = {r["original_participant_id"]: r for r in result["descendants"]}
    assert descendants["child"]["action"] == "reused_live", (
        f"Child should be reused_live (reconciled independently), "
        f"got {descendants['child']['action']!r}"
    )
    assert descendants["child"]["classification"] != "ancestor_skipped"

    # The creator's row must NOT have been marked dead.
    creator = daemon.store.get_participant("creator")
    assert creator is not None and creator.status is not Status.DEAD

    # One node succeeded (child), one failed (creator) → partial.
    assert result["restore_state"] == "partial", (
        f"Expected partial (one success, one failure), got {result['restore_state']!r}"
    )


# ---- detect_harness call-count regression guard -----------------------------


async def test_detect_harness_called_once_per_pane(client, daemon, fake_tmux, monkeypatch):
    """detect_harness must be called exactly once per pane during a restore,
    not twice.  _get_pane_info already runs detection; classify_node must
    consume that result rather than re-detecting — each detection is a
    subprocess spawn on the fallback path, and doubling it stalls the
    daemon event loop.
    """
    import theater.daemon.harness_detect as hd

    fake_tmux.add_pane("%10", command="vibe", pid=10001)
    fake_tmux.add_pane("%11", command="vibe", pid=10002)

    _make_participant(daemon, pid="creator", harness="vibe", pane="%10")
    _make_participant(daemon, pid="child", harness="vibe", pane="%11", parent_id="creator")

    created = await client.call("checkpoint.create", caller_id="creator", name="cp")

    # Count calls to detect_harness from _get_pane_info (the restore path).
    # _verify_creator_pane_harness (preflight) also calls detect_harness for
    # the creator, so the total is: 1 (preflight creator) + 1 (restore creator)
    # + 1 (restore child) = 3, one per pane queried.
    call_count = 0
    real_detect = hd.detect_harness

    def _counting_detect(cmd, pid, snapshot=None):
        nonlocal call_count
        call_count += 1
        return real_detect(cmd, pid, snapshot)

    # Patch at the harness_detect module level so _get_pane_info (which
    # imports detect_harness lazily) sees the counter.  Also patch methods.py
    # so the preflight's _verify_creator_pane_harness sees it too.
    monkeypatch.setattr(hd, "detect_harness", _counting_detect)
    import theater.daemon.methods as methods_mod

    monkeypatch.setattr(methods_mod, "detect_harness", _counting_detect)

    result = await client.call(
        "checkpoint.restore",
        checkpoint_id=created["checkpoint_id"],
        approval="yolo",
        caller_id="creator",
    )

    assert result["restore_state"] == "restored"
    # 3 detect_harness calls, one per pane queried:
    #   - preflight: creator pane → 1 call (no snapshot; fresh ps)
    #   - restore_tree: creator pane via _get_pane_info → 1 call (shared snapshot)
    #   - restore_tree: child pane via _get_pane_info → 1 call (shared snapshot)
    # A count of 4 would mean classify_node is re-detecting instead of
    # consuming pane_info['harness'].  The shared snapshot means the two
    # restore-path calls pay one ps total, not two — but the function is
    # still called once per pane.
    assert call_count == 3, (
        f"detect_harness called {call_count} times, expected 3 "
        f"(1 preflight + 2 restore); a count of 4 means classify_node is "
        f"re-detecting instead of consuming pane_info['harness']"
    )
