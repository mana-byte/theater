"""Pi's isolated session launch, JSONL observation, and safe resume contract."""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from theater import paths
from theater.daemon.spawner import Spawner, SpawnRequest
from theater.daemon.store import Store
from theater.daemon.trajectory.project import fact_to_record
from theater.harness import theater_mcp_servers
from theater.harness.builtin.plugins.pi import bootstrap
from theater.harness.builtin.plugins.pi.constants import (
    PI_ISOLATION_MARKER,
    PI_RECORD_BYTES,
    PI_SWITCH_MARKER,
    PI_SWITCH_MARKER_VERSION,
    PI_SWITCHES_DIRNAME,
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
from theater.harness.manifests.compiler import compile_manifest
from theater.models import Participant, Status
from theater.resume_floor import UNKNOWN_FLOOR, encode_floor
from theater.trajectory.enums import (
    TimingProvenance,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryStatus,
)
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


def _bash_execution(
    entry_id: str = "bash-1", *, exit_code: int | None = 0, cancelled: bool = False
) -> dict[str, object]:
    return _message(
        entry_id,
        {
            "role": "bashExecution",
            "command": "git push",
            "output": "done",
            "exitCode": exit_code,
            "cancelled": cancelled,
            "truncated": False,
            "timestamp": 1788349677604,
        },
    )


def _lifecycle(phase: str, entry_id: str = "life-1") -> dict[str, object]:
    return {
        "type": "custom",
        "id": entry_id,
        "timestamp": "2026-08-31T12:00:02.000Z",
        "customType": "theater:lifecycle",
        "data": {"version": 1, "phase": phase},
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
    _write_switch_marker(
        root,
        target,
        {
            "version": PI_SWITCH_MARKER_VERSION,
            "reason": reason,
            "location": str(target.resolve()),
            "previous_location": str(previous.resolve()),
            "offset": offset,
            "records": records,
            "dev": dev,
            "ino": ino,
        },
    )


def _mark_startup_fork(
    root: Path, *, previous: Path, target: Path, offset: int | None = None
) -> None:
    stat = target.stat()
    _write_switch_marker(
        root,
        target,
        {
            "version": PI_SWITCH_MARKER_VERSION,
            "reason": "startup-fork",
            "location": str(target.resolve()),
            "previous_location": str(previous.resolve()),
            "offset": stat.st_size if offset is None else offset,
            "dev": stat.st_dev,
            "ino": stat.st_ino,
        },
    )


def _write_switch_marker(root: Path, target: Path, value: dict[str, object]) -> None:
    body = json.dumps(value) + "\n"
    (root / PI_SWITCH_MARKER).write_text(body, encoding="utf-8")
    archive = root / PI_SWITCHES_DIRNAME
    archive.mkdir(exist_ok=True)
    digest = hashlib.sha256(str(target.resolve()).encode()).hexdigest()
    (archive / f"{digest}.json").write_text(body, encoding="utf-8")


def test_pi_launch_advertises_only_yolo_and_builds_isolated_argv(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    config_path = tmp_path / "mcp.json"

    plan = compile_manifest("pi", MANIFEST).plan_launch(
        participant_id="pi-child",
        prompt="inspect this",
        config_path=config_path,
        approval="yolo",
        model="openai/gpt-5.6",
        reasoning_effort="high",
        mcp_servers=theater_mcp_servers("pi-child", "pi"),
    )

    session_dir = paths.participant_observation_dir("pi-child", "pi") / "sessions"
    assert plan.argv[:3] == [
        sys.executable,
        "-m",
        "theater.harness.builtin.plugins.pi.bootstrap",
    ]
    assert plan.argv[plan.argv.index("--session-dir") + 1] == str(session_dir)
    assert plan.argv[plan.argv.index("--theater-mcp-config") + 1] == str(config_path)
    assert plan.argv[-1] == "inspect this"
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
        "--toolset",
        "control",
    ]
    # Pi has no Theater-enforced permission prompt or sandbox, so the launch
    # contract must advertise only the behavior plan_launch produces.
    assert MANIFEST.launch.approvals == ("yolo",)
    assert plan.env["PI_OFFLINE"] == "1"
    assert plan.env["PI_CACHE_RETENTION"] == "long"
    assert plan.env["PI_SKIP_VERSION_CHECK"] == "1"
    assert plan.env["PI_TELEMETRY"] == "0"


def test_pi_launch_argv_is_independent_of_approval_and_manifest_rejects_other_modes(
    tmp_path, monkeypatch
) -> None:
    # plan_launch never reads context.approval, so the produced argv must be
    # identical for every approval value. The manifest gate is the only thing
    # that enforces the contract, and it must reject manual/edits because Pi
    # has no Theater-enforced permission prompt or sandbox for them.
    from theater.harness.manifests.compiler import compile_manifest
    from theater.models import BadRequest

    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    config_path = tmp_path / "mcp.json"
    base = plan_launch(
        LaunchContext(
            participant_id="pi-child",
            prompt="inspect this",
            config_path=config_path,
            approval="yolo",
        )
    ).argv
    for approval in ("manual", "edits", "yolo"):
        plan = plan_launch(
            LaunchContext(
                participant_id="pi-child",
                prompt="inspect this",
                config_path=config_path,
                approval=approval,
            )
        )
        assert plan.argv == base

    harness = compile_manifest("pi", MANIFEST)
    for rejected in ("manual", "edits"):
        with pytest.raises(BadRequest, match="approval must be one of yolo"):
            harness.plan_launch(
                participant_id="pi-child",
                prompt="inspect this",
                config_path=config_path,
                approval=rejected,
            )


def test_pi_bootstrap_suppresses_only_the_expected_cold_warning(monkeypatch) -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is unavailable")
    captured: dict[str, object] = {}

    def execvpe(file, args, env):
        captured.update(file=file, args=args, env=env)
        raise RuntimeError("exec intercepted")

    monkeypatch.setenv("NODE_OPTIONS", "--no-warnings")
    monkeypatch.setattr(sys, "argv", ["bootstrap", "--theater-cold-session-id", "child-1"])
    monkeypatch.setattr(bootstrap.os, "execvpe", execvpe)
    with pytest.raises(RuntimeError, match="exec intercepted"):
        bootstrap.main()

    env = captured["env"]
    assert isinstance(env, dict)
    assert str(env["NODE_OPTIONS"]).startswith("--no-warnings ")
    result = subprocess.run(
        [
            node,
            "-e",
            "console.error('other'); "
            "console.error(\"\\x1b[33mWarning: No project session found with id 'child-1'; "
            'creating a new session with that id.\\x1b[39m"); '
            "console.error('after')",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stderr.splitlines() == ["other", "after"]


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


def test_pi_source_reports_completed_direct_bash_as_idle(tmp_path) -> None:
    sessions = tmp_path / "sessions"
    workdir = tmp_path / "work"
    sessions.mkdir()
    workdir.mkdir()
    transcript = sessions / "native-id.jsonl"
    _append(transcript, _session(session_id="native-id", cwd=workdir))
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id"
    )
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()
    asyncio.run(source.read())

    _append(transcript, _bash_execution())
    batch = asyncio.run(source.read())

    assert batch.status is Status.IDLE
    assert not batch.events
    assert not batch.trajectory


@pytest.mark.parametrize(
    ("records", "expected_status"),
    [
        (
            (
                _bash_execution(),
                _message("user-1", {"role": "user", "content": "continue"}),
            ),
            None,
        ),
        (
            (
                _message("user-1", {"role": "user", "content": "continue"}),
                _bash_execution(),
            ),
            Status.IDLE,
        ),
    ],
)
def test_pi_source_resolves_direct_bash_status_in_record_order(
    tmp_path, records, expected_status
) -> None:
    sessions = tmp_path / "sessions"
    workdir = tmp_path / "work"
    sessions.mkdir()
    workdir.mkdir()
    transcript = sessions / "native-id.jsonl"
    _append(transcript, _session(session_id="native-id", cwd=workdir))
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id"
    )
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()
    asyncio.run(source.read())

    _append(transcript, *records)
    batch = asyncio.run(source.read())

    assert batch.status is expected_status
    assert [event.kind for event in batch.events] == [EventKind.USER]


def test_pi_attachment_restores_completed_direct_bash_status(tmp_path) -> None:
    sessions = tmp_path / "sessions"
    workdir = tmp_path / "work"
    sessions.mkdir()
    workdir.mkdir()
    transcript = sessions / "native-id.jsonl"
    _append(
        transcript,
        _session(session_id="native-id", cwd=workdir),
        _bash_execution(),
    )
    source = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="native-id"
    )

    attached = asyncio.run(source.read()).attached

    assert attached is not None
    assert attached.last_event is None
    assert attached.status is Status.IDLE


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
    ("exit_code", "cancelled"),
    [(0, False), (1, False), (None, True)],
)
def test_pi_parser_treats_completed_direct_bash_as_idle_without_a_turn(
    tmp_path, exit_code, cancelled
) -> None:
    parsed = PiObserver(root=tmp_path).parse_record(
        json.dumps(_bash_execution(exit_code=exit_code, cancelled=cancelled)), 1
    )

    assert parsed.status is Status.IDLE
    assert parsed.events == ()
    assert parsed.trajectory == ()


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


