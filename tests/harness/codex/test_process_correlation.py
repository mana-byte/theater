"""Codex transcript correlation through the pane's own process: proof and precedence."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from tests.support.codex_process_correlation import PID_A, PID_B, SESSION_A, SESSION_B, history_of
from tests.support.codex_rollouts import hold, write_rollout
from tests.support.shipped import CodexHarness
from theater import proc
from theater.daemon.observer import Observer
from theater.daemon.registry import Registry
from theater.harness.builtin.plugins.codex.observer import CodexObserver
from theater.harness.transcript import open_participant_source
from theater.models import ProviderRecord, TerminalBindingRecord
from theater.provenance import TranscriptProvenance

#: The shell a pane runs when the session was adopted rather than spawned.
PID_SHELL = 74001


async def test_the_held_rollout_wins_over_the_newest_cwd_match(monkeypatch, rollout_pair):
    """The cwd scan would return B for everyone; the process says A."""
    hold(monkeypatch, {PID_A: [rollout_pair["a"]]})
    reader = CodexObserver(root=rollout_pair["root"], pane_pid=PID_A)

    found = reader.find_transcript(cwd=str(rollout_pair["project"]))

    assert found == rollout_pair["a"].resolve()


async def test_a_held_rollout_is_reported_as_proven(monkeypatch, rollout_pair):
    """Process proof is trusted, distinct from an exact launch/session id."""
    hold(monkeypatch, {PID_A: [rollout_pair["a"]]})
    reader = CodexObserver(root=rollout_pair["root"], pane_pid=PID_A)

    history = await history_of(reader, rollout_pair["project"])

    assert history.correlation == "proven"
    assert history.location == str(rollout_pair["a"].resolve())


def test_established_process_proof_reuses_os_discovery(monkeypatch, rollout_pair):
    """Repeated proof reads reuse the exact process-owned rollout until invalidated."""
    calls: list[list[str]] = []
    started_at = [123.0]

    def check_output(argv, **kwargs):
        calls.append(list(argv))
        if argv[1] == "-p":
            return "codex\n"
        return f"  PID  PPID COMM\n{PID_A} 1 codex\n"

    def run(argv, **kwargs):
        calls.append(list(argv))
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=f"p{PID_A}\nf12\nn{rollout_pair['a']}\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "check_output", check_output)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(proc, "process_started_at", lambda pid: started_at[0])
    reader = CodexObserver(root=rollout_pair["root"], pane_pid=PID_A)

    assert reader.proven_transcript(cwd=str(rollout_pair["project"])) == rollout_pair["a"].resolve()
    assert reader.proven_transcript(cwd=str(rollout_pair["project"])) == rollout_pair["a"].resolve()

    assert calls == [
        ["ps", "-p", str(PID_A), "-o", "comm="],
        ["lsof", "-n", "-P", "-p", str(PID_A), "-F", "n"],
    ]

    started_at[0] = 124.0
    assert reader.proven_transcript(cwd=str(rollout_pair["project"])) == rollout_pair["a"].resolve()
    assert sum(call[0] == "lsof" for call in calls) == 2
    assert all(call[:2] != ["ps", "-eo"] for call in calls)


async def test_two_siblings_each_read_their_own_transcript(monkeypatch, rollout_pair):
    """The whole point: one cwd, one root, two agents, two right answers."""
    hold(monkeypatch, {PID_A: [rollout_pair["a"]], PID_B: [rollout_pair["b"]]})
    harness = CodexHarness(root=rollout_pair["root"])

    first = open_participant_source(
        harness.observer, participant_id="one", cwd=str(rollout_pair["project"]), pane_pid=PID_A
    )
    second = open_participant_source(
        harness.observer, participant_id="two", cwd=str(rollout_pair["project"]), pane_pid=PID_B
    )
    try:
        assert (first._observer.pane_pid, second._observer.pane_pid) == (PID_A, PID_B)
        assert first._observer.proves_ownership is True
        one = await first.history(last_n=0)
        two = await second.history(last_n=0)
    finally:
        await first.aclose()
        await second.aclose()

    assert [event.text for event in one.events if event.text] == ["agent A"]
    assert [event.text for event in two.events if event.text] == ["agent B"]
    assert one.correlation == two.correlation == "proven"


async def test_process_evidence_outranks_a_session_id_we_were_given(monkeypatch, rollout_pair):
    """A persisted id may itself be an earlier guess, so it does not go first.

    This is the upgrade path for a participant that attached heuristically
    before Theater could ask the process: taking the id's glob first would
    re-derive the same wrong file on every poll, forever.
    """
    hold(monkeypatch, {PID_A: [rollout_pair["a"]]})
    reader = CodexObserver(root=rollout_pair["root"], pane_pid=PID_A)

    found = reader.find_transcript(cwd=str(rollout_pair["project"]), session_id=SESSION_B)

    assert found == rollout_pair["a"].resolve()


async def test_the_birth_time_floor_does_not_veto_process_evidence(monkeypatch, rollout_pair):
    """The floor is a proxy for ownership; the open file is the thing itself.

    A resumed session's rollout predates the participant that resumed it, and
    it is still that participant's rollout.
    """
    hold(monkeypatch, {PID_A: [rollout_pair["a"]]})
    reader = CodexObserver(root=rollout_pair["root"], pane_pid=PID_A)
    tomorrow = rollout_pair["a"].stat().st_mtime + 86_400

    found = reader.find_transcript(cwd=str(rollout_pair["project"]), after=tomorrow)

    assert found == rollout_pair["a"].resolve()


@pytest.mark.parametrize("sibling_exists", [False, True])
async def test_one_source_recovers_when_the_rollout_finally_appears(
    monkeypatch, tmp_path, sibling_exists
):
    """A delayed first prompt must not make a sibling's rollout look like a conflict."""
    root = tmp_path / ".codex" / "sessions"
    root.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    if sibling_exists:
        write_rollout(root, SESSION_B, project, at="01-19-01", text="sibling")
    held: dict[int, list[Path]] = {PID_A: []}
    hold(monkeypatch, held)
    reader = CodexObserver(root=root, pane_pid=PID_A)
    source = reader.open_source(cwd=str(project))

    try:
        waiting = await source.read()
        assert waiting.waiting and waiting.attached is None and waiting.error_code is None
        assert (await source.history(last_n=0)).location is None

        rollout = write_rollout(root, SESSION_A, project, at="01-19-02", text="agent A")
        held[PID_A] = [rollout]
        second = await source.history(last_n=0)
    finally:
        await source.aclose()

    assert second.correlation == "proven"
    assert second.location == str(rollout.resolve())


