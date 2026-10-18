"""Codex first-send wait and adopted-pane diagnostics bind the pane's own rollout."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from tests.support.codex_rollouts import hold, write_rollout
from tests.support.shipped import CodexHarness
from tests.support.waiting import until
from theater import proc
from theater.daemon.frontend.control_handlers import controls_get
from theater.daemon.frontend.participant_read_handlers import participant_to_wire
from theater.daemon.observer import Observer
from theater.daemon.registry import Registry
from theater.daemon.rpc.controls import _controls
from theater.harness.builtin.plugins.codex.observer import CodexObserver
from theater.models import JobState, ParticipantOrigin, Status, Tier, TranscriptUntrusted, now

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


def _spawn_siblings(daemon, terminal_provider, root, project, held):
    children = []
    rollouts = []
    for index in range(4):
        child = daemon.registry.create_spawned(harness="codex", cwd=str(project))
        terminal_id = terminal_provider.bind(daemon, child.id, command="codex")
        terminal = terminal_provider.terminals[-1]
        terminal_provider.screens[terminal_id] = "› "
        session_id = str(uuid4())
        held[terminal.process_id] = []
        if index < 3:
            rollout = write_rollout(
                root, session_id, project, at=f"01-19-0{index}", text=f"sibling-{index}"
            )
            held[terminal.process_id] = [rollout]
            rollouts.append(rollout)
        children.append((child, terminal, session_id))
    return children, rollouts


def _binding_generation(daemon, child, previous, current):
    with daemon.store.write_unit() as unit:
        assert daemon.store.terminal_bindings.restore_generation(
            child.id,
            previous_generation=previous,
            provider_generation=current,
            report_revision=1,
            health="healthy",
            updated_at=now(),
            connection=unit.connection,
        )


def _completed_rollout(root, session_id, project):
    own = write_rollout(root, session_id, project, at="01-19-03", text="LUNA3_OK")
    with own.open("a") as stream:
        stream.write(
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {"type": "task_complete", "last_agent_message": "LUNA3_OK"},
                }
            )
            + "\n"
        )
    return own


@pytest.mark.parametrize("reconnecting", [False, True], ids=["connected", "reconnected"])
async def test_delayed_first_send_waits_for_its_own_transcript(
    daemon, terminal_provider, tmp_path, monkeypatch, reconnecting
):
    root = tmp_path / "sessions"
    root.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    watcher = daemon.observer
    watcher.harnesses["codex"] = CodexHarness(root=root)
    watcher.search = watcher.poll = watcher.sync = 0.01
    held = {}
    asked = hold(monkeypatch, held)
    children, rollouts = _spawn_siblings(daemon, terminal_provider, root, project, held)

    child, terminal, session_id = children[-1]
    if reconnecting:
        _binding_generation(daemon, child, 1, 0)
    watcher.start()
    assert await until(lambda: watcher.transcript_pending(child.id))
    if reconnecting:
        assert terminal.process_id not in asked.open_files
        original_watch = watcher._tasks[child.id]
        _binding_generation(daemon, child, 0, 1)
        assert await until(lambda: terminal.process_id in asked.open_files)
        assert watcher._tasks[child.id] is not original_watch
    # Probe entry precedes publication of the replacement watch's first batch.
    assert await until(
        lambda: (
            watcher.transcript_pending(child.id)
            and all(daemon.registry.get(p.id).session_id for p, _, _ in children[:3])
        )
    )

    async def identities():
        views = [
            await participant_to_wire(daemon, daemon.registry.get(child.id)),
            await controls_get(daemon, None, {"participant_id": child.id}),
            await _controls(daemon, {"target": child.id}),
        ]
        snapshot = daemon.state_service.snapshot(f"startup-test-{uuid4()}", page_size=500)
        views.extend(
            item for item in snapshot["participants"] if item["participant_id"] == child.id
        )
        return [view["transcript_identity"] for view in views]

    assert {(value["state"], value.get("pending")) for value in await identities()} == {
        ("missing", True)
    }
    # Multiple process-owned root rollouts are a real conflict, not ordinary startup.
    held[terminal.process_id] = rollouts[:2]
    assert await until(lambda: watcher.transcript_correlation_ambiguous(child.id))
    assert {value["state"] for value in await identities()} == {"ambiguous"}
    with pytest.raises(TranscriptUntrusted):
        await daemon.controls.send(child.id, caller_id="cli", prompt="must not be delivered")
    assert not terminal_provider.deliveries

    held[terminal.process_id] = []
    assert await until(
        lambda: (
            watcher.transcript_pending(child.id)
            and not watcher.transcript_correlation_ambiguous(child.id)
        )
    )
    active_watch = watcher._tasks[child.id]
    job = await daemon.controls.send(child.id, caller_id="cli", prompt="reply LUNA3_OK")
    assert terminal_provider.deliveries == [(terminal.terminal_id, "reply LUNA3_OK")]
    prior_polls = asked.open_files.count(terminal.process_id)
    assert await until(lambda: asked.open_files.count(terminal.process_id) > prior_polls + 1)
    assert daemon.jobs.get(job.handle).state == JobState.RUNNING
    assert watcher._tasks[child.id] is active_watch

    own = _completed_rollout(root, session_id, project)
    held[terminal.process_id] = [own]
    assert await until(lambda: daemon.jobs.get(job.handle).state == JobState.DONE)
    assert daemon.registry.get(child.id).session_id == session_id
    assert daemon.registry.get(child.id).transcript_location == str(own.resolve())
    assert {value["state"] for value in await identities()} == {"trusted"}
    assert "LUNA3_OK" in daemon.jobs.get(job.handle).result
    assert "sibling" not in daemon.jobs.get(job.handle).result
