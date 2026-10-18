"""Codex correlation pins, rotation probes and native sub-agent rollouts."""

from __future__ import annotations

import os

import pytest

from tests.support.codex_process_correlation import PID_A, SESSION_A, SESSION_B, history_of
from tests.support.codex_rollouts import hold, write_rollout
from theater.harness.builtin.plugins.codex.observer import CodexObserver
from theater.provenance import TranscriptProvenance

SESSION_C = "01a00cdf-ffff-7f92-8429-d75394c3edfc"


async def test_a_heuristic_pin_is_replaced_by_what_the_process_holds(monkeypatch, codex_tree):
    """The upgrade path, and the one that fixes participants already running.

    A participant bound before process evidence existed carries an admitted
    location that may be its sibling's. Every later poll takes that pin before
    discovery is consulted, so without a proof-only channel it would stay
    contested — and `read_transcript` would keep refusing — for the rest of
    the session.
    """
    hold(monkeypatch, {PID_A: [codex_tree["a"]]})
    reader = CodexObserver(root=codex_tree["root"], pane_pid=PID_A)

    history = await history_of(reader, codex_tree["project"], known_location=str(codex_tree["b"]))

    assert history.location == str(codex_tree["a"].resolve())
    assert history.correlation == "proven"


async def test_the_watcher_restages_a_pinned_sibling_and_commits_the_proven_one(
    monkeypatch, codex_tree
):
    """The repair as the poll loop performs it, not as `read_transcript` does.

    The test above proves the upgrade through `history`, which has its own call
    to it; the watcher reaches the same seam by another road — `read` stages an
    attachment, the reducer accepts it, `commit_attachment` makes it live — and
    that road is the one that repairs a running participant. It has to be
    exercised separately, because removing the upgrade from `_attach` alone
    leaves the history test green.
    """
    hold(monkeypatch, {PID_A: [codex_tree["a"]]})
    reader = CodexObserver(root=codex_tree["root"], pane_pid=PID_A)
    source = reader.open_source(
        cwd=str(codex_tree["project"]),
        known_location=str(codex_tree["b"]),
    )
    try:
        batch = await source.read()

        assert batch.attached is not None
        assert batch.attached.location == str(codex_tree["a"].resolve())
        assert batch.attached.correlation == "proven"
        assert source.path is None, "a candidate must not go live before the reducer accepts it"

        source.commit_attachment()

        assert source.path == codex_tree["a"].resolve()
    finally:
        await source.aclose()


async def test_a_pin_the_process_cannot_improve_on_is_left_alone(monkeypatch, codex_tree):
    """A probe that proves nothing must not turn a pin into a fresh guess.

    The pin here is the *older* rollout, so a fall-through to the cwd scan
    would visibly drift onto the newer sibling. Staying heuristic is the
    correct outcome; drifting is the mis-attribution.
    """
    hold(monkeypatch, {PID_A: []})
    reader = CodexObserver(root=codex_tree["root"], pane_pid=PID_A)

    history = await history_of(reader, codex_tree["project"], known_location=str(codex_tree["a"]))

    assert history.location == str(codex_tree["a"])
    assert history.correlation == "heuristic"


async def test_newer_codex_guess_is_only_noncommittable_loss_evidence(monkeypatch, codex_tree):
    os.utime(codex_tree["a"], ns=(1_000_000_000, 1_000_000_000))
    os.utime(codex_tree["b"], ns=(1_500_000_000, 1_500_000_000))
    hold(monkeypatch, {PID_A: []})
    reader = CodexObserver(root=codex_tree["root"], pane_pid=PID_A)
    source = reader.open_source(
        cwd=str(codex_tree["project"]),
        session_id=SESSION_A,
        session_provenance=TranscriptProvenance.OPERATOR,
        known_location=str(codex_tree["a"]),
    )
    initial = await source.read()
    assert initial.attached is not None
    source.commit_attachment()
    newer = write_rollout(
        codex_tree["root"],
        SESSION_C,
        codex_tree["project"],
        at="01-20-46",
        text="not attributed",
    )
    os.utime(newer, ns=(2_000_000_000, 2_000_000_000))

    refresh = await source.refresh()
    evidence = await source.probe_identity_loss()

    assert refresh.attached is None
    assert evidence is not None
    assert evidence.location == str(newer)
    assert source.path == codex_tree["a"]