async def test_an_id_we_were_given_outranks_the_process(monkeypatch, rollout_pair):
    """A resume token names the file outright; nothing beats it, not even us.

    The mirror of the test above. There the id had been read back off a file
    an earlier guess picked, so the process had to be asked first; here the id
    is a launch receipt, and asking the process could only introduce doubt —
    a second codex in the pane, a pid the kernel has since reissued.
    """
    asked = hold(monkeypatch, {PID_A: [rollout_pair["a"]]})
    reader = CodexObserver(
        root=rollout_pair["root"], pane_pid=PID_A, session_provenance=TranscriptProvenance.EXACT
    )

    found = reader.find_transcript(cwd=str(rollout_pair["project"]), session_id=SESSION_B)

    assert found == rollout_pair["b"]
    assert asked.anything == []


async def test_an_id_we_were_given_that_names_nothing_falls_through(monkeypatch, rollout_pair):
    """An exact id for a rollout that does not exist yet is not a dead end.

    A deliberate choice of availability over caution, and worth naming as one:
    an exact id that matches nothing is also evidence — the session it names
    has not written its rollout — and a fail-closed adapter could reasonably
    answer nothing and wait. Codex writes the file a moment after launch, so
    waiting would blind the watcher for that window on every spawn. Falling
    through is safe because it cannot manufacture confidence: the weaker
    channels are labelled as what they are, and the test below holds them to
    it.
    """
    hold(monkeypatch, {PID_A: [rollout_pair["a"]]})
    reader = CodexObserver(
        root=rollout_pair["root"], pane_pid=PID_A, session_provenance=TranscriptProvenance.EXACT
    )

    found = reader.find_transcript(
        cwd=str(rollout_pair["project"]),
        session_id="01a00cdf-0000-0000-0000-000000000000",
    )

    assert found == rollout_pair["a"].resolve()


async def test_missing_exact_id_does_not_fall_back_to_a_sibling(monkeypatch, rollout_pair):
    """No rollout from the recorded process means pending, never a same-cwd guess."""
    hold(monkeypatch, {PID_A: []})
    reader = CodexObserver(root=rollout_pair["root"], pane_pid=PID_A)

    history = await history_of(
        reader,
        rollout_pair["project"],
        session_id="01a00cdf-0000-0000-0000-000000000000",
        session_provenance=TranscriptProvenance.EXACT,
    )

    assert history.location is None
    assert history.correlation == "heuristic"