def test_pi_parser_derives_assistant_timing_from_inner_start_and_outer_completion(tmp_path) -> None:
    # Pi writes two timestamps: inner message.timestamp (set by pi-ai when the
    # pending response object is created, ~= generation start) and outer
    # record.timestamp (set by the session manager on message_end, ~= completion).
    # The parser must pair them into a DERIVED interval instead of mislabeling the
    # inner timestamp as the end and discarding the outer one.
    observer = PiObserver(root=tmp_path)
    inner_epoch_ms = 1788254909379
    record = {
        "type": "message",
        "id": "assistant-1",
        "timestamp": "2026-09-01T09:28:39.296Z",  # outer (completion)
        "message": {
            "role": "assistant",
            "timestamp": inner_epoch_ms,  # inner (start)
            "content": [{"type": "text", "text": "hi"}],
            "stopReason": "stop",
        },
    }
    parsed = observer.parse_record(json.dumps(record), 1)
    assistant = next(f for f in parsed.trajectory if f.kind is TrajectoryKind.ASSISTANT)
    assert assistant.timing is not None
    assert assistant.timing.provenance is TimingProvenance.DERIVED
    assert assistant.timing.start == inner_epoch_ms / 1_000
    timing = assistant.timing
    assert timing.end is not None
    assert timing.start is not None
    assert timing.duration_ms is not None
    assert timing.duration_ms == pytest.approx((timing.end - timing.start) * 1_000)
    # Directly assert the end is the outer (completion) timestamp, not just self-consistent.
    assert timing.end == 1788254919.296  # 2026-09-01T09:28:39.296Z
    # Control Event.ts must prefer the outer (completion) timestamp, not the
    # inner generation-start, so the bus stamp is the moment Pi finalized.
    assert parsed.events
    assert parsed.events[0].ts == 1788254919.296


