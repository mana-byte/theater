"""Real Claude CLI checks run inside the harness-compat sandbox."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from pathlib import Path

import pytest

from scripts.harness_compat.contract import MOCK_REPLY
from scripts.harness_compat.mock import MockServer
from theater.harness.base import EventKind
from theater.harness.builtin.plugins.claude.hooks import (
    NATIVE_HOOK_CHANNEL,
    correlate_tool_hook,
    decode_tool_hook,
    install_native_hooks,
    native_hook_token_path,
)
from theater.harness.builtin.plugins.claude.launch import plan_launch
from theater.harness.builtin.plugins.claude.observer import ClaudeCodeObserver
from theater.harness.contracts.callbacks import (
    HookCorrelationContext,
    HookDecodeContext,
    HookInstallContext,
    LaunchContext,
)
from theater.trajectory.enums import TrajectoryStatus


def _run_tmux(socket: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["tmux", "-S", str(socket), *args], capture_output=True, text=True, check=False, timeout=10
    )


def _wait_for(predicate, *, timeout: float = 45) -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError("Claude TUI did not produce the expected transcript evidence")


def _run_turn(*, settings: Path | None = None, prompt: str = "Reply with a short greeting") -> Path:
    binary = Path(os.environ["HARNESS_COMPAT_BINARY"])
    cwd = Path(os.environ["HARNESS_COMPAT_WORKDIR"])
    socket = Path(os.environ["HARNESS_COMPAT_TMUX_SOCKET"])
    config = Path(os.environ["CLAUDE_CONFIG_DIR"])
    session_id = str(uuid.uuid4())
    command = [str(binary), f"--session-id={session_id}", "--name=compat-proof"]
    if settings is not None:
        command.extend([f"--settings={settings}", "--permission-mode=default"])
    launched = _run_tmux(
        socket,
        "new-session",
        "-d",
        "-s",
        "compat",
        "-x",
        "150",
        "-y",
        "45",
        "-c",
        str(cwd),
        shlex.join(command),
    )
    assert launched.returncode == 0, launched.stderr
    assert _run_tmux(socket, "set-option", "-g", "remain-on-exit", "on").returncode == 0
    try:
        accepted_bypass = False

        def ready() -> bool:
            nonlocal accepted_bypass
            pane = _run_tmux(socket, "capture-pane", "-p", "-t", "compat", "-S", "-80")
            if "WARNING: Claude Code running in Bypass Permissions mode" in pane.stdout:
                if not accepted_bypass:
                    _run_tmux(socket, "send-keys", "-t", "compat", "Down", "Enter")
                    accepted_bypass = True
                return False
            return (
                pane.returncode == 0
                and "Claude Code" in pane.stdout
                and "❯" in pane.stdout
                and ("for agents" in pane.stdout or "? for shortcuts" in pane.stdout)
            )

        _wait_for(ready)
        assert _run_tmux(socket, "send-keys", "-t", "compat", "-l", prompt).returncode == 0
        assert _run_tmux(socket, "send-keys", "-t", "compat", "Enter").returncode == 0

        def completed() -> Path | None:
            paths = list((config / "projects").glob(f"*/{session_id}.jsonl"))
            if not paths:
                return None
            try:
                records = [json.loads(line) for line in paths[0].read_text().splitlines()]
            except (OSError, json.JSONDecodeError):
                return None
            if any(
                record.get("type") == "system" and record.get("subtype") == "turn_duration"
                for record in records
            ):
                return paths[0]
            return None

        try:
            return _wait_for(
                completed, timeout=float(os.environ.get("HARNESS_COMPAT_TURN_TIMEOUT", "90"))
            )  # type: ignore[return-value]
        except AssertionError as exc:
            pane = _run_tmux(socket, "capture-pane", "-p", "-t", "compat", "-S", "-80")
            paths = list((config / "projects").glob(f"*/{session_id}.jsonl"))
            routes = config / "local-tool-requests.jsonl"
            detail = routes.read_text()[-1000:] if routes.exists() else "no local requests"
            raise AssertionError(
                f"Claude turn incomplete; pane={pane.stdout[-1200:]!r}; "
                f"transcripts={paths!r}; routes={detail}"
            ) from exc
    finally:
        _run_tmux(socket, "kill-server")


@pytest.fixture(scope="module")
def tui_transcript() -> Path:
    if "HARNESS_COMPAT_BINARY" not in os.environ:
        pytest.skip("real Claude qualification requires a sandbox binary")
    return _run_turn()


def test_claude_seeded_tui_turn(tui_transcript: Path) -> None:
    records = [json.loads(line) for line in tui_transcript.read_text().splitlines()]
    requests = [
        json.loads(line)
        for line in (Path(os.environ["HARNESS_COMPAT_EVIDENCE_DIR"]) / "mock_requests.jsonl")
        .read_text()
        .splitlines()
    ]
    assert any(
        request["method"] == "POST"
        and request["path"] == "/v1/messages"
        and request["authorized"]
        and request["handled"]
        for request in requests
    )
    assert any(record.get("type") == "user" for record in records)
    assert any(
        record.get("type") == "assistant"
        and any(
            block.get("type") == "text" and MOCK_REPLY in block.get("text", "")
            for block in record.get("message", {}).get("content", [])
        )
        for record in records
    )


def test_claude_observer_seals_tui_turn(tui_transcript: Path) -> None:
    observer = ClaudeCodeObserver(root=tui_transcript.parent.parent)
    events = [
        event
        for index, line in enumerate(tui_transcript.read_text().splitlines())
        for event in observer.parse(line, index)
    ]
    finals = [event for event in events if event.kind is EventKind.ASSISTANT and event.turn_end]
    assert len(finals) == 1
    assert MOCK_REPLY in (finals[0].raw_text or "")


@contextmanager
def _local_tool_mock(config: Path) -> Iterator[MockServer]:
    """Temporary tool reply until the shared mock implements queued tool calls."""
    server = MockServer(config / "local-tool-requests.jsonl")
    original_respond = server._respond
    sent = False

    def respond(route, body):
        nonlocal sent
        title_request = "Write the title" in json.dumps(body.get("messages", []))
        if route == ("POST", "/v1/messages") and not title_request and not sent:
            sent = True
            start = {
                "id": "msg_mock_tool",
                "type": "message",
                "role": "assistant",
                "model": "mock-model",
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
            events = [
                {"type": "message_start", "message": start},
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {
                        "type": "tool_use",
                        "id": "toolu_mock_compat",
                        "name": "Read",
                        "input": {},
                    },
                },
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": json.dumps(
                            {
                                "file_path": str(
                                    Path(os.environ["HARNESS_COMPAT_WORKDIR"]) / "hook-proof.txt"
                                )
                            }
                        ),
                    },
                },
                {"type": "content_block_stop", "index": 0},
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                    "usage": {"output_tokens": 4},
                },
                {"type": "message_stop"},
            ]
            payload = "".join(
                f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
            )
            return 200, "text/event-stream", payload
        return original_respond(route, body)

    server._respond = respond  # type: ignore[method-assign]
    server.start()
    previous = os.environ["ANTHROPIC_BASE_URL"]
    os.environ["ANTHROPIC_BASE_URL"] = server.base_urls["anthropic"]
    try:
        yield server
    finally:
        os.environ["ANTHROPIC_BASE_URL"] = previous
        server.stop()


def test_claude_tool_hook_decodes_live_payload() -> None:
    if os.environ.get("HARNESS_COMPAT_TOOL_REPLY") != "1":
        pytest.skip("mock tool-call replies require the framework stream")
    config = Path(os.environ["CLAUDE_CONFIG_DIR"])
    settings_path = config / "compat-hooks.json"
    capture = config / "hook-payloads.jsonl"
    capture_script = (
        "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
        "p.open('a').write(sys.stdin.read().strip()+'\\n')"
    )
    command = shlex.join([sys.executable, "-c", capture_script, str(capture)])
    plan = plan_launch(
        LaunchContext(
            participant_id="compat", prompt="", config_path=config / "mcp.json", approval="manual"
        )
    )
    overlay = install_native_hooks(
        HookInstallContext(
            participant_id="compat",
            channel_id=NATIVE_HOOK_CHANNEL,
            token_file=native_hook_token_path("compat"),
            theater_executable="theater",
            public_files=plan.files,
        )
    )
    settings = json.loads(next(iter(overlay.replacements.values())))
    assert {"SessionStart", "PreCompact", "PreToolUse", "PostToolUse"} <= settings["hooks"].keys()
    for entries in settings["hooks"].values():
        for entry in entries:
            for hook in entry["hooks"]:
                hook["command"] = command
                hook["async"] = False
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    local = os.environ.get("HARNESS_COMPAT_LOCAL_TOOL_REPLY") == "1"
    with _local_tool_mock(config) if local else nullcontext() as mock:
        _run_turn(settings=settings_path, prompt="Read hook-proof.txt, then reply done")
        if mock is not None:
            assert mock.requests and all(
                request.handled and request.authorized for request in mock.requests
            )

    def payloads() -> list[dict] | None:
        if not capture.exists():
            return None
        records = [json.loads(line) for line in capture.read_text().splitlines() if line]
        return (
            records
            if any(record.get("hook_event_name") == "PostToolUse" for record in records)
            else None
        )

    records = _wait_for(
        payloads, timeout=float(os.environ.get("HARNESS_COMPAT_HOOK_TIMEOUT", "45"))
    )
    assert {"SessionStart", "PreToolUse", "PostToolUse"} <= {
        record.get("hook_event_name") for record in records
    }
    post = next(record for record in records if record.get("hook_event_name") == "PostToolUse")  # type: ignore[union-attr]
    assert post["tool_name"] == "Read"
    native_id = correlate_tool_hook(
        HookCorrelationContext(
            participant_id="compat",
            channel_id=NATIVE_HOOK_CHANNEL,
            event="PostToolUse",
            payload=post,
            expected_session_id=post["session_id"],
            expected_transcript_location=post["transcript_path"],
            expected_session_provenance="exact",
        )
    )
    [decoded] = decode_tool_hook(
        HookDecodeContext(
            participant_id="compat",
            channel_id=NATIVE_HOOK_CHANNEL,
            event="PostToolUse",
            payload=post,
            native_id=native_id,
        )
    )
    assert decoded.fact.status is TrajectoryStatus.COMPLETED
    assert decoded.fact.source == "claude-hook"