async def test_a_pane_process_that_is_not_codex_is_not_evidence(monkeypatch, rollout_pair):
    """Holding a rollout open is not evidence unless the holder is the CLI.

    Descriptors travel from parent to child and never back, so a shell does
    *not* pick up the rollout its codex opens after the fork — but a wrapper
    that opened or kept the file itself does hold it, and so does anything the
    operator arranged to. That process is not a codex session, and if a codex
    session exists it is somewhere below, where the pane root cannot say which
    one. So a pane process that is not codex is not asked what it holds.
    """
    hold(
        monkeypatch,
        {PID_SHELL: [rollout_pair["a"]]},
        comms={PID_SHELL: "-zsh"},
    )
    reader = CodexObserver(root=rollout_pair["root"], pane_pid=PID_SHELL)

    history = await history_of(reader, rollout_pair["project"])

    assert history.correlation == "heuristic"
    assert not reader.proved(rollout_pair["a"])


async def test_an_adopted_pane_gets_no_proof_from_its_descendants(monkeypatch, rollout_pair):
    """A shell outlives what it ran, so what runs under it now proves nothing.

    Searching an adopted pane's descendants is the version of this that looks
    right and is not: finding exactly one codex under the shell shows that one
    codex is running there *now*, not that it is the session the participant
    was adopted from. The operator can have quit the first and started a
    second, and the second's rollout would be proved as the first's. Counting
    cannot answer a question about identity over time, so nothing is claimed.
    """
    hold(
        monkeypatch,
        {PID_A: [rollout_pair["a"]]},
        comms={PID_SHELL: "-zsh", PID_A: "codex"},
        beneath=[(PID_A, "codex")],
    )
    reader = CodexObserver(root=rollout_pair["root"], pane_pid=PID_SHELL)

    history = await history_of(reader, rollout_pair["project"])

    assert history.correlation == "heuristic"
    assert not reader.proved(rollout_pair["a"])


async def test_a_codex_below_the_pane_process_is_never_reached(monkeypatch, rollout_pair):
    """A spawned pane *is* the CLI, so anything under it is somebody else.

    Codex can launch codex — as a sub-agent, or because the agent typed it.
    That session's rollout is not this participant's, and the participant's own
    process is the only one asked.
    """
    hold(
        monkeypatch,
        {PID_B: [rollout_pair["b"]]},
        comms={PID_A: "codex", PID_B: "codex"},
        beneath=[(PID_B, "codex")],
    )
    reader = CodexObserver(root=rollout_pair["root"], pane_pid=PID_A)

    history = await history_of(reader, rollout_pair["project"])

    assert history.correlation == "heuristic"
    assert not reader.proved(rollout_pair["b"])


@pytest.mark.parametrize("provider_bound", [False, True])
async def test_both_siblings_bind_and_neither_job_is_refused(
    monkeypatch, registry: Registry, rollout_pair, provider_bound
):
    """End to end: the collision guard admits two exact, distinct claims.

    Before process evidence this pair produced two `transcript_correlation_
    ambiguous` crashes and no transcript binding at all.
    """
    from tests.support.waiting import until

    hold(monkeypatch, {PID_A: [rollout_pair["a"]], PID_B: [rollout_pair["b"]]})
    watcher = Observer(
        registry,
        {"codex": CodexHarness(root=rollout_pair["root"])},
        poll=0.01,
        search=0.01,
        sync=0.01,
    )

    async def capture(_pane):
        return "› "

    watcher._capture = capture
    first = registry.register(harness="codex", pane=None, cwd=str(rollout_pair["project"]))
    first.pid = PID_A
    registry.store.upsert_participant(first)
    second = registry.register(harness="codex", pane=None, cwd=str(rollout_pair["project"]))
    second.pid = PID_B
    registry.store.upsert_participant(second)
    if provider_bound:
        with registry.store.write_unit() as unit:
            registry.store.providers.register(
                ProviderRecord(
                    provider_id="codex-provider",
                    selector="codex-provider",
                    kind="fixture",
                    credential_verifier="a" * 64,
                    configuration_version=1,
                    capabilities=("terminal-provider.v1",),
                    limits={},
                    generation=1,
                    last_report_revision=1,
                    created_at=1.0,
                    updated_at=1.0,
                ),
                connection=unit.connection,
            )
            for participant in (first, second):
                registry.store.terminal_bindings.bind(
                    TerminalBindingRecord(
                        participant_id=participant.id,
                        provider_id="codex-provider",
                        provider_generation=1,
                        terminal_id=participant.id,
                        terminal_incarnation="incarnation",
                        occupant_evidence={"id": participant.id},
                        process_facts={"pid": participant.pid},
                        health="healthy",
                        report_revision=1,
                        created_at=1.0,
                        updated_at=1.0,
                    ),
                    connection=unit.connection,
                )
                participant.pid = None
                registry.store.upsert_participant(participant, connection=unit.connection)

    watcher.start()
    try:
        assert await until(lambda: len(watcher._bound_transcripts) == 2)
        # Snapshot before closing: teardown releases every binding it holds.
        bound = {pid: location for location, pid in watcher._bound_transcripts.items()}
    finally:
        await watcher.aclose()

    assert bound[first.id] == str(rollout_pair["a"].resolve())
    assert bound[second.id] == str(rollout_pair["b"].resolve())
    assert registry.get(first.id).session_correlation == "proven"
    assert registry.get(second.id).session_correlation == "proven"
    assert registry.get(first.id).session_id == SESSION_A
    assert registry.get(second.id).session_id == SESSION_B