def test_pi_parser_anchors_tool_call_start_at_containing_assistant_completion(tmp_path) -> None:
    observer = PiObserver(root=tmp_path)
    record = {
        "type": "message",
        "id": "assistant-1",
        "timestamp": "2026-09-01T09:28:39.296Z",  # outer (completion)
        "message": {
            "role": "assistant",
            "timestamp": 1788254909379,  # inner (start)
            "content": [
                {
                    "type": "toolCall",
                    "id": "call-1",
                    "name": "bash",
                    "arguments": {"command": "echo hi"},
                }
            ],
            "stopReason": "toolUse",
        },
    }
    parsed = observer.parse_record(json.dumps(record), 1)
    tool_call = next(f for f in parsed.trajectory if f.kind is TrajectoryKind.TOOL_CALL)
    assert tool_call.timing is not None
    assert tool_call.timing.provenance is TimingProvenance.DERIVED
    # Tool-call start is the assistant's OUTER (completion) timestamp, not the inner.
    assert tool_call.timing.start == 1788254919.296
    assert tool_call.timing.end is None


def test_pi_parser_falls_back_to_start_source_when_only_inner_timestamp_exists(tmp_path) -> None:
    # When the outer record.timestamp is absent, the inner timestamp is treated as
    # the start (not the end) so the original mislabeling bug never returns.
    observer = PiObserver(root=tmp_path)
    inner_epoch_ms = 1788254909379
    record = {
        "type": "message",
        "id": "assistant-1",
        "message": {
            "role": "assistant",
            "timestamp": inner_epoch_ms,
            "content": [{"type": "text", "text": "hi"}],
            "stopReason": "stop",
        },
    }
    parsed = observer.parse_record(json.dumps(record), 1)
    assistant = next(f for f in parsed.trajectory if f.kind is TrajectoryKind.ASSISTANT)
    assert assistant.timing is not None
    assert assistant.timing.provenance is TimingProvenance.SOURCE
    assert assistant.timing.start == inner_epoch_ms / 1_000
    assert assistant.timing.end is None