def test_native_subagents_do_not_trigger_or_exhaust_identity_loss_probe(codex_tree):
    os.utime(codex_tree["a"], ns=(1, 1))
    os.utime(codex_tree["b"], ns=(0, 0))
    primary = write_rollout(
        codex_tree["root"],
        SESSION_C,
        codex_tree["project"],
        at="01-20-46",
        text="independent primary",
        metadata={"thread_source": "user", "source": "cli"},
    )
    os.utime(primary, ns=(2, 2))
    for index in range(9):
        child = write_rollout(
            codex_tree["root"],
            f"01a00cdf-dead-beef-9a9f-{index:012d}",
            codex_tree["project"],
            at=f"01-21-{index:02d}",
            text="native child",
            metadata={"thread_source": "subagent", "source": {"subagent": {}}},
        )
        os.utime(child, ns=(10 + index, 10 + index))
    reader = CodexObserver(root=codex_tree["root"])

    candidate = reader.identity_loss_candidate(
        cwd=str(codex_tree["project"]),
        current=codex_tree["a"],
        current_mtime_ns=codex_tree["a"].stat().st_mtime_ns,
    )

    assert candidate == primary


async def test_process_proven_codex_rotation_still_attaches(monkeypatch, codex_tree):
    held = {PID_A: [codex_tree["a"]]}
    hold(monkeypatch, held)
    reader = CodexObserver(root=codex_tree["root"], pane_pid=PID_A)
    source = reader.open_source(
        cwd=str(codex_tree["project"]),
        session_id=SESSION_A,
        session_provenance=TranscriptProvenance.OPERATOR,
        known_location=str(codex_tree["a"]),
    )
    initial = await source.read()
    assert initial.attached is not None
    source.commit_attachment()
    newer = write_rollout(
        codex_tree["root"],
        SESSION_C,
        codex_tree["project"],
        at="01-20-46",
        text="still mine",
    )
    held[PID_A] = [newer]

    refresh = await source.refresh()

    assert refresh.attached is not None
    assert refresh.attached.location == str(newer.resolve())
    assert refresh.attached.correlation == "proven"


async def test_quiet_transcript_backs_off_rotation_probes(monkeypatch, codex_tree):
    asked = hold(monkeypatch, {PID_A: [codex_tree["a"]]})
    clock = [100.0]
    monkeypatch.setattr("theater.harness.transcript.source.time.monotonic", lambda: clock[0])
    reader = CodexObserver(root=codex_tree["root"], pane_pid=PID_A)
    source = reader.open_source(
        cwd=str(codex_tree["project"]),
        session_id=SESSION_A,
        session_provenance=TranscriptProvenance.OPERATOR,
        known_location=str(codex_tree["a"]),
    )
    await source.read()
    source.commit_attachment()
    probes = []
    for _ in range(4):  # relocate windows 5 s apart while nothing changes
        before = len(asked.open_files)
        await source.refresh()
        probes.append(len(asked.open_files) > before)
        clock[0] += 5
    assert probes == [True, False, True, False]  # 5 s, then 10 s spacing


async def test_committing_a_guess_gives_up_the_claim_that_the_id_was_exact(monkeypatch, codex_tree):
    """Even an unscoped source cannot launder a guessed location into proof."""
    hold(monkeypatch, {PID_A: []})
    reader = CodexObserver(root=codex_tree["root"], session_provenance=TranscriptProvenance.EXACT)
    source = reader.open_source(
        cwd=str(codex_tree["project"]),
        session_id="01a00cdf-0000-0000-0000-000000000000",
        session_provenance=TranscriptProvenance.EXACT,
    )
    try:
        batch = await source.read()
        assert batch.attached is not None
        assert batch.attached.correlation == "heuristic"
        source.commit_attachment()

        after = await source.history(last_n=0)
    finally:
        await source.aclose()

    assert after.correlation == "heuristic"


async def test_open_source_does_not_let_the_two_provenance_flags_disagree(monkeypatch, codex_tree):
    """`session_exact` has to reach the ordering, not just the labelling.

    The observer decides which key discovery asks first and the source decides
    how the answer is labelled. A caller that passes the flag to `open_source`
    alone would be told its exact id is exact while the process was still
    asked ahead of it.
    """
    asked = hold(monkeypatch, {PID_A: [codex_tree["a"]]})
    reader = CodexObserver(root=codex_tree["root"], pane_pid=PID_A)

    history = await history_of(
        reader,
        codex_tree["project"],
        session_id=SESSION_B,
        session_provenance=TranscriptProvenance.EXACT,
    )

    assert history.location == str(codex_tree["b"])
    assert asked.anything == []


async def test_a_pin_that_is_already_exact_is_not_probed(monkeypatch, codex_tree):
    """Nothing to gain, three subprocesses to lose."""
    asked = hold(monkeypatch, {PID_A: [codex_tree["a"]]})
    reader = CodexObserver(
        root=codex_tree["root"], pane_pid=PID_A, session_provenance=TranscriptProvenance.EXACT
    )

    history = await history_of(
        reader,
        codex_tree["project"],
        session_id=SESSION_B,
        session_provenance=TranscriptProvenance.EXACT,
        known_location=str(codex_tree["b"]),
    )

    assert history.location == str(codex_tree["b"])
    assert history.correlation == "exact"
    assert asked.anything == []