async def test_probe_identity_loss_populates_session_id(monkeypatch, rollout_pair):
    """The probe returns the harness session_id it read off the candidate."""
    os.utime(rollout_pair["a"], ns=(1_000_000_000, 1_000_000_000))
    hold(monkeypatch, {PID_A: []})
    reader = CodexObserver(
        root=rollout_pair["root"], pane_pid=PID_A, session_provenance=TranscriptProvenance.OPERATOR
    )
    source = reader.open_source(
        cwd=str(rollout_pair["project"]),
        session_id=SESSION_A,
        session_provenance=TranscriptProvenance.OPERATOR,
        known_location=str(rollout_pair["a"]),
    )
    initial = await source.read()
    assert initial.attached is not None
    source.commit_attachment()

    # SESSION_B is already newer than SESSION_A in the fixture.
    os.utime(rollout_pair["b"], ns=(2_000_000_000, 2_000_000_000))

    evidence = await source.probe_identity_loss()
    assert evidence is not None
    assert evidence.session_id == SESSION_B


async def test_probe_identity_loss_session_id_none_when_candidate_has_no_session_id(
    monkeypatch, rollout_pair, tmp_path
):
    """A candidate whose filename has no UUID tail leaves evidence.session_id None.

    Not a tautological dataclass test: this exercises the real probe path with
    a genuine None-session candidate — a file whose ``session_meta`` carries
    the right cwd (so ``_transcript_cwd`` matches and the candidate is found)
    but whose name lacks the UUID stem pattern (so ``session_id`` returns None).
    """
    os.utime(rollout_pair["a"], ns=(1_000_000_000, 1_000_000_000))
    # Make SESSION_B older than SESSION_A so the probe does not find it.
    os.utime(rollout_pair["b"], ns=(500_000_000, 500_000_000))
    hold(monkeypatch, {PID_A: []})
    reader = CodexObserver(
        root=rollout_pair["root"], pane_pid=PID_A, session_provenance=TranscriptProvenance.OPERATOR
    )
    source = reader.open_source(
        cwd=str(rollout_pair["project"]),
        session_id=SESSION_A,
        session_provenance=TranscriptProvenance.OPERATOR,
        known_location=str(rollout_pair["a"]),
    )
    initial = await source.read()
    assert initial.attached is not None
    source.commit_attachment()

    # A rollout whose session_meta has the right cwd but whose filename
    # does not match the _STEM UUID pattern, so session_id() returns None.
    day = rollout_pair["root"] / "2026" / "08" / "17"
    no_sid = day / "rollout-broken.jsonl"
    no_sid.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-17T01:21:00.000Z",
                "type": "session_meta",
                "payload": {"id": "no-uuid-here", "cwd": str(rollout_pair["project"])},
            }
        )
        + "\n",
    )
    os.utime(no_sid, ns=(2_000_000_000, 2_000_000_000))

    evidence = await source.probe_identity_loss()
    assert evidence is not None, "probe must find the no-session candidate"
    assert evidence.location == str(no_sid.resolve())
    assert evidence.session_id is None