def test_pi_parser_inner_only_assistant_usage_uses_start_not_end(tmp_path) -> None:
    # When only the inner timestamp exists, the assistant usage fact must agree with
    # the assistant/reasoning facts: start-only SOURCE. It must not fall back to the
    # old end-only _timing() path.
    observer = PiObserver(root=tmp_path)
    inner_epoch_ms = 1788254909379
    record = {
        "type": "message",
        "id": "assistant-1",
        "message": {
            "role": "assistant",
            "timestamp": inner_epoch_ms,
            "content": [{"type": "text", "text": "hi"}],
            "stopReason": "stop",
            "usage": {
                "input": 10,
                "output": 5,
                "cacheRead": 0,
                "cacheWrite": 0,
                "totalTokens": 15,
                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
            },
        },
    }
    parsed = observer.parse_record(json.dumps(record), 1)
    assistant = next(f for f in parsed.trajectory if f.kind is TrajectoryKind.ASSISTANT)
    usage = next(f for f in parsed.trajectory if f.kind is TrajectoryKind.USAGE)
    assert assistant.timing is not None
    assert assistant.timing.provenance is TimingProvenance.SOURCE
    assert assistant.timing.start == inner_epoch_ms / 1_000
    assert assistant.timing.end is None
    # Usage fact must agree with the assistant fact: start-only SOURCE, not end-only.
    assert usage.timing is not None
    assert usage.timing.provenance is TimingProvenance.SOURCE
    assert usage.timing.start == inner_epoch_ms / 1_000
    assert usage.timing.end is None


def test_pi_parser_falls_back_to_end_source_when_only_outer_timestamp_exists(tmp_path) -> None:
    # When the inner message.timestamp is absent but the outer record.timestamp is
    # present, the assistant record becomes Timing(end=outer, SOURCE) -- NOT a
    # zero-width Timing(start=outer, end=outer, DERIVED) interval.
    observer = PiObserver(root=tmp_path)
    record = {
        "type": "message",
        "id": "assistant-1",
        "timestamp": "2026-09-01T09:28:39.296Z",  # outer only
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "hi"}],
            "stopReason": "stop",
        },
    }
    parsed = observer.parse_record(json.dumps(record), 1)
    assistant = next(f for f in parsed.trajectory if f.kind is TrajectoryKind.ASSISTANT)
    assert assistant.timing is not None
    assert assistant.timing.provenance is TimingProvenance.SOURCE
    assert assistant.timing.start is None
    assert assistant.timing.end == 1788254919.296  # 2026-09-01T09:28:39.296Z
    assert assistant.timing.duration_ms is None


def test_pi_parser_drops_interval_when_outer_precedes_inner(tmp_path) -> None:
    # If the outer (completion) timestamp precedes the inner (start) -- a malformed
    # or clock-skewed record -- the interval is not built as a negative-duration
    # DERIVED; the inner is kept as a start-only SOURCE point.
    observer = PiObserver(root=tmp_path)
    record = {
        "type": "message",
        "id": "assistant-1",
        "timestamp": "2026-09-01T09:28:29.000Z",  # outer EARLIER than inner
        "message": {
            "role": "assistant",
            "timestamp": 1788254919379,  # inner later (09:28:39.379Z)
            "content": [{"type": "text", "text": "hi"}],
            "stopReason": "stop",
        },
    }
    parsed = observer.parse_record(json.dumps(record), 1)
    assistant = next(f for f in parsed.trajectory if f.kind is TrajectoryKind.ASSISTANT)
    assert assistant.timing is not None
    # outer < inner => no DERIVED interval; inner retained as start-only SOURCE.
    assert assistant.timing.provenance is TimingProvenance.SOURCE
    assert assistant.timing.start == 1788254919.379
    assert assistant.timing.end is None
    assert assistant.timing.duration_ms is None


def test_pi_parser_extracts_reasoning_from_thinking_blocks(tmp_path) -> None:
    # Pi ThinkingContent is {"type":"thinking","thinking":str,...}; the parser
    # previously read only "text" (a TextContent field), so REASONING facts were
    # always empty.
    observer = PiObserver(root=tmp_path)
    parsed = observer.parse_record(
        json.dumps(
            _message(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "answer"},
                        {"type": "thinking", "thinking": "I should inspect the tree first."},
                        {"type": "thinking"},  # redacted / empty
                        {"type": "thinking", "thinking": ""},  # explicit empty
                    ],
                    "stopReason": "stop",
                },
            )
        ),
        1,
    )
    reasoning = [fact for fact in parsed.trajectory if fact.kind is TrajectoryKind.REASONING]
    assert [fact.summary for fact in reasoning] == ["I should inspect the tree first."]
    assistant = next(fact for fact in parsed.trajectory if fact.kind is TrajectoryKind.ASSISTANT)
    assert assistant.summary == "answer"


