"""The adopted codex pane that process proof cannot attribute.

No exact per-pane evidence exists inside plugin bounds, so the harness reports
an actionable no-attribution diagnostic on the adopted path only; spawned
participants keep today's quiet first-turn wait.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from shipped import CodexHarness

from theater import proc
from theater.daemon.observer import Observer
from theater.daemon.registry import Registry
from theater.harness.builtin.plugins.codex.observer import CodexObserver
from theater.models import ParticipantOrigin, Status, Tier

SESSION = "01a00cdf-17f9-7851-99a4-b0dbaad18bed"
PID = 74065


def _rollout(root: Path, cwd: Path) -> Path:
    day = root / "2026" / "08" / "17"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-2026-08-17T01-19-02-{SESSION}.jsonl"
    path.write_text(
        json.dumps(
            {
                "timestamp": "2026-08-17T01:19:02.000Z",
                "type": "session_meta",
                "payload": {"id": SESSION, "cwd": str(cwd)},
            }
        )
        + "\n"
    )
    return path


def _stub_proc(monkeypatch, *, comm: str, held: list[Path]) -> None:
    monkeypatch.setattr(proc, "comm", lambda pid: comm)
    monkeypatch.setattr(proc, "open_files", lambda pid: list(held))


def _observer(root: Path, *, adopted: bool) -> CodexObserver:
    return CodexObserver(root=root, pane_pid=PID, participant_scoped=True, adopted=adopted)


@pytest.fixture
def codex_tree(tmp_path):
    root = tmp_path / ".codex" / "sessions"
    root.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()
    return {"root": root, "project": project, "rollout": _rollout(root, project)}


def test_an_unproven_adopted_pane_reports_the_limitation(monkeypatch, codex_tree):
    _stub_proc(monkeypatch, comm="codex", held=[codex_tree["project"]])
    reader = _observer(codex_tree["root"], adopted=True)

    assert reader.find_transcript(cwd=str(codex_tree["project"])) is None
    error = reader.process_identity_error
    assert error is not None
    assert "No eligible rollout" in error
    assert "no direct-process ownership was proved" in error
    assert "managed app-server backend" in error
    assert "operator candidate admission" in error


def test_the_diagnostic_clears_when_the_pane_proves_its_rollout(monkeypatch, codex_tree):
    _stub_proc(monkeypatch, comm="codex", held=[codex_tree["project"]])
    reader = _observer(codex_tree["root"], adopted=True)
    reader.find_transcript(cwd=str(codex_tree["project"]))
    assert reader.process_identity_error is not None

    monkeypatch.setattr(proc, "open_files", lambda pid: [codex_tree["rollout"]])
    found = reader.find_transcript(cwd=str(codex_tree["project"]))

    assert found == codex_tree["rollout"].resolve()
    assert reader.process_identity_error is None


def test_a_spawned_first_turn_wait_stays_silent(monkeypatch, codex_tree):
    _stub_proc(monkeypatch, comm="codex", held=[codex_tree["project"]])
    reader = _observer(codex_tree["root"], adopted=False)

    assert reader.find_transcript(cwd=str(codex_tree["project"])) is None
    assert reader.process_identity_error is None


def test_the_diagnostic_needs_no_sessions_directory(monkeypatch, tmp_path):
    _stub_proc(monkeypatch, comm="codex", held=[tmp_path])
    reader = _observer(tmp_path / ".codex" / "sessions", adopted=True)

    assert reader.find_transcript(cwd=str(tmp_path)) is None
    assert reader.process_identity_error is not None

    spawned = _observer(tmp_path / ".codex" / "sessions", adopted=False)
    spawned.find_transcript(cwd=str(tmp_path))
    assert spawned.process_identity_error is None


def test_an_adopted_shell_pane_is_not_claimed(monkeypatch, codex_tree):
    _stub_proc(monkeypatch, comm="zsh", held=[codex_tree["rollout"]])
    reader = _observer(codex_tree["root"], adopted=True)

    assert reader.find_transcript(cwd=str(codex_tree["project"])) is None
    assert reader.process_identity_error is None


async def test_daemon_observation_carries_adopted_into_the_source(
    monkeypatch, registry: Registry, codex_tree
):
    """The real daemon path: tier ADOPTED reaches the plugin through the context."""
    _stub_proc(monkeypatch, comm="codex", held=[codex_tree["project"]])
    harness = CodexHarness(root=codex_tree["root"])
    watcher = Observer(registry, {"codex": harness})
    participant = registry.create_spawned(
        harness="codex",
        cwd=str(codex_tree["project"]),
        tier=Tier.ADOPTED,
        origin=ParticipantOrigin.ADOPTED,
    )
    participant.pid = PID
    registry.store.upsert_participant(participant)

    source = watcher._open_source(participant.id, harness.observer)
    assert source is not None
    try:
        batch = await source.read()
    finally:
        await source.aclose()

    assert batch.error_code == "transcript_correlation_ambiguous"
    assert "no direct-process ownership was proved" in (batch.error or "")
    assert registry.get(participant.id).status is not Status.DEAD
