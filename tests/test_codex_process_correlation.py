"""Codex transcript correlation through the pane's own process.

The failure this covers: several codex agents in one working directory. Codex
mints its session id internally and files every rollout under one machine-wide
root, so cwd-and-birth-time discovery matches all of them at once. The
reducer's collision guard then refuses every candidate — correctly, because
attaching one participant to a sibling's transcript would report the wrong
status, answer the wrong await, and serve the wrong `read_transcript` — and the
participants are left with no observation channel at all.

The exact channel is the file the codex process is holding open. These tests
stub `theater.proc`, because the alternative is launching real codex sessions:
what is under test is the correlation policy, not whether `lsof` works.

The two `theater.proc` parsers get their own tests at the bottom. The `/proc`
half reads a real directory of real symlinks, so it would notice a change in
how those read back; the `lsof` half is fed a hand-written string and so tests
only the parse. Neither can confirm the premise the policy rests on — that a
live codex names itself `codex` and holds exactly one rollout open. That was
checked by hand against codex 0.146.0 and is the thing to re-check when a codex
release changes how it writes its rollout.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from shipped import CodexHarness

from theater import proc
from theater.daemon import observer as observer_mod
from theater.daemon.observer import Observer
from theater.daemon.registry import Registry
from theater.harness.builtin.plugins.codex.observer import CodexObserver
from theater.harness.transcript import open_participant_source
from theater.models import Status
from theater.provenance import TranscriptProvenance

# Two sessions born a minute apart, so "newest" is unambiguous and a test that
# means to select the older one is visibly not just getting lucky.
SESSION_A = "01a00cdf-17f9-7851-99a4-b0dbaad18bed"
SESSION_B = "01a00cdf-c496-7f92-8429-d75394c3edfb"
SESSION_C = "01a00cdf-ffff-7f92-8429-d75394c3edfc"
PID_A = 74065
PID_B = 74675
#: The shell a pane runs when the session was adopted rather than spawned.
PID_SHELL = 74001


def _rollout(root: Path, session_id: str, cwd: Path, *, at: str, text: str) -> Path:
    """One codex rollout, filed the way codex files them.

    `~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<local ISO>-<session id>.jsonl`,
    opening with the `session_meta` record that carries the cwd.
    """
    day = root / "2026" / "08" / "17"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-08-17T{at}-{session_id}.jsonl"
    path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "timestamp": "2026-08-17T01:19:02.000Z",
                        "type": "session_meta",
                        "payload": {"id": session_id, "cwd": str(cwd)},
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-08-17T01:19:03.000Z",
                        "type": "event_msg",
                        "payload": {"type": "agent_message", "message": text},
                    }
                ),
            ]
        )
        + "\n"
    )
    return path


@pytest.fixture
def codex_tree(tmp_path):
    """A codex session root with two rollouts sharing one working directory."""
    root = tmp_path / ".codex" / "sessions"
    root.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    older = _rollout(root, SESSION_A, project, at="01-19-02", text="agent A")
    newer = _rollout(root, SESSION_B, project, at="01-19-46", text="agent B")
    return {"root": root, "project": project, "a": older, "b": newer}


class Asked:
    """Which pids each `theater.proc` helper was asked about, in order.

    One list per helper rather than one between them: "the operating system was
    never asked" and "the process was identified but never opened" are
    different claims, and a recorder that pooled them could not tell a test
    which of the two it had proved.
    """

    def __init__(self) -> None:
        self.comm: list[int] = []
        self.open_files: list[int] = []
        self.descendants: list[int] = []

    @property
    def anything(self) -> list[int]:
        return self.comm + self.open_files + self.descendants


def hold(
    monkeypatch,
    held: dict[int, list[Path]],
    *,
    comm: str = "codex",
    comms: dict[int, str] | None = None,
    beneath: list[tuple[int, str]] | None = None,
) -> Asked:
    """Pretend each pid in *held* is a codex process holding those files open.

    *comm* is what `ps` reports for the pane process; the default is the shape
    of a pane Theater spawned, where the pane process is the CLI. Pass *comms*
    to give different pids different commands, and *beneath* to put processes
    under the pane — which nothing should ever ask about, and which is stubbed
    here precisely so that a version which did would be caught.

    *beneath* is `(pid, comm)` pairs because that is what `proc.descendants`
    returns. A stub of a different shape would make a version that searched the
    tree fail on the shape rather than on the attribution, which is the one
    thing these tests must not mistake for a pass.

    Returns the record of what the probe asked the operating system, so a test
    can assert that a participant with no pid, or one whose answer was already
    exact, never reaches it at all.
    """
    asked = Asked()

    def process_comm(pid: int) -> str:
        asked.comm.append(pid)
        if comms is not None:
            return comms.get(pid, "")
        return comm

    def open_files(pid: int) -> list[Path]:
        asked.open_files.append(pid)
        return list(held.get(pid, ()))

    def descendants(pid: int) -> list[tuple[int, str]]:
        asked.descendants.append(pid)
        return list(beneath or ())

    monkeypatch.setattr(proc, "comm", process_comm)
    monkeypatch.setattr(proc, "open_files", open_files)
    monkeypatch.setattr(proc, "descendants", descendants)
    return asked


async def _history(observer: CodexObserver, cwd: Path, **kwargs):
    source = observer.open_source(cwd=str(cwd), **kwargs)
    try:
        return await source.history(last_n=0)
    finally:
        await source.aclose()


# ---- the process names the transcript ------------------------------------


async def test_the_held_rollout_wins_over_the_newest_cwd_match(monkeypatch, codex_tree):
    """The cwd scan would return B for everyone; the process says A."""
    hold(monkeypatch, {PID_A: [codex_tree["a"]]})
    reader = CodexObserver(root=codex_tree["root"], pane_pid=PID_A)

    found = reader.find_transcript(cwd=str(codex_tree["project"]))

    assert found == codex_tree["a"].resolve()


async def test_a_held_rollout_is_reported_as_proven(monkeypatch, codex_tree):
    """Process proof is trusted, distinct from an exact launch/session id."""
    hold(monkeypatch, {PID_A: [codex_tree["a"]]})
    reader = CodexObserver(root=codex_tree["root"], pane_pid=PID_A)

    history = await _history(reader, codex_tree["project"])

    assert history.correlation == "proven"
    assert history.location == str(codex_tree["a"].resolve())


async def test_two_siblings_each_read_their_own_transcript(monkeypatch, codex_tree):
    """The whole point: one cwd, one root, two agents, two right answers."""
    hold(monkeypatch, {PID_A: [codex_tree["a"]], PID_B: [codex_tree["b"]]})
    harness = CodexHarness(root=codex_tree["root"])

    first = open_participant_source(
        harness.observer, participant_id="one", cwd=str(codex_tree["project"]), pane_pid=PID_A
    )
    second = open_participant_source(
        harness.observer, participant_id="two", cwd=str(codex_tree["project"]), pane_pid=PID_B
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


async def test_process_evidence_outranks_a_session_id_we_were_given(monkeypatch, codex_tree):
    """A persisted id may itself be an earlier guess, so it does not go first.

    This is the upgrade path for a participant that attached heuristically
    before Theater could ask the process: taking the id's glob first would
    re-derive the same wrong file on every poll, forever.
    """
    hold(monkeypatch, {PID_A: [codex_tree["a"]]})
    reader = CodexObserver(root=codex_tree["root"], pane_pid=PID_A)

    found = reader.find_transcript(cwd=str(codex_tree["project"]), session_id=SESSION_B)

    assert found == codex_tree["a"].resolve()


async def test_the_birth_time_floor_does_not_veto_process_evidence(monkeypatch, codex_tree):
    """The floor is a proxy for ownership; the open file is the thing itself.

    A resumed session's rollout predates the participant that resumed it, and
    it is still that participant's rollout.
    """
    hold(monkeypatch, {PID_A: [codex_tree["a"]]})
    reader = CodexObserver(root=codex_tree["root"], pane_pid=PID_A)
    tomorrow = codex_tree["a"].stat().st_mtime + 86_400

    found = reader.find_transcript(cwd=str(codex_tree["project"]), after=tomorrow)

    assert found == codex_tree["a"].resolve()


async def test_one_source_recovers_when_the_rollout_finally_appears(monkeypatch, tmp_path):
    """Codex writes the rollout on its first turn, not at startup.

    One source across both calls, and nothing to find on the first: a watcher
    polls the source it already holds, so the retry has to work without
    anything being rebuilt. Starting from an empty root is the point — with a
    sibling's rollout already on disk the first call finds *that* and the test
    would pass without any retry happening.
    """
    root = tmp_path / ".codex" / "sessions"
    root.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    held: dict[int, list[Path]] = {PID_A: []}
    hold(monkeypatch, held)
    reader = CodexObserver(root=root, pane_pid=PID_A)
    source = reader.open_source(cwd=str(project))

    try:
        assert (await source.history(last_n=0)).location is None

        rollout = _rollout(root, SESSION_A, project, at="01-19-02", text="agent A")
        held[PID_A] = [rollout]
        second = await source.history(last_n=0)
    finally:
        await source.aclose()

    assert second.correlation == "proven"
    assert second.location == str(rollout.resolve())


async def test_an_id_we_were_given_outranks_the_process(monkeypatch, codex_tree):
    """A resume token names the file outright; nothing beats it, not even us.

    The mirror of the test above. There the id had been read back off a file
    an earlier guess picked, so the process had to be asked first; here the id
    is a launch receipt, and asking the process could only introduce doubt —
    a second codex in the pane, a pid the kernel has since reissued.
    """
    asked = hold(monkeypatch, {PID_A: [codex_tree["a"]]})
    reader = CodexObserver(
        root=codex_tree["root"], pane_pid=PID_A, session_provenance=TranscriptProvenance.EXACT
    )

    found = reader.find_transcript(cwd=str(codex_tree["project"]), session_id=SESSION_B)

    assert found == codex_tree["b"]
    assert asked.anything == []


async def test_an_id_we_were_given_that_names_nothing_falls_through(monkeypatch, codex_tree):
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
    hold(monkeypatch, {PID_A: [codex_tree["a"]]})
    reader = CodexObserver(
        root=codex_tree["root"], pane_pid=PID_A, session_provenance=TranscriptProvenance.EXACT
    )

    found = reader.find_transcript(
        cwd=str(codex_tree["project"]),
        session_id="01a00cdf-0000-0000-0000-000000000000",
    )

    assert found == codex_tree["a"].resolve()


async def test_falling_through_an_exact_id_never_makes_the_guess_exact(monkeypatch, codex_tree):
    """The floor under that choice: no id, no process, so no confidence.

    The dangerous version of the fall-through is the one where the receipt
    misses, the process proves nothing, and the cwd scan quietly hands back a
    sibling — or an older session of this participant's own — with the
    exactness of the receipt still attached to it. The location may be wrong
    here, and that is allowed; claiming to be sure of it is not.
    """
    hold(monkeypatch, {PID_A: []})
    reader = CodexObserver(root=codex_tree["root"], pane_pid=PID_A)

    history = await _history(
        reader,
        codex_tree["project"],
        session_id="01a00cdf-0000-0000-0000-000000000000",
        session_provenance=TranscriptProvenance.EXACT,
    )

    assert history.location == str(codex_tree["b"])
    assert history.correlation == "heuristic"


# ---- only the pane's own process is asked ---------------------------------


async def test_a_pane_process_that_is_not_codex_is_not_evidence(monkeypatch, codex_tree):
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
        {PID_SHELL: [codex_tree["a"]]},
        comms={PID_SHELL: "-zsh"},
    )
    reader = CodexObserver(root=codex_tree["root"], pane_pid=PID_SHELL)

    history = await _history(reader, codex_tree["project"])

    assert history.correlation == "heuristic"
    assert not reader.proved(codex_tree["a"])


async def test_an_adopted_pane_gets_no_proof_from_its_descendants(monkeypatch, codex_tree):
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
        {PID_A: [codex_tree["a"]]},
        comms={PID_SHELL: "-zsh", PID_A: "codex"},
        beneath=[(PID_A, "codex")],
    )
    reader = CodexObserver(root=codex_tree["root"], pane_pid=PID_SHELL)

    history = await _history(reader, codex_tree["project"])

    assert history.correlation == "heuristic"
    assert not reader.proved(codex_tree["a"])


async def test_a_codex_below_the_pane_process_is_never_reached(monkeypatch, codex_tree):
    """A spawned pane *is* the CLI, so anything under it is somebody else.

    Codex can launch codex — as a sub-agent, or because the agent typed it.
    That session's rollout is not this participant's, and the participant's own
    process is the only one asked.
    """
    hold(
        monkeypatch,
        {PID_B: [codex_tree["b"]]},
        comms={PID_A: "codex", PID_B: "codex"},
        beneath=[(PID_B, "codex")],
    )
    reader = CodexObserver(root=codex_tree["root"], pane_pid=PID_A)

    history = await _history(reader, codex_tree["project"])

    assert history.correlation == "heuristic"
    assert not reader.proved(codex_tree["b"])


# ---- a location admitted before any of this existed -----------------------


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

    history = await _history(reader, codex_tree["project"], known_location=str(codex_tree["b"]))

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

    history = await _history(reader, codex_tree["project"], known_location=str(codex_tree["a"]))

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
    newer = _rollout(
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
    newer = _rollout(
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


async def test_committing_a_guess_gives_up_the_claim_that_the_id_was_exact(monkeypatch, codex_tree):
    """A guessed location must not be able to launder itself into proof.

    An exact id whose rollout is not on disk, and no process to ask: discovery
    falls through to the cwd scan and the reducer may accept that candidate
    when nothing competes for it. Committing copies the *found* file's id over
    the exact one — after which the id matches trivially, and a source that
    still called itself exact would report proof for the guess, outranking
    real evidence later.
    """
    hold(monkeypatch, {PID_A: []})
    reader = CodexObserver(
        root=codex_tree["root"], pane_pid=PID_A, session_provenance=TranscriptProvenance.EXACT
    )
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

    history = await _history(
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

    history = await _history(
        reader,
        codex_tree["project"],
        session_id=SESSION_B,
        session_provenance=TranscriptProvenance.EXACT,
        known_location=str(codex_tree["b"]),
    )

    assert history.location == str(codex_tree["b"])
    assert history.correlation == "exact"
    assert asked.anything == []


# ---- what is not evidence -------------------------------------------------


async def test_two_open_rollouts_are_not_evidence(monkeypatch, codex_tree, caplog):
    """One codex session holds one rollout. Two means we are guessing again."""
    hold(monkeypatch, {PID_A: [codex_tree["a"], codex_tree["b"]]})
    reader = CodexObserver(root=codex_tree["root"], pane_pid=PID_A)

    with caplog.at_level("WARNING"):
        history = await _history(reader, codex_tree["project"])

    assert history.correlation == "heuristic"
    assert "declining to pick one" in caplog.text


async def test_a_file_outside_the_transcript_root_is_not_evidence(
    monkeypatch, codex_tree, tmp_path
):
    """A rollout-shaped name elsewhere on disk proves nothing about this root."""
    stray = _rollout(
        tmp_path / "elsewhere", SESSION_A, codex_tree["project"], at="01-19-02", text="x"
    )
    hold(monkeypatch, {PID_A: [stray]})
    reader = CodexObserver(root=codex_tree["root"], pane_pid=PID_A)

    history = await _history(reader, codex_tree["project"])

    assert history.correlation == "heuristic"
    assert not reader.proved(stray)


async def test_a_rollout_for_another_directory_is_not_evidence(monkeypatch, codex_tree, tmp_path):
    """`session_meta.cwd` has to agree, or the process is in a different job."""
    other = tmp_path / "other"
    other.mkdir()
    foreign = _rollout(
        codex_tree["root"],
        "01a00cdf-dead-beef-9a9f-67e9ebe0e2b2",
        other,
        at="01-20-00",
        text="somewhere else",
    )
    hold(monkeypatch, {PID_A: [foreign]})
    reader = CodexObserver(root=codex_tree["root"], pane_pid=PID_A)

    history = await _history(reader, codex_tree["project"])

    assert history.correlation == "heuristic"
    assert history.location != str(foreign.resolve())


async def test_an_ordinary_open_file_is_not_evidence(monkeypatch, codex_tree):
    """Codex has the repo open too; only a rollout name is considered."""
    ordinary = codex_tree["project"] / "notes.jsonl"
    ordinary.write_text("{}\n")
    hold(monkeypatch, {PID_A: [ordinary]})
    reader = CodexObserver(root=codex_tree["root"], pane_pid=PID_A)

    history = await _history(reader, codex_tree["project"])

    assert history.correlation == "heuristic"


async def test_a_non_codex_process_in_the_pane_is_not_opened(monkeypatch, codex_tree):
    """Identified, then dropped: its files are never read at all.

    The companion to the test above, which shows that a non-codex root yields
    no proof. This one shows *where* it stops — `ps` is asked what the process
    is, and once the answer is not codex the fds are never listed. A version
    that read them and then discarded the result would reach the same verdict
    by doing the work anyway, and would be one edit away from trusting it.

    `ps comm` is an absolute path here because that is how it reports a binary
    invoked by path, which the name check has to tolerate.
    """
    asked = hold(monkeypatch, {PID_A: [codex_tree["a"]]}, comm="/bin/zsh")
    reader = CodexObserver(root=codex_tree["root"], pane_pid=PID_A)

    history = await _history(reader, codex_tree["project"])

    assert history.correlation == "heuristic"
    assert asked.comm == [PID_A]
    assert asked.open_files == []


async def test_without_a_pid_the_operating_system_is_never_asked(monkeypatch, codex_tree):
    """An external participant has no pane, so there is no process to inspect."""
    asked = hold(monkeypatch, {PID_A: [codex_tree["a"]]})
    reader = CodexObserver(root=codex_tree["root"], pane_pid=None)

    history = await _history(reader, codex_tree["project"])

    assert asked.anything == []
    assert history.correlation == "heuristic"


# ---- a dead participant's pid belongs to someone else now -----------------


def test_live_pid_is_withheld_once_a_participant_is_dead(registry: Registry, tmp_path):
    """The number outlives the process, and the kernel hands it out again."""
    p = registry.register(harness="codex", pane="%1", cwd=str(tmp_path))
    registry.attach_pane(p.id, "%1", pane_pid=PID_A)

    alive = registry.get(p.id)
    assert alive.live_pid == PID_A

    registry.set_status(p.id, Status.DEAD)
    assert registry.get(p.id).live_pid is None


def test_the_watcher_passes_the_live_pid_to_the_adapter(monkeypatch, registry: Registry, tmp_path):
    """Wiring: `_open_source` is where the pid reaches a harness plugin."""
    seen: dict = {}

    def spy(observer, **kwargs):
        seen.update(kwargs)
        return CodexObserver(root=tmp_path).open_source(cwd=kwargs["cwd"])

    monkeypatch.setattr(observer_mod, "open_participant_source", spy)
    p = registry.register(harness="codex", pane="%1", cwd=str(tmp_path))
    registry.attach_pane(p.id, "%1", pane_pid=PID_A)
    watcher = Observer(registry, {"codex": CodexHarness(root=tmp_path)})

    watcher._open_source(p.id, CodexObserver(root=tmp_path))
    assert seen["pane_pid"] == PID_A

    registry.set_status(p.id, Status.DEAD)
    watcher._open_source(p.id, CodexObserver(root=tmp_path))
    assert seen["pane_pid"] is None


# ---- the reducer accepts what the process proved --------------------------


async def test_both_siblings_bind_and_neither_job_is_refused(
    monkeypatch, registry: Registry, codex_tree
):
    """End to end: the collision guard admits two exact, distinct claims.

    Before process evidence this pair produced two `transcript_correlation_
    ambiguous` crashes and no transcript binding at all.
    """
    from tests.test_observer import until

    hold(monkeypatch, {PID_A: [codex_tree["a"]], PID_B: [codex_tree["b"]]})
    watcher = Observer(
        registry,
        {"codex": CodexHarness(root=codex_tree["root"])},
        poll=0.01,
        search=0.01,
        sync=0.01,
    )

    async def capture(_pane):
        return "› "

    watcher._capture = capture
    first = registry.register(harness="codex", pane="%1", cwd=str(codex_tree["project"]))
    registry.attach_pane(first.id, "%1", pane_pid=PID_A)
    second = registry.register(harness="codex", pane="%2", cwd=str(codex_tree["project"]))
    registry.attach_pane(second.id, "%2", pane_pid=PID_B)

    watcher.start()
    try:
        assert await until(lambda: len(watcher._bound_transcripts) == 2)
        # Snapshot before closing: teardown releases every binding it holds.
        bound = {pid: location for location, pid in watcher._bound_transcripts.items()}
    finally:
        await watcher.aclose()

    assert bound[first.id] == str(codex_tree["a"].resolve())
    assert bound[second.id] == str(codex_tree["b"].resolve())
    assert registry.get(first.id).session_correlation == "proven"
    assert registry.get(second.id).session_correlation == "proven"
    assert registry.get(first.id).session_id == SESSION_A
    assert registry.get(second.id).session_id == SESSION_B


# ---- theater.proc, against the real filesystem ----------------------------


def test_proc_fd_reports_files_and_skips_everything_else(tmp_path):
    """The Linux branch: `/proc/<pid>/fd` is a directory of symlinks."""
    target = tmp_path / "rollout.jsonl"
    target.write_text("{}\n")
    fds = tmp_path / "fd"
    fds.mkdir()
    (fds / "3").symlink_to(target)
    # A socket reads back as a name that is not a path at all.
    (fds / "4").symlink_to("socket:[12345]")
    # A link whose target is gone still names a path; it is simply not there.
    (fds / "5").symlink_to(tmp_path / "gone.jsonl")

    found = proc._proc_open_files(fds)

    assert target in found
    assert not any("socket" in str(path) for path in found)


def test_proc_fd_of_an_unreadable_directory_is_no_evidence(tmp_path):
    found = proc._proc_open_files(tmp_path / "does-not-exist")
    assert found == []


def test_lsof_field_output_is_parsed_to_absolute_paths(monkeypatch):
    """The macOS branch: `-F n` is one field per line, prefixed by its letter."""
    output = (
        "p74065\n"
        "fcwd\nn/Users/x/project\n"
        "f12\nn/Users/x/.codex/sessions/r.jsonl\n"
        "f13\nn->1.2.3.4:443\n"
    )

    def run(argv, **kwargs):
        assert argv[0] == "lsof"
        assert "-n" in argv and "-P" in argv
        return subprocess.CompletedProcess(argv, 1, stdout=output, stderr="")

    monkeypatch.setattr(subprocess, "run", run)

    found = proc._lsof_open_files(PID_A)

    assert found == [Path("/Users/x/project"), Path("/Users/x/.codex/sessions/r.jsonl")]


def test_a_missing_lsof_is_no_evidence_rather_than_an_error(monkeypatch):
    """A machine with neither `/proc` nor `lsof` keeps the old behaviour."""

    def run(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(subprocess, "run", run)
    assert proc._lsof_open_files(PID_A) == []


def test_a_process_table_we_cannot_read_yields_no_descendants(monkeypatch):
    def check_output(argv, **kwargs):
        raise OSError("ps is not available")

    monkeypatch.setattr(subprocess, "check_output", check_output)
    assert proc.descendants(PID_A) == []


# ---- theater.proc.ProcessSnapshot: one `ps`, reused across roots ----------

_PS_TABLE = (
    f"  PID  PPID COMM\n{PID_A} 1 zsh\n{PID_B} {PID_A} vibe\n9001 {PID_B} node\n9002 1 unrelated\n"
)


def test_process_snapshot_captures_once_and_serves_multiple_roots(monkeypatch):
    """The whole point of the snapshot: one `ps`, walked for two different roots."""
    calls = []

    def check_output(argv, **kwargs):
        calls.append(argv)
        return _PS_TABLE

    monkeypatch.setattr(subprocess, "check_output", check_output)

    snapshot = proc.ProcessSnapshot.capture()
    assert len(calls) == 1

    assert snapshot.descendants(PID_A) == [(PID_B, "vibe"), (9001, "node")]
    assert snapshot.descendants(9002) == []
    # Still one `ps` call: the same parsed table served both roots.
    assert len(calls) == 1


def test_process_snapshot_walk_is_breadth_first_with_cycle_protection(monkeypatch):
    """A malformed table that cycles back must not hang the walk."""
    table = "  PID  PPID COMM\n100 200 a\n200 100 b\n"
    monkeypatch.setattr(subprocess, "check_output", lambda argv, **kwargs: table)

    snapshot = proc.ProcessSnapshot.capture()

    assert snapshot.descendants(100) == [(200, "b")]


def test_process_snapshot_capture_failure_is_no_evidence(monkeypatch):
    """An unreadable `ps` yields an empty snapshot, not an exception."""

    def check_output(argv, **kwargs):
        raise OSError("ps is not available")

    monkeypatch.setattr(subprocess, "check_output", check_output)

    snapshot = proc.ProcessSnapshot.capture()

    assert snapshot.descendants(PID_A) == []


def test_descendants_still_captures_a_fresh_snapshot_per_call(monkeypatch):
    """`proc.descendants` must keep capturing fresh state — no hidden cache."""
    calls = []

    def check_output(argv, **kwargs):
        calls.append(argv)
        return _PS_TABLE

    monkeypatch.setattr(subprocess, "check_output", check_output)

    proc.descendants(PID_A)
    proc.descendants(PID_A)

    assert len(calls) == 2


def test_process_snapshot_comm_reads_root_from_parsed_table(monkeypatch):
    """``snapshot.comm(pid)`` returns the process name from the already-parsed
    table — no second ``ps``.  The whole point of the ``_comms`` map: a caller
    that captured a snapshot for a descendant walk can also read root comms
    from it for free.
    """
    root_pid = 50000
    table = f"  PID  PPID COMM\n{root_pid} 1 opencode\n50001 {root_pid} node\n"

    calls: list[list[str]] = []

    def check_output(argv, **kwargs):
        calls.append(list(argv))
        return table

    monkeypatch.setattr(subprocess, "check_output", check_output)

    snapshot = proc.ProcessSnapshot.capture()
    assert len(calls) == 1
    assert calls[0] == ["ps", "-eo", "pid,ppid,comm"]

    assert snapshot.comm(root_pid) == "opencode"

    # Repeated reads stay at one ps — comm() reads the parsed table.
    assert snapshot.comm(root_pid) == "opencode"
    assert snapshot.comm(50001) == "node"
    assert snapshot.comm(99999) == ""
    _ = snapshot.descendants(root_pid)
    _ = snapshot.descendants(root_pid)
    assert len(calls) == 1


def test_detect_harness_reads_root_comm_from_snapshot(monkeypatch):
    """``detect_harness`` uses ``snapshot.comm()`` for the root check when a
    snapshot is supplied — no ``ps -p`` fork.  A pane whose foreground is
    ``python3.12`` but whose root process is ``opencode`` resolves via the
    snapshot with zero additional subprocess spawns.
    """
    from theater.config import Config
    from theater.daemon.harness_detect import detect_harness
    from theater.harness import install

    install(Config())

    root_pid = 50000
    table = f"  PID  PPID COMM\n{root_pid} 1 opencode\n50001 {root_pid} node\n"

    calls: list[list[str]] = []

    def check_output(argv, **kwargs):
        calls.append(list(argv))
        return table

    monkeypatch.setattr(subprocess, "check_output", check_output)

    snapshot = proc.ProcessSnapshot.capture()
    assert len(calls) == 1

    # detect_harness("python3.12", root_pid, snapshot) must find "opencode"
    # via snapshot.comm(root_pid) — the foreground is "python3.12" (no match),
    # and descendants are "node" (no match), but the root IS "opencode".
    result = detect_harness("python3.12", root_pid, snapshot=snapshot)
    assert result == "opencode"

    # Still only one ps — no per-pane fork.
    assert len(calls) == 1


# ---- IdentityLossEvidence carries the session_id the source already knows ---


async def test_probe_identity_loss_populates_session_id(monkeypatch, codex_tree):
    """The probe returns the harness session_id it read off the candidate."""
    os.utime(codex_tree["a"], ns=(1_000_000_000, 1_000_000_000))
    hold(monkeypatch, {PID_A: []})
    reader = CodexObserver(
        root=codex_tree["root"], pane_pid=PID_A, session_provenance=TranscriptProvenance.OPERATOR
    )
    source = reader.open_source(
        cwd=str(codex_tree["project"]),
        session_id=SESSION_A,
        session_provenance=TranscriptProvenance.OPERATOR,
        known_location=str(codex_tree["a"]),
    )
    initial = await source.read()
    assert initial.attached is not None
    source.commit_attachment()

    # SESSION_B is already newer than SESSION_A in the fixture.
    os.utime(codex_tree["b"], ns=(2_000_000_000, 2_000_000_000))

    evidence = await source.probe_identity_loss()
    assert evidence is not None
    assert evidence.session_id == SESSION_B


async def test_probe_identity_loss_session_id_none_when_candidate_has_no_session_id(
    monkeypatch, codex_tree, tmp_path
):
    """A candidate whose filename has no UUID tail leaves evidence.session_id None.

    Not a tautological dataclass test: this exercises the real probe path with
    a genuine None-session candidate — a file whose ``session_meta`` carries
    the right cwd (so ``_transcript_cwd`` matches and the candidate is found)
    but whose name lacks the UUID stem pattern (so ``session_id`` returns None).
    """
    os.utime(codex_tree["a"], ns=(1_000_000_000, 1_000_000_000))
    # Make SESSION_B older than SESSION_A so the probe does not find it.
    os.utime(codex_tree["b"], ns=(500_000_000, 500_000_000))
    hold(monkeypatch, {PID_A: []})
    reader = CodexObserver(
        root=codex_tree["root"], pane_pid=PID_A, session_provenance=TranscriptProvenance.OPERATOR
    )
    source = reader.open_source(
        cwd=str(codex_tree["project"]),
        session_id=SESSION_A,
        session_provenance=TranscriptProvenance.OPERATOR,
        known_location=str(codex_tree["a"]),
    )
    initial = await source.read()
    assert initial.attached is not None
    source.commit_attachment()

    # A rollout whose session_meta has the right cwd but whose filename
    # does not match the _STEM UUID pattern, so session_id() returns None.
    day = codex_tree["root"] / "2026" / "08" / "17"
    no_sid = day / "rollout-broken.jsonl"
    no_sid.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-17T01:21:00.000Z",
                "type": "session_meta",
                "payload": {"id": "no-uuid-here", "cwd": str(codex_tree["project"])},
            }
        )
        + "\n",
    )
    os.utime(no_sid, ns=(2_000_000_000, 2_000_000_000))

    evidence = await source.probe_identity_loss()
    assert evidence is not None, "probe must find the no-session candidate"
    assert evidence.location == str(no_sid.resolve())
    assert evidence.session_id is None