def test_pi_parser_error_response_defers_turn_end_until_settled_marker(tmp_path) -> None:
    observer = PiObserver(root=tmp_path)
    user = observer.parse_record(
        json.dumps(_message("user-1", {"role": "user", "content": "do it"})), 1
    )
    error = observer.parse_record(
        json.dumps(
            _message(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": ""}],
                    "stopReason": "error",
                    "errorMessage": "overloaded",
                },
            )
        ),
        2,
    )
    # The error must not close the turn yet (no turn_end), though it still
    # emits its error content/trajectory facts.
    assert not any(event.turn_end for event in error.events)
    assert [event.kind for event in error.events] == [EventKind.ERROR]
    assert error.trajectory[0].status is TrajectoryStatus.ERROR
    assert observer._pending_terminal_turn_id == "user-1"
    settled = observer.parse_record(json.dumps(_lifecycle("settled")), 3)
    assert [event.kind for event in settled.events] == [EventKind.ASSISTANT]
    assert settled.events[0].turn_end is True
    assert settled.events[0].turn_id == "user-1"
    # No duplicate assistant text, usage, or trajectory facts on the marker.
    assert settled.events[0].text == ""
    assert settled.events[0].usage is None
    assert settled.trajectory == ()
    assert observer._pending_terminal is False
    assert observer._pending_terminal_turn_id is None
    assert observer._active_turn_id is None
    _ = user


def test_pi_parser_error_then_retry_marker_has_no_turn_end(tmp_path) -> None:
    observer = PiObserver(root=tmp_path)
    observer.parse_record(json.dumps(_message("user-1", {"role": "user", "content": "do it"})), 1)
    error = observer.parse_record(
        json.dumps(
            _message(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": ""}],
                    "stopReason": "error",
                    "errorMessage": "overloaded",
                },
            )
        ),
        2,
    )
    assert not any(event.turn_end for event in error.events)
    assert observer._pending_terminal_turn_id == "user-1"
    # A retry-scheduled marker is an informational no-op: it signals continued
    # work, not its success. Compaction may still fail and then settled must
    # close, so the pending terminal is retained.
    retry = observer.parse_record(json.dumps(_lifecycle("retry-scheduled")), 3)
    assert not retry.events
    assert observer._pending_terminal is True
    assert observer._pending_terminal_turn_id == "user-1"
    assert observer._active_turn_id == "user-1"


def test_pi_parser_settled_marker_may_arrive_in_a_later_parse_record_call(tmp_path) -> None:
    observer = PiObserver(root=tmp_path)
    observer.parse_record(json.dumps(_message("user-1", {"role": "user", "content": "do it"})), 1)
    observer.parse_record(
        json.dumps(
            _message(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": ""}],
                    "stopReason": "error",
                    "errorMessage": "overloaded",
                },
            )
        ),
        2,
    )
    # Simulate drain batches arriving later: a tool result then the marker.
    tool_result = observer.parse_record(
        json.dumps(
            _message(
                "tool-1",
                {
                    "role": "toolResult",
                    "toolCallId": "call-1",
                    "toolName": "bash",
                    "content": [{"type": "text", "text": "ok"}],
                },
            )
        ),
        3,
    )
    assert tool_result.events[0].kind is EventKind.TOOL_RESULT
    settled = observer.parse_record(json.dumps(_lifecycle("settled")), 4)
    assert settled.events[0].turn_end is True
    assert settled.events[0].turn_id == "user-1"
    assert observer._pending_terminal is False
    assert observer._pending_terminal_turn_id is None


def test_pi_parser_length_plus_compaction_marker_stays_open(tmp_path) -> None:
    observer = PiObserver(root=tmp_path)
    observer.parse_record(
        json.dumps(_message("user-1", {"role": "user", "content": "long task"})), 1
    )
    length = observer.parse_record(
        json.dumps(
            _message(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "partial..."}],
                    "stopReason": "length",
                },
            )
        ),
        2,
    )
    # length is recoverable (compact-and-retry); must not close.
    assert not any(event.turn_end for event in length.events)
    assert observer._pending_terminal_turn_id == "user-1"
    assert observer._pending_terminal is True
    compaction = observer.parse_record(json.dumps(_lifecycle("compaction-will-retry")), 3)
    assert not compaction.events
    # compaction-will-retry is informational: compaction may still fail, so the
    # pending terminal is retained until settled.
    assert observer._pending_terminal is True
    assert observer._pending_terminal_turn_id == "user-1"
    assert observer._active_turn_id == "user-1"


