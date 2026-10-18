"""Process evidence admissibility and the OS facts behind it: open files, lsof, snapshots."""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.support.codex_process_correlation import PID_A, PID_B, SESSION_A, history_of
from tests.support.codex_rollouts import hold, write_rollout
from tests.support.shipped import CodexHarness
from theater import proc
from theater.daemon.observer import Observer
from theater.daemon.registry import Registry
from theater.harness.builtin.plugins.codex.observer import CodexObserver
from theater.models import Status


async def test_a_file_outside_the_transcript_root_is_not_evidence(
    monkeypatch, codex_tree, tmp_path
):
    """A rollout-shaped name elsewhere on disk proves nothing about this root."""
    stray = write_rollout(
        tmp_path / "elsewhere", SESSION_A, codex_tree["project"], at="01-19-02", text="x"
    )
    hold(monkeypatch, {PID_A: [stray]})
    reader = CodexObserver(root=codex_tree["root"], pane_pid=PID_A)

    history = await history_of(reader, codex_tree["project"])

    assert history.correlation == "heuristic"
    assert not reader.proved(stray)


async def test_a_rollout_for_another_directory_is_not_evidence(monkeypatch, codex_tree, tmp_path):
    """`session_meta.cwd` has to agree, or the process is in a different job."""
    other = tmp_path / "other"
    other.mkdir()
    foreign = write_rollout(
        codex_tree["root"],
        "01a00cdf-dead-beef-9a9f-67e9ebe0e2b2",
        other,
        at="01-20-00",
        text="somewhere else",
    )
    hold(monkeypatch, {PID_A: [foreign]})
    reader = CodexObserver(root=codex_tree["root"], pane_pid=PID_A)

    history = await history_of(reader, codex_tree["project"])

    assert history.correlation == "heuristic"
    assert history.location != str(foreign.resolve())


async def test_an_ordinary_open_file_is_not_evidence(monkeypatch, codex_tree):
    """Codex has the repo open too; only a rollout name is considered."""
    ordinary = codex_tree["project"] / "notes.jsonl"
    ordinary.write_text("{}\n")
    hold(monkeypatch, {PID_A: [ordinary]})
    reader = CodexObserver(root=codex_tree["root"], pane_pid=PID_A)

    history = await history_of(reader, codex_tree["project"])

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

    history = await history_of(reader, codex_tree["project"])

    assert history.correlation == "heuristic"
    assert asked.comm == [PID_A]
    assert asked.open_files == []


async def test_without_a_pid_the_operating_system_is_never_asked(monkeypatch, codex_tree):
    """An external participant has no pane, so there is no process to inspect."""
    asked = hold(monkeypatch, {PID_A: [codex_tree["a"]]})
    reader = CodexObserver(root=codex_tree["root"], pane_pid=None)

    history = await history_of(reader, codex_tree["project"])

    assert asked.anything == []
    assert history.correlation == "heuristic"


def test_live_pid_is_withheld_once_a_participant_is_dead(registry: Registry, tmp_path):
    """The number outlives the process, and the kernel hands it out again."""
    p = registry.register(harness="codex", pane=None, cwd=str(tmp_path))
    p.pid = PID_A
    registry.store.upsert_participant(p)

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

    p = registry.register(harness="codex", pane=None, cwd=str(tmp_path))
    p.pid = PID_A
    registry.store.upsert_participant(p)
    watcher = Observer(registry, {"codex": CodexHarness(root=tmp_path)}, source_factory=spy)

    watcher._open_source(p.id, CodexObserver(root=tmp_path))
    assert seen["pane_pid"] == PID_A

    registry.set_status(p.id, Status.DEAD)
    watcher._open_source(p.id, CodexObserver(root=tmp_path))
    assert seen["pane_pid"] is None


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