async def test_two_open_rollouts_are_not_evidence(monkeypatch, codex_tree):
    """One codex session holds one rollout. Two means we are guessing again."""
    hold(monkeypatch, {PID_A: [codex_tree["a"], codex_tree["b"]]})
    reader = CodexObserver(root=codex_tree["root"], pane_pid=PID_A)

    source = reader.open_source(cwd=str(codex_tree["project"]))
    try:
        batch = await source.read()
        history = await source.history(last_n=0)
        page = await source.history_page()
        assert batch.waiting and batch.attached is None
        assert history.location is page.location is None
        for result in (batch, history, page):
            assert result.error_code == "transcript_correlation_ambiguous"
    finally:
        await source.aclose()


async def test_native_subagent_rollout_does_not_make_process_proof_ambiguous(
    monkeypatch, codex_tree
):
    child = write_rollout(
        codex_tree["root"],
        SESSION_C,
        codex_tree["project"],
        at="01-20-46",
        text="native child",
        metadata={"thread_source": "subagent", "source": {"subagent": {}}},
    )
    hold(monkeypatch, {PID_A: [codex_tree["a"], child]})
    reader = CodexObserver(root=codex_tree["root"], pane_pid=PID_A)

    found = reader.find_transcript(cwd=str(codex_tree["project"]))

    assert found == codex_tree["a"].resolve()
    assert reader.proved(codex_tree["a"])


@pytest.mark.parametrize(
    "metadata",
    [
        {"thread_source": "subagent", "source": "cli"},
        {"thread_source": "user", "source": {"subagent": {}}},
    ],
)
def test_each_native_subagent_marker_blocks_exact_and_operator_attachment(
    monkeypatch, tmp_path, metadata
):
    root = tmp_path / ".codex" / "sessions"
    project = tmp_path / "project"
    project.mkdir()
    write_rollout(
        root,
        SESSION_A,
        project,
        at="01-19-02",
        text="parent",
        metadata={"thread_source": "user", "source": "cli"},
    )
    child = write_rollout(
        root,
        SESSION_C,
        project,
        at="01-20-46",
        text="native child",
        metadata=metadata,
    )
    hold(monkeypatch, {PID_A: []})
    reader = CodexObserver(root=root, pane_pid=PID_A, session_exact=True)

    assert reader.find_transcript(cwd=str(project), session_id=SESSION_C) is None
    with pytest.raises(ValueError, match="codex native subagent rollout"):
        reader.admit_operator_candidate(cwd=str(project), candidate=str(child))
    listed = next(
        row for row in reader.transcript_candidates(cwd=str(project)) if row.location == str(child)
    )
    assert listed.rejection_reason == "codex native subagent rollout"


def test_rejected_exact_subagent_falls_through_to_parent_process_proof(monkeypatch, tmp_path):
    root = tmp_path / ".codex" / "sessions"
    project = tmp_path / "project"
    project.mkdir()
    parent = write_rollout(
        root,
        SESSION_A,
        project,
        at="01-19-02",
        text="parent",
        metadata={"thread_source": "user", "source": "cli"},
    )
    write_rollout(
        root,
        SESSION_C,
        project,
        at="01-20-46",
        text="native child",
        metadata={"thread_source": "subagent", "source": {"subagent": {}}},
    )
    hold(monkeypatch, {PID_A: [parent]})
    reader = CodexObserver(root=root, pane_pid=PID_A, session_exact=True)

    assert reader.find_transcript(cwd=str(project), session_id=SESSION_C) == parent.resolve()


def test_unknown_metadata_requires_trusted_selection_but_user_fork_remains_eligible(tmp_path):
    root = tmp_path / ".codex" / "sessions"
    project = tmp_path / "project"
    project.mkdir()
    unknown = write_rollout(
        root,
        SESSION_A,
        project,
        at="01-19-02",
        text="future primary",
        metadata={"thread_source": "api", "source": {"future": {}}},
    )
    fork = write_rollout(
        root,
        SESSION_B,
        project,
        at="01-19-46",
        text="user fork",
        metadata={
            "thread_source": "user",
            "source": "cli",
            "forked_from_id": SESSION_A,
        },
    )
    reader = CodexObserver(root=root)

    assert reader.find_transcript(cwd=str(project)) == fork
    admitted = reader.admit_operator_candidate(cwd=str(project), candidate=str(unknown))
    assert admitted.location == str(unknown.resolve())
    exact = CodexObserver(root=root, session_exact=True)
    assert exact.find_transcript(cwd=str(project), session_id=SESSION_A) == unknown