def test_pi_parser_retry_success_closes_once_and_clears_pending(tmp_path) -> None:
    observer = PiObserver(root=tmp_path)
    observer.parse_record(json.dumps(_message("user-1", {"role": "user", "content": "do it"})), 1)
    observer.parse_record(
        json.dumps(
            _message(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": ""}],
                    "stopReason": "error",
                    "errorMessage": "overloaded",
                },
            )
        ),
        2,
    )
    assert observer._pending_terminal_turn_id == "user-1"
    # A later successful stop closes normally and clears the stale pending state.
    success = observer.parse_record(
        json.dumps(
            _message(
                "assistant-2",
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "done"}],
                    "stopReason": "stop",
                },
            )
        ),
        3,
    )
    assert success.events[-1].turn_end is True
    assert observer._pending_terminal is False
    assert observer._pending_terminal_turn_id is None
    assert observer._active_turn_id is None
    # A subsequent settled marker with no pending terminal is a no-op.
    redundant = observer.parse_record(json.dumps(_lifecycle("settled")), 4)
    assert not redundant.events


def test_pi_parser_malformed_lifecycle_marker_ignored(tmp_path) -> None:
    observer = PiObserver(root=tmp_path)
    observer.parse_record(json.dumps(_message("user-1", {"role": "user", "content": "do it"})), 1)
    observer.parse_record(
        json.dumps(
            _message(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": ""}],
                    "stopReason": "error",
                    "errorMessage": "overloaded",
                },
            )
        ),
        2,
    )
    for malformed in (
        {
            "type": "custom",
            "customType": "theater:lifecycle",
            "data": {"version": 2, "phase": "settled"},
        },
        {
            "type": "custom",
            "customType": "theater:lifecycle",
            "data": {"version": 1, "phase": "unknown"},
        },
        {"type": "custom", "customType": "other", "data": {"version": 1, "phase": "settled"}},
        {"type": "custom", "customType": "theater:lifecycle", "data": "not-a-dict"},
        {"type": "custom", "customType": "theater:lifecycle"},
        {
            "type": "message",
            "customType": "theater:lifecycle",
            "data": {"version": 1, "phase": "settled"},
        },
    ):
        parsed = observer.parse_record(json.dumps(malformed), 3)
        assert not parsed.events
        assert parsed.trajectory == ()
    # The pending terminal is untouched by malformed markers.
    assert observer._pending_terminal_turn_id == "user-1"


def test_pi_parser_aborted_partial_calls_are_interrupted_not_pending(tmp_path) -> None:
    observer = PiObserver(root=tmp_path)
    observer.parse_record(json.dumps(_message("user-1", {"role": "user", "content": "do it"})), 1)
    parsed = observer.parse_record(
        json.dumps(
            _message(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "call-1",
                            "name": "bash",
                            "arguments": {},
                        }
                    ],
                    "stopReason": "aborted",
                    "errorMessage": "user cancelled",
                },
            )
        ),
        2,
    )
    # Aborted defers the turn_end (a settled marker will release it).
    assert not any(event.turn_end for event in parsed.events)
    call = next(f for f in parsed.trajectory if f.kind is TrajectoryKind.TOOL_CALL)
    assert call.status is TrajectoryStatus.INTERRUPTED
    assert observer._pending_terminal_turn_id == "user-1"
    settled = observer.parse_record(json.dumps(_lifecycle("settled")), 3)
    assert settled.events[0].turn_end is True


def test_pi_parser_error_partial_calls_are_error_not_pending(tmp_path) -> None:
    observer = PiObserver(root=tmp_path)
    observer.parse_record(json.dumps(_message("user-1", {"role": "user", "content": "do it"})), 1)
    parsed = observer.parse_record(
        json.dumps(
            _message(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "toolCall",
                            "id": "call-1",
                            "name": "bash",
                            "arguments": {},
                        }
                    ],
                    "stopReason": "error",
                    "errorMessage": "overloaded",
                },
            )
        ),
        2,
    )
    assert not any(event.turn_end for event in parsed.events)
    call = next(f for f in parsed.trajectory if f.kind is TrajectoryKind.TOOL_CALL)
    assert call.status is TrajectoryStatus.ERROR


