"""Provider first-send bootstrap in a populated shared Codex transcript directory."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from shipped import CodexHarness

from tests.test_codex_process_correlation import _rollout, hold
from tests.test_observer import until
from theater.daemon.frontend.control_handlers import controls_get
from theater.daemon.frontend.participant_read_handlers import participant_to_wire
from theater.daemon.rpc.controls import _controls
from theater.models import JobState, TranscriptUntrusted, now


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
            rollout = _rollout(
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
    own = _rollout(root, session_id, project, at="01-19-03", text="LUNA3_OK")
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
