"""Pi's isolated session launch, JSONL observation, and safe resume contract."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from theater.daemon.spawner import Spawner, SpawnRequest
from theater.daemon.store import Store
from theater.daemon.trajectory.project import fact_to_record
from theater.harness.base import APPROVALS
from theater.harness.builtin.plugins.pi.constants import (
    PI_ISOLATION_MARKER,
    PI_RECORD_BYTES,
    PI_SWITCH_MARKER,
    PI_SWITCH_MARKER_VERSION,
)
from theater.harness.builtin.plugins.pi.launch import (
    participant_root,
    plan_launch,
    resume_launch_overlay,
)
from theater.harness.builtin.plugins.pi.manifest import MANIFEST
from theater.harness.builtin.plugins.pi.observer import PiObserver
from theater.harness.builtin.plugins.pi.screen import classify_screen
from theater.harness.contracts.callbacks import LaunchContext, ResumeContext, ScreenContext
from theater.harness.contracts.events import EventKind
from theater.harness.contracts.observation import ScreenConfidence, ScreenKind
from theater.models import Participant, Status
from theater.resume_floor import UNKNOWN_FLOOR, encode_floor
from theater.trajectory.enums import TrajectoryKind, TrajectoryLane, TrajectoryStatus
from theater.trajectory.tools import tool_operations_for_records


def _session(
    *,
    session_id: str,
    cwd: Path,
    timestamp: str = "2026-08-31T12:00:00.000Z",
    parent_session: Path | None = None,
) -> dict[str, object]:
    session: dict[str, object] = {
        "type": "session",
        "version": 3,
        "id": session_id,
        "timestamp": timestamp,
        "cwd": str(cwd),
    }
    if parent_session is not None:
        session["parentSession"] = str(parent_session.resolve())
    return session


def _message(entry_id: str, message: dict[str, object]) -> dict[str, object]:
    return {
        "type": "message",
        "id": entry_id,
        "timestamp": "2026-08-31T12:00:01.000Z",
        "message": message,
    }


def _append(path: Path, *records: dict[str, object]) -> None:
    with path.open("a", encoding="utf-8") as stream:
        for record in records:
            stream.write(json.dumps(record) + "\n")


def _mark_switch(
    root: Path,
    *,
    previous: Path,
    target: Path,
    reason: str,
    offset: int | None = None,
    records: int | None = None,
) -> None:
    if reason == "new":
        offset = records = 0
        dev = ino = None
    elif target.exists():
        stat = target.stat()
        offset = stat.st_size if offset is None else offset
        records = None
        dev, ino = stat.st_dev, stat.st_ino
    else:
        dev = ino = None
    (root / PI_SWITCH_MARKER).write_text(
        json.dumps(
            {
                "version": PI_SWITCH_MARKER_VERSION,
                "reason": reason,
                "location": str(target.resolve()),
                "previous_location": str(previous.resolve()),
                "offset": offset,
                "records": records,
                "dev": dev,
                "ino": ino,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _mark_startup_fork(
    root: Path, *, previous: Path, target: Path, offset: int | None = None
) -> None:
    stat = target.stat()
    (root / PI_SWITCH_MARKER).write_text(
        json.dumps(
            {
                "version": PI_SWITCH_MARKER_VERSION,
                "reason": "startup-fork",
                "location": str(target.resolve()),
                "previous_location": str(previous.resolve()),
                "offset": stat.st_size if offset is None else offset,
                "dev": stat.st_dev,
                "ino": stat.st_ino,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_pi_launch_isolated_session_config_and_all_approval_modes(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    config_path = tmp_path / "mcp.json"

    plan = plan_launch(
        LaunchContext(
            participant_id="pi-child",
            prompt="inspect this",
            config_path=config_path,
            approval="yolo",
            model="openai/gpt-5.6",
            reasoning_effort="high",
        )
    )

    session_dir = tmp_path / "theater-home" / "observations" / "pi" / "pi-child" / "sessions"
    assert plan.argv == [
        "pi",
        "--extension",
        str(Path(__file__).parents[1] / "theater/harness/builtin/plugins/pi/theater_mcp_bridge.ts"),
        "--session-id",
        "pi-child",
        "--session-dir",
        str(session_dir),
        "--theater-mcp-config",
        str(config_path),
        "--model",
        "openai/gpt-5.6",
        "--thinking",
        "high",
        "inspect this",
    ]
    assert plan.session_id == "pi-child"
    assert plan.transcript_domain == str(session_dir.resolve())
    assert set(plan.files) == {config_path, session_dir / PI_ISOLATION_MARKER}
    config = json.loads(plan.files[config_path])
    assert config["mcpServers"]["theater"]["args"] == [
        "mcp",
        "--id",
        "pi-child",
        "--harness",
        "pi",
    ]
    assert MANIFEST.launch.approvals == APPROVALS
    assert plan.env["PI_OFFLINE"] == "1"
    assert plan.env["PI_CACHE_RETENTION"] == "long"
    assert plan.env["PI_SKIP_VERSION_CHECK"] == "1"
    assert plan.env["PI_TELEMETRY"] == "0"


async def test_pi_resume_spawn_forks_to_fresh_native_identity_and_domain(
    tmp_path, monkeypatch, registry, fake_tmux
) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    monkeypatch.setattr("theater.daemon.spawning.service.shutil.which", lambda b: f"/usr/bin/{b}")
    workdir = tmp_path / "work"
    workdir.mkdir()
    predecessor = registry.register(
        harness="pi", pane=None, cwd=str(workdir), session_id="old-native-id"
    )
    predecessor.session_correlation = "exact"
    domain = participant_root(predecessor.id)
    cold_plan = plan_launch(LaunchContext(predecessor.id, "", tmp_path / "old-config.json", "yolo"))
    domain.mkdir(parents=True)
    marker = domain / PI_ISOLATION_MARKER
    marker.write_text(cold_plan.files[marker], encoding="utf-8")
    transcript = domain / "old.jsonl"
    _append(transcript, _session(session_id="old-native-id", cwd=workdir))
    predecessor.transcript_domain = str(domain.resolve())
    predecessor.transcript_location = str(transcript.resolve())
    registry.store.upsert_participant(predecessor)
    registry.mark_dead(predecessor.id)

    successor = await Spawner(registry).spawn(
        SpawnRequest(
            harness="pi",
            prompt="continue",
            cwd=str(tmp_path / "ignored-cwd"),
            approval="yolo",
            resume="old-native-id",
        )
    )

    command = fake_tmux.windows[-1]["command"]
    successor_domain = participant_root(successor.id)
    assert command[command.index("--session-id") + 1] == successor.id
    assert command[command.index("--session-dir") + 1] == str(successor_domain)
    assert command[command.index("--fork") + 1] == str(transcript.resolve())
    assert successor.session_id == successor.id
    assert successor.transcript_domain == str(successor_domain.resolve())
    assert successor.resumed_from_id == predecessor.id
    assert successor.cwd == str(workdir)
    assert successor_domain != domain


def test_pi_source_waits_until_the_initial_session_file_exists(tmp_path) -> None:
    sessions = tmp_path / "sessions"
    workdir = tmp_path / "work"
    workdir.mkdir()
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id"
    )

    assert asyncio.run(source.read()).waiting is True

    sessions.mkdir()
    transcript = sessions / "2026-08-31T120000_native-id.jsonl"
    _append(transcript, _session(session_id="native-id", cwd=workdir))
    attached = asyncio.run(source.read())
    assert attached.attached is not None
    assert attached.attached.session_id == "native-id"
    source.commit_attachment()

    with transcript.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(_message("user-1", {"role": "user", "content": "hello"})))
    partial = asyncio.run(source.read())
    assert partial.progressed is True
    assert not partial.events

    with transcript.open("a", encoding="utf-8") as stream:
        stream.write("\n")
    batch = asyncio.run(source.read())
    assert [(event.kind, event.text) for event in batch.events] == [(EventKind.USER, "hello")]


def test_pi_header_identity_does_not_depend_on_the_filename(tmp_path) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    transcript = tmp_path / "arbitrary-name.jsonl"
    _append(transcript, _session(session_id="native-id", cwd=workdir))

    observer = PiObserver(root=tmp_path)
    assert observer.session_id(transcript) == "native-id"
    assert observer.find_transcript(cwd=str(workdir), session_id="native-id") == (
        transcript.resolve()
    )


def test_pi_screen_classifier_is_conservative_and_never_raises() -> None:
    assert classify_screen(ScreenContext(capture="Esc to interrupt")).kind is ScreenKind.UNKNOWN
    assert (
        classify_screen(
            ScreenContext(
                capture=(
                    "escape interrupt · ctrl+c/ctrl+d clear/exit\n"
                    "0.0%/1.0M (auto) (mistral) zai-glm-5-2 • high"
                )
            )
        ).kind
        is ScreenKind.UNKNOWN
    )
    idle = classify_screen(ScreenContext(capture="0.0%/1.0M\nTheater: idle"))
    assert idle.kind is ScreenKind.PROMPT
    assert idle.confidence is ScreenConfidence.HIGH
    # A stale idle marker must never beat a real status indicator.
    assert (
        classify_screen(ScreenContext(capture="⠋ Working...\n0.0%/1.0M\nTheater: idle")).kind
        is ScreenKind.WORKING
    )
    # Extension widgets can displace Pi's loader far above the footer.
    assert (
        classify_screen(
            ScreenContext(capture="⠙ Retrying (1/3)\n" + "widget\n" * 12 + "footer")
        ).kind
        is ScreenKind.WORKING
    )
    # Only the final footer-status line can carry the idle marker.
    assert (
        classify_screen(ScreenContext(capture="Theater: idle\nassistant prose")).kind
        is ScreenKind.UNKNOWN
    )
    prompt = classify_screen(ScreenContext(capture="\n❯"))
    assert prompt.kind is ScreenKind.PROMPT
    assert prompt.confidence is ScreenConfidence.LOW
    assert classify_screen(ScreenContext(capture="agent prose only")).kind is ScreenKind.UNKNOWN


def test_pi_bridge_uses_an_instance_lease_for_every_session_lifecycle() -> None:
    """Pi creates a fresh extension instance for every session replacement."""
    bridge = (
        Path(__file__).parents[1] / "theater/harness/builtin/plugins/pi/theater_mcp_bridge.ts"
    ).read_text(encoding="utf-8")

    assert "function acquireBridge(): symbol | undefined" in bridge
    assert "const bridgeLease = acquireBridge();" in bridge
    assert "if (owners[OWNER] === lease) delete owners[OWNER];" in bridge
    assert "await client.close();\n\t\treleaseBridge(bridgeLease);" in bridge


def test_pi_bridge_marks_only_pi_confirmed_idle_states() -> None:
    bridge = (
        Path(__file__).parents[1] / "theater/harness/builtin/plugins/pi/theater_mcp_bridge.ts"
    ).read_text(encoding="utf-8")

    assert 'const IDLE_STATUS_TEXT = "Theater: idle";' in bridge
    assert "ctx.ui.setStatus(IDLE_STATUS_KEY, undefined);" in bridge
    assert "if (ctx.isIdle()) ctx.ui.setStatus(IDLE_STATUS_KEY, IDLE_STATUS_TEXT);" in bridge
    for event in (
        "session_start",
        "before_agent_start",
        "agent_start",
        "agent_settled",
        "session_before_compact",
        "session_compact",
        "session_before_tree",
        "session_tree",
        "session_shutdown",
    ):
        assert f'pi.on("{event}"' in bridge
    # Registering the status protocol cannot depend on owning the MCP bridge:
    # a user-local bridge may already hold that process-wide lease.
    assert bridge.index("registerIdleStatus(pi);") < bridge.index(
        "const bridgeLease = acquireBridge();"
    )


def test_pi_bridge_records_session_switch_boundaries_independently_of_mcp_ownership() -> None:
    bridge = (
        Path(__file__).parents[1] / "theater/harness/builtin/plugins/pi/theater_mcp_bridge.ts"
    ).read_text(encoding="utf-8")

    assert 'const SWITCH_MARKER = ".theater-pi-switch.json";' in bridge
    assert 'pi.on("session_start"' in bridge
    assert 'pi.on("session_shutdown"' in bridge
    assert "writeSwitchMarker(event.reason" in bridge
    assert 'reason: "startup-fork"' in bridge
    assert "if (startupFork) writeStartupForkMarker(startupFork, ctx);" in bridge
    assert bridge.index("registerTranscriptSwitches(pi);") < bridge.index(
        "const bridgeLease = acquireBridge();"
    )


def test_pi_parser_pairs_tools_projects_usage_and_ends_the_turn(tmp_path) -> None:
    observer = PiObserver(root=tmp_path)
    user = observer.parse_record(
        json.dumps(_message("user-1", {"role": "user", "content": "check status"})), 1
    )
    tool_call = observer.parse_record(
        json.dumps(
            _message(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "I should inspect it."},
                        {
                            "type": "toolCall",
                            "id": "call-1",
                            "name": "bash",
                            "arguments": {"command": "git status --short"},
                        },
                    ],
                    "stopReason": "toolUse",
                },
            )
        ),
        2,
    )
    tool_result = observer.parse_record(
        json.dumps(
            _message(
                "tool-1",
                {
                    "role": "toolResult",
                    "toolCallId": "call-1",
                    "toolName": "bash",
                    "content": [{"type": "text", "text": "clean"}],
                },
            )
        ),
        3,
    )
    terminal = observer.parse_record(
        json.dumps(
            _message(
                "assistant-2",
                {
                    "role": "assistant",
                    "model": "gpt-5.6",
                    "content": [{"type": "text", "text": "The tree is clean."}],
                    "stopReason": "stop",
                    "usage": {"input": 7, "output": 4, "reasoning": 2, "cost": {"total": 0.01}},
                },
            )
        ),
        4,
    )

    assert user.events[0].turn_id == "user-1"
    assert user.trajectory[0].status is TrajectoryStatus.COMPLETED
    assert [event.kind for event in tool_call.events] == [EventKind.TOOL_CALL]
    assert tool_call.trajectory[-1].kind is TrajectoryKind.TOOL_CALL
    assert tool_call.trajectory[-1].call_id == "call-1"
    assistant = next(fact for fact in tool_call.trajectory if fact.kind is TrajectoryKind.ASSISTANT)
    assert assistant.status is TrajectoryStatus.COMPLETED
    assert tool_call.trajectory[-1].status is TrajectoryStatus.PENDING
    assert tool_result.events[0].kind is EventKind.TOOL_RESULT
    assert tool_result.trajectory[0].call_id == "call-1"
    assert tool_result.trajectory[0].status is TrajectoryStatus.COMPLETED
    tool_records = tuple(
        fact_to_record(fact, participant_id="pi-child", source_epoch="epoch")
        for parsed in (tool_call, tool_result)
        for fact in parsed.trajectory
        if fact.kind in {TrajectoryKind.TOOL_CALL, TrajectoryKind.TOOL_RESULT}
    )
    assert tool_operations_for_records(tool_records)[0].status is TrajectoryStatus.COMPLETED
    assert terminal.events[-1].turn_end is True
    assert terminal.events[-1].usage is not None
    assert terminal.events[-1].usage.input_tokens == 7
    assert all(fact.status is TrajectoryStatus.COMPLETED for fact in terminal.trajectory)


@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("error", TrajectoryStatus.ERROR),
        ("aborted", TrajectoryStatus.INTERRUPTED),
        (None, TrajectoryStatus.UNKNOWN),
    ],
)
def test_pi_parser_preserves_non_success_assistant_statuses(
    tmp_path, stop_reason, expected
) -> None:
    message: dict[str, object] = {
        "role": "assistant",
        "content": [{"type": "text", "text": "partial"}],
    }
    if stop_reason is not None:
        message["stopReason"] = stop_reason

    parsed = PiObserver(root=tmp_path).parse_record(json.dumps(_message("assistant-1", message)), 1)

    assert parsed.trajectory[0].status is expected


@pytest.mark.parametrize(
    ("name", "identity"),
    [
        ("grafana__query_prometheus", ("grafana", "query_prometheus")),
        ("bash", (None, None)),
        ("__missing_server", (None, None)),
        ("missing_tool__", (None, None)),
    ],
)
def test_pi_parser_identifies_mcp_tool_calls_and_results(tmp_path, name, identity) -> None:
    observer = PiObserver(root=tmp_path)
    call = observer.parse_record(
        json.dumps(
            _message(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "call-1",
                            "name": name,
                            "arguments": {},
                        }
                    ],
                    "stopReason": "toolUse",
                },
            )
        ),
        1,
    )
    result = observer.parse_record(
        json.dumps(
            _message(
                "result-1",
                {
                    "role": "toolResult",
                    "toolCallId": "call-1",
                    "toolName": name,
                    "content": [{"type": "text", "text": "ok"}],
                },
            )
        ),
        2,
    )

    assert (call.trajectory[-1].mcp_server, call.trajectory[-1].mcp_tool) == identity
    assert (result.trajectory[0].mcp_server, result.trajectory[0].mcp_tool) == identity


def test_pi_theater_tools_project_to_theater_activity(tmp_path) -> None:
    observer = PiObserver(root=tmp_path)
    call = observer.parse_record(
        json.dumps(
            _message(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "call-1",
                            "name": "theater__whoami",
                            "arguments": {},
                        }
                    ],
                    "stopReason": "toolUse",
                },
            )
        ),
        1,
    ).trajectory[-1]
    result = observer.parse_record(
        json.dumps(
            _message(
                "result-1",
                {
                    "role": "toolResult",
                    "toolCallId": "call-1",
                    "toolName": "theater__whoami",
                    "content": [{"type": "text", "text": "ok"}],
                },
            )
        ),
        2,
    ).trajectory[0]

    call_record = fact_to_record(call, participant_id="pi-child", source_epoch="epoch")
    result_record = fact_to_record(result, participant_id="pi-child", source_epoch="epoch")
    assert (call_record.kind, call_record.lane, call_record.summary) == (
        TrajectoryKind.THEATER_CALL,
        TrajectoryLane.THEATER,
        "whoami",
    )
    assert (result_record.kind, result_record.lane, result_record.summary) == (
        TrajectoryKind.THEATER_RESULT,
        TrajectoryLane.THEATER,
        "whoami completed",
    )


def test_pi_summary_usage_is_durable_and_inherits_the_active_model(tmp_path) -> None:
    observer = PiObserver(root=tmp_path)
    model_change = observer.parse_record(
        json.dumps(
            {
                "type": "model_change",
                "id": "model-1",
                "modelId": "gpt-5.6",
                "provider": "openai",
            }
        ),
        1,
    )
    assert model_change.trajectory[0].status is TrajectoryStatus.COMPLETED

    summary = observer.parse_record(
        json.dumps(
            {
                "type": "compaction",
                "id": "compact-1",
                "summary": "compacted",
                "usage": {"input": 12, "output": 3, "cost": {"total": 0.01}},
            }
        ),
        2,
    )

    assert len(summary.events) == 1
    assert summary.events[0].usage_only is True
    assert summary.events[0].usage is not None
    assert summary.events[0].usage.idempotency_key == "compact-1"
    assert summary.events[0].usage.model == "gpt-5.6"
    assert summary.events[0].usage.provider == "openai"
    assert all(fact.status is TrajectoryStatus.COMPLETED for fact in summary.trajectory)

    branch = observer.parse_record(
        json.dumps(
            {
                "type": "branch_summary",
                "id": "branch-1",
                "summary": "branched",
                "usage": {"input": 4, "output": 1},
            }
        ),
        3,
    )
    assert branch.events[0].usage_only is True
    assert branch.events[0].usage is not None
    assert branch.events[0].usage.input_tokens == 4
    assert all(fact.status is TrajectoryStatus.COMPLETED for fact in branch.trajectory)


def test_pi_cold_isolated_source_replays_pre_attach_usage_without_an_attach_completion(
    tmp_path,
) -> None:
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    workdir.mkdir()
    sessions.mkdir()
    transcript = sessions / "native-id.jsonl"
    _append(
        transcript,
        _session(session_id="native-id", cwd=workdir),
        _message(
            "assistant-1",
            {
                "role": "assistant",
                "model": "gpt-5.6",
                "content": "done",
                "stopReason": "stop",
                "usage": {"input": 11, "output": 2},
            },
        ),
    )
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id"
    )

    attached = asyncio.run(source.read()).attached
    assert attached is not None
    assert attached.skipped == 0
    assert attached.last_event is None
    source.commit_attachment()

    batch = asyncio.run(source.read())
    usage = [event.usage for event in batch.events if event.usage is not None]
    assert [event.input_tokens for event in usage] == [11]


def test_pi_resumed_source_starts_at_the_durable_usage_floor(tmp_path) -> None:
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    workdir.mkdir()
    sessions.mkdir()
    transcript = sessions / "native-id.jsonl"
    _append(
        transcript,
        _session(session_id="native-id", cwd=workdir),
        _message(
            "assistant-before",
            {
                "role": "assistant",
                "content": "old",
                "stopReason": "stop",
                "usage": {"input": 100, "output": 1},
            },
        ),
    )
    observer = PiObserver(root=sessions, isolated=True)
    floor = observer.stream_floor(str(transcript))
    assert floor is not None
    _append(
        transcript,
        _message(
            "assistant-after",
            {
                "role": "assistant",
                "content": "new",
                "stopReason": "stop",
                "usage": {"input": 7, "output": 2},
            },
        ),
    )
    source = observer.open_source(
        cwd=str(workdir), session_id="native-id", usage_floor=encode_floor(floor)
    )

    attached = asyncio.run(source.read()).attached
    assert attached is not None
    assert attached.skipped == floor.records
    assert attached.last_event is None
    source.commit_attachment()

    batch = asyncio.run(source.read())
    usage = [event.usage for event in batch.events if event.usage is not None]
    assert [event.input_tokens for event in usage] == [7]


def test_pi_unknown_resume_usage_floor_fails_closed(tmp_path) -> None:
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    workdir.mkdir()
    sessions.mkdir()
    transcript = sessions / "native-id.jsonl"
    _append(
        transcript,
        _session(session_id="native-id", cwd=workdir),
        _message(
            "assistant-old",
            {
                "role": "assistant",
                "content": "old",
                "stopReason": "stop",
                "usage": {"input": 100, "output": 1},
            },
        ),
    )
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id", usage_floor=UNKNOWN_FLOOR
    )

    attached = asyncio.run(source.read()).attached
    assert attached is not None
    assert attached.skipped == 2
    source.commit_attachment()
    assert asyncio.run(source.read()).events == ()


def test_pi_restart_replays_only_usage_from_a_known_isolated_transcript(tmp_path) -> None:
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    workdir.mkdir()
    sessions.mkdir()
    transcript = sessions / "native-id.jsonl"
    _append(
        transcript,
        _session(session_id="native-id", cwd=workdir),
        _message(
            "assistant-1",
            {
                "role": "assistant",
                "content": "done",
                "stopReason": "stop",
                "usage": {"input": 5, "output": 1},
            },
        ),
    )
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id", known_location=str(transcript)
    )

    attached = asyncio.run(source.read()).attached
    assert attached is not None
    assert attached.last_event is None
    source.commit_attachment()

    batch = asyncio.run(source.read())
    assert len(batch.events) == 1
    assert batch.events[0].usage_only is True
    assert batch.events[0].usage is not None
    assert batch.events[0].usage.input_tokens == 5
    assert batch.trajectory == ()


def test_pi_restart_usage_reconciliation_is_idempotent_in_the_durable_ledger(tmp_path) -> None:
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    workdir.mkdir()
    sessions.mkdir()
    transcript = sessions / "native-id.jsonl"
    _append(
        transcript,
        _session(session_id="native-id", cwd=workdir),
        _message(
            "assistant-1",
            {
                "role": "assistant",
                "content": "done",
                "stopReason": "stop",
                "usage": {"input": 5, "output": 1},
            },
        ),
    )

    first = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id"
    )
    assert asyncio.run(first.read()).attached is not None
    first.commit_attachment()
    initial = asyncio.run(first.read())

    restarted = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id", known_location=str(transcript)
    )
    assert asyncio.run(restarted.read()).attached is not None
    restarted.commit_attachment()
    catchup = asyncio.run(restarted.read())

    store = Store(tmp_path / "theater.db")
    try:
        participant = Participant(id="participant", harness="pi", session_id="native-id")
        store.upsert_participant(participant)
        for batch in (initial, catchup):
            for event in batch.events:
                if event.usage is None:
                    continue
                usage = event.usage
                assert store.record_usage(
                    participant_id=participant.id,
                    tree_root_id=participant.id,
                    usage_key=f"{participant.session_id}:{usage.idempotency_key}",
                    ts=event.ts or 0,
                    model=usage.model,
                    harness="pi",
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_creation_input_tokens=usage.cache_creation_input_tokens,
                    cache_read_input_tokens=usage.cache_read_input_tokens,
                    reasoning_output_tokens=usage.reasoning_output_tokens,
                    cost_microcents=0,
                ) is (batch is initial)
        assert store.usage_totals()["input_tokens"] == 5
    finally:
        store.close()


def test_pi_restart_resumes_from_the_current_accounting_checkpoint(tmp_path) -> None:
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    workdir.mkdir()
    sessions.mkdir()
    transcript = sessions / "native-id.jsonl"
    _append(
        transcript,
        _session(session_id="native-id", cwd=workdir),
        _message(
            "assistant-1",
            {
                "role": "assistant",
                "content": "one",
                "stopReason": "stop",
                "usage": {"input": 5, "output": 1},
            },
        ),
    )
    first = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id"
    )
    assert asyncio.run(first.read()).attached is not None
    first.commit_attachment()
    assert [
        event.usage.input_tokens for event in asyncio.run(first.read()).events if event.usage
    ] == [5]
    first.acknowledge_accounting_checkpoint()
    checkpoint = first.accounting_checkpoint()
    assert checkpoint is not None

    _append(
        transcript,
        _message(
            "assistant-2",
            {
                "role": "assistant",
                "content": "two",
                "stopReason": "stop",
                "usage": {"input": 7, "output": 1},
            },
        ),
    )
    restarted = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir),
        session_id="native-id",
        known_location=str(transcript),
        usage_checkpoint=checkpoint,
    )
    attached = asyncio.run(restarted.read()).attached
    assert attached is not None
    assert attached.last_event is None
    restarted.commit_attachment()
    batch = asyncio.run(restarted.read())
    assert [event.usage.input_tokens for event in batch.events if event.usage] == [7]
    assert all(event.usage_only for event in batch.events)
    assert batch.trajectory == ()


def test_pi_resume_new_restart_replays_only_the_post_floor_usage(tmp_path) -> None:
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    workdir.mkdir()
    sessions.mkdir()
    old = sessions / "old.jsonl"
    _append(
        old,
        _session(session_id="old", cwd=workdir, timestamp="2020-01-01T00:00:00.000Z"),
        _message(
            "old-a",
            {
                "role": "assistant",
                "content": "old",
                "stopReason": "stop",
                "usage": {"input": 100, "output": 1},
            },
        ),
    )
    observer = PiObserver(root=sessions, isolated=True)
    floor = observer.stream_floor(str(old))
    assert floor is not None
    source = observer.open_source(
        cwd=str(workdir),
        session_id="old",
        after=1_800_000_000,
        usage_floor=encode_floor(floor),
    )
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()
    source.acknowledge_accounting_checkpoint()
    assert asyncio.run(source.read()).events == ()

    fresh = sessions / "fresh.jsonl"
    _append(
        fresh,
        _session(session_id="fresh", cwd=workdir, timestamp="2028-01-01T00:00:00.000Z"),
        _message(
            "new-a",
            {
                "role": "assistant",
                "content": "new",
                "stopReason": "stop",
                "usage": {"input": 7, "output": 1},
            },
        ),
    )
    _mark_switch(sessions, previous=old, target=fresh, reason="new")
    rotated = asyncio.run(source.refresh())
    assert rotated.attached is not None
    source.commit_attachment()
    assert [
        event.usage.input_tokens for event in asyncio.run(source.read()).events if event.usage
    ] == [7]
    source.acknowledge_accounting_checkpoint()
    checkpoint = source.accounting_checkpoint()
    assert checkpoint is not None

    _append(
        fresh,
        _message(
            "new-b",
            {
                "role": "assistant",
                "content": "new after restart",
                "stopReason": "stop",
                "usage": {"input": 9, "output": 1},
            },
        ),
    )
    restarted = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir),
        session_id="fresh",
        known_location=str(fresh),
        usage_checkpoint=checkpoint,
        after=1_800_000_000,
    )
    assert asyncio.run(restarted.read()).attached is not None
    restarted.commit_attachment()
    restarted_batch = asyncio.run(restarted.read())
    assert [event.usage.input_tokens for event in restarted_batch.events if event.usage] == [9]
    assert all(event.usage_only for event in restarted_batch.events)
    assert restarted_batch.trajectory == ()

    _append(
        old,
        _message(
            "old-b",
            {
                "role": "assistant",
                "content": "old again",
                "stopReason": "stop",
                "usage": {"input": 100, "output": 1},
            },
        ),
    )
    assert asyncio.run(source.refresh()).attached is None


def test_pi_fork_switch_skips_copied_history_and_observes_only_new_records(tmp_path) -> None:
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    workdir.mkdir()
    sessions.mkdir()
    old = sessions / "old.jsonl"
    copied = _message(
        "copied-assistant",
        {
            "role": "assistant",
            "content": "already accounted",
            "stopReason": "stop",
            "usage": {"input": 100, "output": 1},
        },
    )
    _append(old, _session(session_id="old", cwd=workdir), copied)
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="old", after=1_800_000_000
    )
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()
    source.acknowledge_accounting_checkpoint()
    assert [
        event.usage.input_tokens for event in asyncio.run(source.read()).events if event.usage
    ] == [100]
    source.acknowledge_accounting_checkpoint()

    fork = sessions / "fork.jsonl"
    _mark_switch(sessions, previous=old, target=fork, reason="fork", records=2)
    _append(
        fork,
        _session(session_id="fork", cwd=workdir),
        copied,
        _message(
            "new-assistant",
            {
                "role": "assistant",
                "content": "new work",
                "stopReason": "stop",
                "usage": {"input": 7, "output": 1},
            },
        ),
    )

    attached = asyncio.run(source.refresh()).attached
    assert attached is not None
    assert attached.session_id == "fork"
    assert attached.skipped == 2
    source.commit_attachment()
    batch = asyncio.run(source.read())
    assert [event.text for event in batch.events if event.text] == ["new work"]
    assert [event.usage.input_tokens for event in batch.events if event.usage] == [7]


def test_pi_startup_fork_waits_for_boundary_and_never_replays_copied_history(tmp_path) -> None:
    workdir = tmp_path / "work"
    predecessor_root = tmp_path / "predecessor"
    successor_root = tmp_path / "successor"
    workdir.mkdir()
    predecessor_root.mkdir()
    successor_root.mkdir()
    predecessor = predecessor_root / "old.jsonl"
    copied = _message(
        "copied-assistant",
        {
            "role": "assistant",
            "content": "already accounted",
            "stopReason": "stop",
            "usage": {"input": 100, "output": 1},
        },
    )
    _append(predecessor, _session(session_id="old", cwd=workdir), copied)
    fork = successor_root / "fork.jsonl"
    _append(
        fork,
        _session(session_id="successor", cwd=workdir, parent_session=predecessor),
        copied,
    )
    copied_offset = fork.stat().st_size
    source = PiObserver(root=successor_root, isolated=True).open_source(
        cwd=str(workdir), session_id="successor"
    )

    # Pi creates the file before extension session_start writes the boundary.
    assert asyncio.run(source.read()).waiting is True

    unrelated = predecessor_root / "unrelated.jsonl"
    _append(unrelated, _session(session_id="unrelated", cwd=workdir))
    _mark_startup_fork(
        successor_root,
        previous=unrelated,
        target=fork,
        offset=copied_offset,
    )
    assert asyncio.run(source.read()).waiting is True

    _mark_startup_fork(
        successor_root,
        previous=predecessor,
        target=fork,
        offset=copied_offset,
    )
    _append(
        fork,
        _message(
            "new-assistant",
            {
                "role": "assistant",
                "content": "new work",
                "stopReason": "stop",
                "usage": {"input": 7, "output": 1},
            },
        ),
    )
    attached = asyncio.run(source.read()).attached
    assert attached is not None
    assert attached.session_id == "successor"
    assert attached.skipped == 2
    source.commit_attachment()

    batch = asyncio.run(source.read())
    assert [event.text for event in batch.events if event.text] == ["new work"]
    assert [event.usage.input_tokens for event in batch.events if event.usage] == [7]


def test_pi_startup_fork_restart_reconciles_only_successor_usage(tmp_path) -> None:
    workdir = tmp_path / "work"
    predecessor_root = tmp_path / "predecessor"
    successor_root = tmp_path / "successor"
    workdir.mkdir()
    predecessor_root.mkdir()
    successor_root.mkdir()
    predecessor = predecessor_root / "old.jsonl"
    copied = _message(
        "copied-assistant",
        {
            "role": "assistant",
            "content": "already accounted",
            "stopReason": "stop",
            "usage": {"input": 100, "output": 1},
        },
    )
    _append(predecessor, _session(session_id="old", cwd=workdir), copied)
    fork = successor_root / "fork.jsonl"
    _append(
        fork,
        _session(session_id="successor", cwd=workdir, parent_session=predecessor),
        copied,
    )
    _mark_startup_fork(successor_root, previous=predecessor, target=fork)
    _append(
        fork,
        _message(
            "new-assistant",
            {
                "role": "assistant",
                "content": "new work",
                "stopReason": "stop",
                "usage": {"input": 7, "output": 1},
            },
        ),
    )
    restarted = PiObserver(root=successor_root, isolated=True).open_source(
        cwd=str(workdir),
        session_id="successor",
        known_location=str(fork),
    )

    assert asyncio.run(restarted.read()).attached is not None
    restarted.commit_attachment()
    batch = asyncio.run(restarted.read())
    assert [event.usage.input_tokens for event in batch.events if event.usage] == [7]
    assert all(event.usage_only for event in batch.events)
    assert batch.trajectory == ()


def test_pi_existing_resume_switch_attaches_at_switch_eof(tmp_path) -> None:
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    workdir.mkdir()
    sessions.mkdir()
    current = sessions / "current.jsonl"
    target = sessions / "existing.jsonl"
    _append(current, _session(session_id="current", cwd=workdir))
    _append(
        target,
        _session(session_id="existing", cwd=workdir, timestamp="2020-01-01T00:00:00.000Z"),
        _message(
            "historical",
            {
                "role": "assistant",
                "content": "historical",
                "stopReason": "stop",
                "usage": {"input": 100, "output": 1},
            },
        ),
    )
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="current", after=1_800_000_000
    )
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()
    source.acknowledge_accounting_checkpoint()
    asyncio.run(source.read())
    source.acknowledge_accounting_checkpoint()

    _mark_switch(sessions, previous=current, target=target, reason="resume", records=2)
    _append(
        target,
        _message(
            "new-on-resume",
            {
                "role": "assistant",
                "content": "after resume",
                "stopReason": "stop",
                "usage": {"input": 13, "output": 2},
            },
        ),
    )
    attached = asyncio.run(source.refresh()).attached
    assert attached is not None
    assert attached.session_id == "existing"
    assert attached.skipped == 2
    source.commit_attachment()
    batch = asyncio.run(source.read())
    assert [event.text for event in batch.events if event.text] == ["after resume"]
    assert [event.usage.input_tokens for event in batch.events if event.usage] == [13]


def test_pi_retries_an_unacknowledged_usage_batch_before_advancing(tmp_path) -> None:
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    workdir.mkdir()
    sessions.mkdir()
    transcript = sessions / "native-id.jsonl"
    _append(transcript, _session(session_id="native-id", cwd=workdir))
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id"
    )
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()
    source.acknowledge_accounting_checkpoint()

    _append(
        transcript,
        _message(
            "lost",
            {
                "role": "assistant",
                "content": "must retry",
                "stopReason": "stop",
                "usage": {"input": 5, "output": 1},
            },
        ),
    )
    failed = asyncio.run(source.read())
    assert [event.usage.input_tokens for event in failed.events if event.usage] == [5]

    source.rollback_accounting_checkpoint()
    _append(
        transcript,
        _message(
            "later",
            {
                "role": "assistant",
                "content": "later",
                "stopReason": "stop",
                "usage": {"input": 7, "output": 1},
            },
        ),
    )
    retried = asyncio.run(source.read())
    assert [event.usage.input_tokens for event in retried.events if event.usage] == [5, 7]


def test_pi_rejected_rotation_keeps_all_active_stream_state(tmp_path, monkeypatch) -> None:
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    workdir.mkdir()
    sessions.mkdir()
    old = sessions / "old.jsonl"
    _append(old, _session(session_id="old", cwd=workdir))
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="old"
    )
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()
    source.acknowledge_accounting_checkpoint()

    _append(
        old,
        _message(
            "old-a",
            {
                "role": "assistant",
                "content": "old",
                "stopReason": "stop",
                "usage": {"input": 5, "output": 1},
            },
        ),
    )
    asyncio.run(source.read())
    source.acknowledge_accounting_checkpoint()

    fresh = sessions / "fresh.jsonl"
    _append(
        fresh,
        _session(session_id="fresh", cwd=workdir),
        _message(
            "candidate-history",
            {
                "role": "assistant",
                "content": "x" * 2_000,
                "stopReason": "stop",
                "usage": {"input": 100, "output": 1},
            },
        ),
    )
    _mark_switch(sessions, previous=old, target=fresh, reason="new")
    monkeypatch.setattr(source, "_replay_cursor", lambda *_args, **_kwargs: (0, 0, True))
    rotation = asyncio.run(source.refresh())
    assert rotation.attached is not None
    source.discard_attachment()

    _append(
        old,
        _message(
            "old-b",
            {
                "role": "assistant",
                "content": "still old",
                "stopReason": "stop",
                "usage": {"input": 7, "output": 1},
            },
        ),
    )
    continued = asyncio.run(source.read())
    assert [event.text for event in continued.events if event.text] == ["still old"]
    assert [event.usage.input_tokens for event in continued.events if event.usage] == [7]
    assert all(not event.usage_only for event in continued.events)
    source.acknowledge_accounting_checkpoint()
    checkpoint = source.accounting_checkpoint()
    assert checkpoint is not None

    _append(
        old,
        _message(
            "old-c",
            {
                "role": "assistant",
                "content": "after restart",
                "stopReason": "stop",
                "usage": {"input": 9, "output": 1},
            },
        ),
    )
    restarted = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir),
        session_id="old",
        known_location=str(old),
        usage_checkpoint=checkpoint,
    )
    assert asyncio.run(restarted.read()).attached is not None
    restarted.commit_attachment()
    assert [
        event.usage.input_tokens for event in asyncio.run(restarted.read()).events if event.usage
    ] == [9]


def test_pi_adopted_source_attaches_at_eof(tmp_path) -> None:
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    workdir.mkdir()
    sessions.mkdir()
    transcript = sessions / "native-id.jsonl"
    _append(
        transcript,
        _session(session_id="native-id", cwd=workdir),
        _message(
            "assistant-old",
            {
                "role": "assistant",
                "content": "old",
                "stopReason": "stop",
                "usage": {"input": 100, "output": 1},
            },
        ),
    )
    source = PiObserver(root=sessions).open_source(cwd=str(workdir), session_id="native-id")
    attached = asyncio.run(source.read()).attached
    assert attached is not None
    assert attached.skipped == 2
    source.commit_attachment()
    assert asyncio.run(source.read()).events == ()


def test_pi_oversized_record_is_dropped_without_stalling_following_records(tmp_path) -> None:
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    workdir.mkdir()
    transcript = sessions / "native-id.jsonl"
    _append(transcript, _session(session_id="native-id", cwd=workdir))
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id"
    )
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()

    with transcript.open("a", encoding="utf-8") as stream:
        stream.write("x" * (PI_RECORD_BYTES + 1) + "\n")
    _append(transcript, _message("user-1", {"role": "user", "content": "survived"}))

    batch = asyncio.run(source.read())
    assert batch.progressed is True
    assert batch.error_code == "pi_transcript_oversized_record"
    assert [(event.kind, event.text) for event in batch.events] == [(EventKind.USER, "survived")]


def test_pi_history_reader_keeps_live_turn_context_and_resume_forks_into_new_session_dir(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    workdir = tmp_path / "work"
    workdir.mkdir()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    transcript = sessions / "native-id.jsonl"
    _append(
        transcript,
        _session(session_id="native-id", cwd=workdir),
        _message("user-1", {"role": "user", "content": "continue"}),
    )
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id"
    )
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()
    assert asyncio.run(source.history_page(limit=2)).events

    _append(
        transcript,
        _message(
            "assistant-1",
            {"role": "assistant", "content": "done", "stopReason": "stop"},
        ),
    )
    live = asyncio.run(source.read())
    assert live.events[0].turn_id == "user-1"

    cold_plan = plan_launch(LaunchContext("predecessor", "", tmp_path / "config.json", "yolo"))
    domain = Path(cold_plan.transcript_domain or "")
    domain.mkdir(parents=True)
    marker = domain / PI_ISOLATION_MARKER
    marker.write_text(cold_plan.files[marker], encoding="utf-8")
    predecessor_transcript = domain / "native-id.jsonl"
    _append(predecessor_transcript, _session(session_id="native-id", cwd=workdir))
    predecessor = Participant(
        id="predecessor",
        harness="pi",
        cwd=str(workdir),
        session_id="native-id",
        transcript_domain=str(domain),
        transcript_location=str(predecessor_transcript),
        session_correlation="exact",
        status=Status.DEAD,
    )
    overlay = resume_launch_overlay(
        ResumeContext(predecessor=predecessor, trusted_session_owners=(predecessor,))
    )
    resumed = plan_launch(
        LaunchContext(
            "successor",
            "continue",
            tmp_path / "resume.json",
            "yolo",
            resume=overlay.resume_reference,
        )
    )

    successor_domain = participant_root("successor")
    assert resumed.session_id == "successor"
    assert resumed.argv[resumed.argv.index("--session-dir") + 1] == str(successor_domain)
    assert resumed.argv[resumed.argv.index("--fork") + 1] == str(predecessor_transcript.resolve())
    assert resumed.transcript_domain == str(successor_domain.resolve())
    assert successor_domain / PI_ISOLATION_MARKER in resumed.files
    assert overlay.resume_reference == str(predecessor_transcript.resolve())
    assert overlay.transcript_domain is None
    assert overlay.cwd == str(workdir)