def test_pi_parser_length_with_calls_marks_calls_error(tmp_path) -> None:
    # A recoverable length response is discarded by Pi's agent-core and retried;
    # its toolCall blocks never execute, so they must be ERROR, not PENDING.
    observer = PiObserver(root=tmp_path)
    observer.parse_record(json.dumps(_message("user-1", {"role": "user", "content": "do it"})), 1)
    parsed = observer.parse_record(
        json.dumps(
            _message(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "partial"},
                        {
                            "type": "toolCall",
                            "id": "call-1",
                            "name": "bash",
                            "arguments": {},
                        },
                    ],
                    "stopReason": "length",
                },
            )
        ),
        2,
    )
    assert not any(event.turn_end for event in parsed.events)
    call = next(f for f in parsed.trajectory if f.kind is TrajectoryKind.TOOL_CALL)
    assert call.status is TrajectoryStatus.ERROR
    assert observer._pending_terminal_turn_id == "user-1"


def test_pi_parser_reset_turn_context_clears_lifecycle_state(tmp_path) -> None:
    observer = PiObserver(root=tmp_path)
    observer.parse_record(json.dumps(_message("user-1", {"role": "user", "content": "do it"})), 1)
    observer.parse_record(
        json.dumps(
            _message(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": ""}],
                    "stopReason": "error",
                    "errorMessage": "overloaded",
                },
            )
        ),
        2,
    )
    assert observer._pending_terminal is True
    assert observer._pending_terminal_turn_id == "user-1"
    observer._reset_turn_context()
    assert observer._pending_terminal is False
    assert observer._pending_terminal_turn_id is None
    assert observer._active_turn_id is None


def test_pi_parser_history_seeding_reconstructs_lifecycle_state(tmp_path) -> None:
    import io

    workdir = tmp_path / "work"
    workdir.mkdir()
    transcript = tmp_path / "native-id.jsonl"
    _append(
        transcript,
        _session(session_id="native-id", cwd=workdir),
        _message("user-1", {"role": "user", "content": "do it"}),
        _message(
            "assistant-1",
            {
                "role": "assistant",
                "content": [{"type": "text", "text": ""}],
                "stopReason": "error",
                "errorMessage": "overloaded",
            },
        ),
        _lifecycle("settled"),
        _message("user-2", {"role": "user", "content": "next"}),
    )
    observer = PiObserver(root=tmp_path)
    # Seeding from just before the second user message replays the settled marker
    # and the error, leaving no stale pending terminal across the gap.
    offset = transcript.stat().st_size
    stream = io.BufferedReader(io.BytesIO(transcript.read_bytes()))
    observer._seed_history_context(stream, offset)
    assert observer._pending_terminal is False
    assert observer._pending_terminal_turn_id is None
    # The last user message reopens the active turn.
    assert observer._active_turn_id == "user-2"


def test_pi_parser_history_seeding_deferred_without_settled_keeps_pending(tmp_path) -> None:
    import io

    workdir = tmp_path / "work"
    workdir.mkdir()
    transcript = tmp_path / "native-id.jsonl"
    _append(
        transcript,
        _session(session_id="native-id", cwd=workdir),
        _message("user-1", {"role": "user", "content": "do it"}),
        _message(
            "assistant-1",
            {
                "role": "assistant",
                "content": [{"type": "text", "text": ""}],
                "stopReason": "error",
                "errorMessage": "overloaded",
            },
        ),
    )
    observer = PiObserver(root=tmp_path)
    offset = transcript.stat().st_size
    stream = io.BufferedReader(io.BytesIO(transcript.read_bytes()))
    observer._seed_history_context(stream, offset)
    # No settled marker arrived in history: the deferred terminal is retained.
    assert observer._pending_terminal is True
    assert observer._pending_terminal_turn_id == "user-1"


def test_pi_parser_compaction_will_retry_then_settled_closes_without_later_assistant(
    tmp_path,
) -> None:
    # Compaction may fail and then agent_settled fires with no later assistant
    # response. The pending terminal from the length response must still close.
    observer = PiObserver(root=tmp_path)
    observer.parse_record(
        json.dumps(_message("user-1", {"role": "user", "content": "long task"})), 1
    )
    length = observer.parse_record(
        json.dumps(
            _message(
                "assistant-1",
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "partial..."}],
                    "stopReason": "length",
                },
            )
        ),
        2,
    )
    assert observer._pending_terminal is True
    compaction = observer.parse_record(json.dumps(_lifecycle("compaction-will-retry")), 3)
    assert not compaction.events
    # The informational compaction marker did NOT clear the pending terminal.
    assert observer._pending_terminal is True
    settled = observer.parse_record(json.dumps(_lifecycle("settled")), 4)
    assert settled.events[0].turn_end is True
    assert settled.events[0].turn_id == "user-1"
    assert observer._pending_terminal is False
    assert observer._pending_terminal_turn_id is None
    _ = length


def test_pi_parser_anonymous_assistant_record_settled_closes_with_turn_id_none(tmp_path) -> None:
    # An assistant record with no entry id still defers; the pending candidate
    # is tracked by presence, not by the (possibly None) turn id, so settled
    # still releases one synthetic turn_end. The turn id is the active turn's,
    # which may itself be None when there was no preceding user record.
    observer = PiObserver(root=tmp_path)
    record = {
        "type": "message",
        "timestamp": "2026-08-31T12:00:01.000Z",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": ""}],
            "stopReason": "error",
            "errorMessage": "overloaded",
        },
    }
    parsed = observer.parse_record(json.dumps(record), 1)
    assert not any(event.turn_end for event in parsed.events)
    assert observer._pending_terminal is True
    # No preceding user record => active turn id is None, but pending is True.
    assert observer._pending_terminal_turn_id is None
    settled = observer.parse_record(json.dumps(_lifecycle("settled")), 2)
    assert settled.events[0].turn_end is True
    assert settled.events[0].turn_id is None
    assert observer._pending_terminal is False
    assert observer._pending_terminal_turn_id is None
    _ = parsed


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


def test_pi_resumed_source_accepts_a_legacy_floor_checkpoint(tmp_path) -> None:
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
        cwd=str(workdir), session_id="native-id", source_checkpoint=encode_floor(floor)
    )

    attached = asyncio.run(source.read()).attached
    assert attached is not None
    assert attached.skipped == floor.records
    assert attached.last_event is None
    source.commit_attachment()

    batch = asyncio.run(source.read())
    usage = [event.usage for event in batch.events if event.usage is not None]
    assert [event.input_tokens for event in usage] == [7]


def test_pi_unknown_resume_checkpoint_fails_closed(tmp_path) -> None:
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
        cwd=str(workdir), session_id="native-id", source_checkpoint=UNKNOWN_FLOOR
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


def test_pi_restart_resumes_from_the_current_source_checkpoint(tmp_path) -> None:
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
    first.acknowledge_source_checkpoint()
    checkpoint = first.source_checkpoint()
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
        source_checkpoint=checkpoint,
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
        source_checkpoint=encode_floor(floor),
    )
    assert asyncio.run(source.read()).attached is not None
    source.commit_attachment()
    source.acknowledge_source_checkpoint()
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
    source.acknowledge_source_checkpoint()
    checkpoint = source.source_checkpoint()
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
        source_checkpoint=checkpoint,
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
    source.acknowledge_source_checkpoint()
    assert [
        event.usage.input_tokens for event in asyncio.run(source.read()).events if event.usage
    ] == [100]
    source.acknowledge_source_checkpoint()

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


def test_pi_restart_uses_archived_fork_boundary_after_a_later_new(tmp_path) -> None:
    workdir = tmp_path / "work"
    sessions = tmp_path / "sessions"
    workdir.mkdir()
    sessions.mkdir()
    previous = tmp_path / "previous.jsonl"
    forked = sessions / "forked.jsonl"
    _append(
        previous,
        _session(session_id="previous", cwd=workdir),
        _message(
            "old",
            {
                "role": "assistant",
                "content": "copied",
                "stopReason": "stop",
                "usage": {"input": 100, "output": 1},
            },
        ),
    )
    _append(
        forked,
        _session(session_id="forked", cwd=workdir, parent_session=previous),
        _message(
            "old",
            {
                "role": "assistant",
                "content": "copied",
                "stopReason": "stop",
                "usage": {"input": 100, "output": 1},
            },
        ),
    )
    _mark_startup_fork(sessions, previous=previous, target=forked)
    _append(
        forked,
        _message(
            "new",
            {
                "role": "assistant",
                "content": "new",
                "stopReason": "stop",
                "usage": {"input": 7, "output": 1},
            },
        ),
    )
    replacement = sessions / "replacement.jsonl"
    _append(replacement, _session(session_id="replacement", cwd=workdir))
    _mark_switch(sessions, previous=forked, target=replacement, reason="new")

    restarted = PiObserver(root=sessions, isolated=True).open_source(
        cwd=str(workdir), session_id="forked", known_location=str(forked)
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
    source.acknowledge_source_checkpoint()
    asyncio.run(source.read())
    source.acknowledge_source_checkpoint()

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
    source.acknowledge_source_checkpoint()

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

    source.rollback_source_checkpoint()
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
    source.acknowledge_source_checkpoint()

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
    source.acknowledge_source_checkpoint()

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
    source.acknowledge_source_checkpoint()
    checkpoint = source.source_checkpoint()
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
        source_checkpoint=checkpoint,
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
