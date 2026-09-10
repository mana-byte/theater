"""Wave 0 proof: promptless native TUI bootstrap on a shared app-server backend.

Feasibility question (harness-runtime wiring plan §3.1–3.2): a control client
can call thread/start, but before the first turn there is no rollout and stock
`codex --remote unix://SOCKET resume THREAD_ID` fails with "no rollout found".
Can the stock native TUI instead be launched promptlessly as
`codex --remote unix://SOCKET` — no prompt, no resume id — create/attach its
own zero-turn thread, while a Theater-like observer discovers that exact
thread from native evidence, waits for event-based readiness, submits exactly
one turn/start, and keeps approval answers exclusively in the UI?

Verified answers against the unmodified installed release (codex-cli 0.154.0):

- YES, promptless bootstrap works. With no prompt and no resume id the stock
  TUI takes the `StartFresh` startup path and eagerly issues `thread/start`
  (codex-rs `tui/src/app/startup.rs`).
- YES, exact discovery works. `thread_start_task` broadcasts `thread/started`
  with the full Thread object to every initialized connection, so an observer
  that initialized before the UI learns the exact thread id — no cwd/time
  guessing (the cwd predicate only confirms the broadcast names this repo).
- NO, pre-subscription does not work at zero turns: `thread/resume` on the
  live zero-turn thread fails with the exact stock error
  `no rollout found for thread id <uuid>`. The observer therefore cannot
  subscribe before the first turn — this is a genuine finding, asserted as
  explicit evidence below, not worked around.
- The gap does not block readiness tracking: `thread/status/changed` is
  broadcast by the ThreadWatchManager to all initialized connections
  (codex-rs `app-server/src/thread_status.rs` + `outgoing_message.rs` — an
  empty connection list becomes `OutgoingEnvelope::Broadcast`), so the
  unsubscribed observer still sees idle/active/waitingOnApproval transitions.
- YES, approvals stay in the UI, structurally: approval requests are sent to
  the thread's *subscribed* connections
  (`ThreadScopedOutgoingMessageSender::send_request` +
  `ThreadStateManager::subscribed_connection_ids`), and `turn/start` does NOT
  subscribe its caller (only `thread/start`/`thread/resume` call
  `try_add_connection_to_thread`). The UI is the only subscribed connection,
  so the approval overlay and its `y` keybinding are the only answer path.
- YES, abrupt control-connection close is survivable mid-turn: the backend,
  the UI, and the turn all continue, and a fresh control client re-subscribes
  to the exact same thread via `thread/resume` (the rollout exists once the
  turn started) and receives `turn/completed` for the exact same turn.

One extra stock behavior the fixture must tolerate: after the first user
message the TUI spawns an ephemeral `ThreadSource::Feature("system")`
title-generation thread (codex-rs `tui/src/app/thread_title.rs` +
`tui/src/temporary_structured_request.rs`) and runs a structured turn through
it, embedding the first-turn prompt verbatim. Those requests carry
`text.format.type == "json_schema"` and are served filler streams by the mock.

The smoke test proves the full chain against the unmodified installed release:

1. isolated app-server on a private Unix socket, isolated CODEX_HOME, temp repo
2. Theater-like observing control client completes initialize/initialized
   first, so the later `thread/started` broadcast must reach it
3. real native TUI in tmux: `codex --remote unix://SOCKET`, no prompt, no resume
4. exact thread discovery from the `thread/started` broadcast; the zero-turn
   `thread/resume` attempt captures the exact stock error as evidence
5. UI readiness from state/event predicates: broadcast received with a live
   thread status, TUI process alive and rendered — no sleep
6. exactly one `turn/start` from the control client after readiness; the
   scripted model turn demands an escalated command; the approval reaches
   only the subscribed connection (the UI) and only the UI answers (`y`)
7. abrupt close of the control connection mid-turn (the mock provider holds
   the continuation); a fresh control client re-subscribes to the exact same
   thread and observes the same turn complete via `turn/completed`, while
   backend and UI stay alive

The offline tests cover the wire shapes and the mock's dispatch rules without
codex or tmux: RFC 6455 framing, the upgrade handshake, upstream's mock SSE
format and config template, the exact-evidence discovery predicate, and the
json_schema-based routing that keeps TUI-internal structured requests from
consuming the scripted turn streams.

Run the smoke (opt-in, real binary, real tmux on a private server):

    THEATER_CODEX_NATIVE_SMOKE=1 uv run --frozen pytest -q \\
        tests/test_codex_native_ui_bootstrap_proof.py -k native_ui_bootstrap

Everything else here runs offline and unconditionally.
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from types import ModuleType

import pytest

TESTS_DIR = Path(__file__).parent
_FIXTURES = TESTS_DIR / "fixtures" / "codex_native_ui_bootstrap"

#: The approval overlay title the stock TUI renders for an escalated command
#: (codex-rs `tui/src/bottom_pane/approval_overlay.rs`, Exec branch).
APPROVAL_OVERLAY_TITLE = "Would you like to run the following command?"

#: Default keybinding for ApprovalKeymap::approve in the stock TUI
#: (codex-rs `tui/src/keymap.rs`).
APPROVE_KEY = "y"

MARKER_FILENAME = "approval_marker.txt"
MARKER_CONTENT = "theater-ui-approval-proof"
PROOF_COMMAND = f"/bin/sh -c 'echo {MARKER_CONTENT} > {MARKER_FILENAME}'"
FIRST_TURN_PROMPT = "Run the proof command now."
FINAL_ASSISTANT_MESSAGE = "Theater bootstrap proof turn completed."

#: Exact stock error a zero-turn `thread/resume` answers with, on the
#: unmodified 0.154.0 app-server (the rollout is only materialised once the
#: first turn starts).
NO_ROLLOUT_ERROR_PREFIX = "no rollout found for thread id "


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


client = _load_module(
    "codex_ui_bootstrap_client", TESTS_DIR / "native" / "codex_ui_bootstrap_client.py"
)
mock_responses = _load_module("codex_native_bootstrap_mock", _FIXTURES / "mock_responses.py")
codex_env = _load_module("codex_native_bootstrap_env", _FIXTURES / "codex_env.py")


# ---------------------------------------------------------------------------
# Exact-evidence discovery predicate (pure, tested offline)
# ---------------------------------------------------------------------------


def discover_ui_thread(received: list[client.Received], *, cwd: str) -> dict:
    """Find the thread/started broadcast for exactly this cwd.

    Identity comes from the notification payload itself: the app-server
    broadcasts `thread/started` with the full Thread object, and we require an
    exact string match on its `cwd`. No creation-time window, no directory
    listing, no guessing.
    """
    matches = [
        received_event.payload["thread"]
        for received_event in received
        if received_event.kind == "notification"
        and received_event.method == "thread/started"
        and received_event.payload.get("thread", {}).get("cwd") == cwd
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one thread/started broadcast for cwd {cwd}, got {len(matches)}"
        )
    return matches[0]


def ui_ready_evidence(started_thread: dict) -> list[str]:
    """State/event predicates that gate the single turn/start, in order.

    The zero-turn `thread/resume` attempt has already failed with the exact
    stock error (captured separately), so readiness here rests on the
    `thread/started` broadcast itself: the thread must be live (idle) in the
    payload the app-server broadcast to every initialized connection.
    """
    status = started_thread.get("status")
    if not (isinstance(status, dict) and status.get("type") in ("idle", "active")):
        raise AssertionError(f"thread is not in a live state: {status!r}")
    return [
        f"thread/started broadcast delivered the exact id {started_thread['id']}",
        f"thread state is live: {status.get('type')}",
    ]


# ---------------------------------------------------------------------------
# Offline wire-shape tests
# ---------------------------------------------------------------------------


def test_client_text_frames_are_masked_and_round_trip() -> None:
    for size in (0, 5, 125, 300, 70_000):
        payload = bytes(range(256))[: min(size, 256)] * (size // 256 + 1)
        payload = payload[:size]
        mask = b"\x0f\x1e\x2d\x3c"
        frame = client.encode_client_text_frame(payload, mask)
        assert frame[0] == 0x81  # FIN + text
        assert frame[1] & 0x80, "client frames must set the mask bit"
        length = frame[1] & 0x7F
        offset = 2
        if length == 126:
            length = int.from_bytes(frame[2:4], "big")
            offset = 4
        elif length == 127:
            length = int.from_bytes(frame[2:10], "big")
            offset = 10
        assert length == size
        body = frame[offset:]
        assert len(body) == 4 + size
        unmasked = bytes(byte ^ body[i % 4] for i, byte in enumerate(body[4:]))
        assert unmasked == payload


def test_decode_server_frame_small_medium_large() -> None:
    for size in (3, 200, 70_000):
        payload = b"x" * size
        wire = _server_frame(payload)
        frame = client.decode_server_frame(wire)
        assert frame is not None
        assert frame.fin and frame.opcode == client.OP_TEXT
        assert frame.payload == payload
        assert client._frame_consumed(wire) == len(wire)


def test_decode_server_frame_waits_for_complete_header() -> None:
    payload = b"hello"
    wire = _server_frame(payload)
    for cut in range(len(wire)):
        assert client.decode_server_frame(wire[:cut]) is None
        assert client._frame_consumed(wire[:cut]) is None


def test_upgrade_request_carries_required_headers_and_no_origin() -> None:
    key = base64.b64encode(b"0123456789abcdef").decode()
    request = client.build_upgrade_request("/", key).decode("ascii")
    lines = request.split("\r\n")
    assert lines[0] == "GET / HTTP/1.1"
    assert not any(line.lower().startswith("origin:") for line in lines)
    headers = {line.split(":")[0].lower(): line.split(":", 1)[1].strip() for line in lines[1:-2]}
    assert headers["upgrade"] == "websocket"
    assert headers["connection"] == "Upgrade"
    assert headers["sec-websocket-version"] == "13"
    assert headers["sec-websocket-key"] == key


def test_expected_accept_header_matches_rfc_6455_vector() -> None:
    # RFC 6455 §1.3 example handshake.
    assert client.expected_accept_header("dGhlIHNhbXBsZSBub25jZQ==") == (
        "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="
    )


def test_escalated_sse_stream_matches_upstream_mock_format() -> None:
    stream = mock_responses.escalated_exec_call_response(PROOF_COMMAND, "call-proof-1", "m.txt")
    events = [line for line in stream.splitlines() if line.startswith("event: ")]
    assert [line.removeprefix("event: ") for line in events] == [
        "response.created",
        "response.output_item.done",
        "response.completed",
    ]
    data = [
        line.removeprefix("data: ") for line in stream.splitlines() if line.startswith("data: ")
    ]
    parsed = [json.loads(item) for item in data]
    assert parsed[0]["response"]["id"] == "resp-proof-1"
    call = parsed[1]["item"]
    assert call["type"] == "function_call"
    assert call["name"] == "exec_command"
    arguments = json.loads(call["arguments"])
    assert arguments["cmd"] == PROOF_COMMAND
    assert arguments["sandbox_permissions"] == "require_escalated"
    assert arguments["justification"].startswith("Theater native UI bootstrap proof")


def test_final_message_stream_completes_the_turn() -> None:
    stream = mock_responses.final_assistant_message_response(FINAL_ASSISTANT_MESSAGE)
    data = [
        json.loads(line.removeprefix("data: "))
        for line in stream.splitlines()
        if line.startswith("data: ")
    ]
    message = data[1]["item"]
    assert message["type"] == "message"
    assert message["content"][0]["text"] == FINAL_ASSISTANT_MESSAGE
    assert data[2]["type"] == "response.completed"


def test_mock_config_toml_mirrors_upstream_provider_template(tmp_path):
    config = codex_env.write_mock_config(tmp_path / "home", "http://127.0.0.1:9/v1")
    text = config.read_text()
    assert 'model_provider = "mock_provider"' in text
    assert 'base_url = "http://127.0.0.1:9/v1"' in text
    assert 'wire_api = "responses"' in text
    assert 'approval_policy = "on-request"' in text
    assert "request_max_retries = 0" in text


def test_git_repo_fixture_is_a_real_repo(tmp_path):
    repo = codex_env.make_git_repo(tmp_path / "repo")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    )
    assert head.stdout.strip()


def test_discover_ui_thread_requires_exactly_one_broadcast_for_cwd() -> None:
    thread = {"id": "019da1a1-bed9-7a43-88a2-b49d43915021", "cwd": "/tmp/repo"}

    def notification(method, payload):
        return client.Received(kind="notification", method=method, id=None, payload=payload)

    received = [
        notification("thread/tokenUsage/updated", {"threadId": "other"}),
        notification("thread/started", {"thread": thread}),
    ]
    assert discover_ui_thread(received, cwd="/tmp/repo") == thread
    with pytest.raises(AssertionError, match="exactly one thread/started"):
        discover_ui_thread(received, cwd="/tmp/different")
    duplicate = notification("thread/started", {"thread": dict(thread)})
    with pytest.raises(AssertionError, match="exactly one thread/started"):
        discover_ui_thread([*received, duplicate], cwd="/tmp/repo")


def test_ui_ready_evidence_requires_a_live_broadcast_status() -> None:
    thread = {"id": "t1", "status": {"type": "idle"}}
    assert ui_ready_evidence(thread) == [
        "thread/started broadcast delivered the exact id t1",
        "thread state is live: idle",
    ]
    with pytest.raises(AssertionError, match="not in a live state"):
        ui_ready_evidence({"id": "t1", "status": {"type": "notLoaded"}})


def test_is_structured_request_detects_json_schema_output_only() -> None:
    # Ordinary turn request: no `text` key at all.
    assert not mock_responses.is_structured_request({"model": "mock-model", "input": []})
    # Title/recap generation request: turn started with an `output_schema`.
    assert mock_responses.is_structured_request(
        {"model": "mock-model", "text": {"format": {"type": "json_schema", "strict": True}}}
    )
    assert not mock_responses.is_structured_request({"text": {}})
    assert not mock_responses.is_structured_request(None)


def test_mock_dispatch_routes_marked_turns_and_filler_to_structured() -> None:
    turn_request = json.dumps({"model": "mock-model", "input": [{"text": FIRST_TURN_PROMPT}]})
    title_request = json.dumps(
        {
            "model": "mock-model",
            "input": [{"text": "generate a title for: " + FIRST_TURN_PROMPT}],
            "text": {"format": {"type": "json_schema"}},
        }
    )
    exec_stream = mock_responses.escalated_exec_call_response(PROOF_COMMAND, "call-1", "m.txt")
    final_stream = mock_responses.final_assistant_message_response(FINAL_ASSISTANT_MESSAGE)
    mock = mock_responses.MockResponsesServer([exec_stream, final_stream], marker=FIRST_TURN_PROMPT)

    # A TUI-internal structured request embeds the prompt verbatim but must
    # get filler, without consuming the scripted turn streams.
    assert mock.next_stream("/v1/responses", title_request, structured=True) == mock.filler
    assert mock.streams == [exec_stream, final_stream]
    assert mock.requests == []

    # The real turn's requests consume the script in order.
    assert mock.next_stream("/v1/responses", turn_request) == exec_stream
    assert mock.next_stream("/v1/responses", turn_request) == final_stream
    assert mock.next_stream("/v1/responses", turn_request) is None


def test_mock_gate_holds_only_the_second_matched_stream() -> None:
    turn_request = json.dumps({"input": [{"text": FIRST_TURN_PROMPT}]})
    exec_stream = mock_responses.escalated_exec_call_response(PROOF_COMMAND, "call-1", "m.txt")
    final_stream = mock_responses.final_assistant_message_response(FINAL_ASSISTANT_MESSAGE)
    filler = mock_responses.final_assistant_message_response('{"title": "t"}')
    mock = mock_responses.MockResponsesServer(
        [exec_stream, final_stream], marker=FIRST_TURN_PROMPT, filler=filler
    )
    mock.hold_second_response()
    assert mock.next_stream("/v1/responses", turn_request) == exec_stream

    result: list[str] = []
    holder = threading.Thread(
        target=lambda: result.append(mock.next_stream("/v1/responses", turn_request))
    )
    holder.start()
    holder.join(timeout=1.0)
    assert holder.is_alive(), "second matched stream must be held on the gate"
    assert result == []
    mock.release_held_response()
    holder.join(timeout=5.0)
    assert result == [final_stream]


def _server_frame(payload: bytes) -> bytes:
    """Unmasked server text frame, the shape the app-server sends."""
    length = len(payload)
    if length < 126:
        header = bytes([0x81, length])
    elif length < 1 << 16:
        header = bytes([0x81, 126]) + length.to_bytes(2, "big")
    else:
        header = bytes([0x81, 127]) + length.to_bytes(8, "big")
    return header + payload


# ---------------------------------------------------------------------------
# Real-native smoke (opt-in)
# ---------------------------------------------------------------------------

SMOKE_ENV_VAR = "THEATER_CODEX_NATIVE_SMOKE"


def _skip_unless_smoke_enabled() -> None:
    if os.environ.get(SMOKE_ENV_VAR) != "1":
        pytest.skip(
            f"set {SMOKE_ENV_VAR}=1 to run the real codex native smoke; "
            "it launches the stock codex app-server and TUI"
        )
    if codex_env.codex_binary() is None:
        pytest.skip("codex is not on PATH")
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not on PATH")


def _tmux(tmux_socket: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", "-S", tmux_socket, *args], capture_output=True, text=True, check=check
    )


def _capture_pane(tmux_socket: str) -> str:
    result = _tmux(tmux_socket, "capture-pane", "-p", "-t", "cxb:0.0", check=False)
    return result.stdout


def _pane_alive(tmux_socket: str) -> bool:
    result = _tmux(tmux_socket, "list-panes", "-t", "cxb:0.0", "-F", "#{pane_pid}", check=False)
    if result.returncode != 0:
        return False
    pid = result.stdout.strip()
    if not pid.isdigit():
        return False
    probe = subprocess.run(
        ["ps", "-p", pid, "-o", "pid="], capture_output=True, text=True, check=False
    )
    return probe.returncode == 0 and probe.stdout.strip() != ""


def _spawn_backend(
    repo: Path, socket_path: Path, backend_log, backend_env: dict
) -> subprocess.Popen:
    """Launch the isolated stock app-server on a private Unix socket."""
    return subprocess.Popen(
        [
            codex_env.codex_binary() or "codex",
            "app-server",
            "--listen",
            f"unix://{socket_path}",
        ],
        cwd=repo,
        env=backend_env,
        stdout=backend_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _launch_tui(
    tmux_socket: Path, codex_home: Path, repo: Path, socket_path: Path, command: str
) -> None:
    """Launch the real native TUI, promptless, on this test's private tmux."""
    _tmux(
        str(tmux_socket),
        "new-session",
        "-d",
        "-x",
        "220",
        "-y",
        "50",
        "-s",
        "cxb",
        "-e",
        f"CODEX_HOME={codex_home}",
        "-c",
        str(repo),
        command,
    )


async def _wait_for_pane_text(tmux_socket: str, needle: str, *, timeout: float = 30.0) -> None:
    """Poll the pane for rendered text. Used only to drive the UI (find the
    approval overlay, confirm the turn rendered) — never for thread identity,
    which comes from notifications with exact ids."""
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        last = _capture_pane(tmux_socket)
        if needle in last:
            return
        await asyncio.sleep(0.1)
    pytest.fail(f"TUI pane never showed {needle!r}; last capture was:\n{last}")


async def _discover_ui_thread(
    observer: client.AppServerClient, repo: Path, marker_path: Path
) -> tuple[dict, str]:
    """Exact thread discovery from the thread/started broadcast, plus the
    explicit zero-turn resume failure that answers the feasibility question."""
    started = await observer.wait_for(
        "notification",
        method="thread/started",
        predicate=lambda event: event.payload.get("thread", {}).get("cwd") == str(repo),
        timeout=90.0,
    )
    started_thread = started.payload["thread"]
    thread_id = started_thread["id"]
    assert started_thread["cwd"] == str(repo)
    assert discover_ui_thread(observer.received, cwd=str(repo))["id"] == thread_id
    assert not marker_path.exists(), "UI bootstrap must not submit the prompt itself"

    # The stock app-server refuses to resume the zero-turn thread (no rollout
    # exists yet), so a control client cannot pre-subscribe before the first
    # turn. Captured as explicit evidence, not worked around.
    try:
        await observer.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
        raise AssertionError("zero-turn thread/resume unexpectedly succeeded")
    except client.RequestError as err:
        assert str(err) == f"{NO_ROLLOUT_ERROR_PREFIX}{thread_id}", (
            f"unexpected zero-turn resume error: {err}"
        )
    return started_thread, thread_id


async def _submit_exactly_one_turn(
    observer: client.AppServerClient,
    tmux_socket: str,
    thread_id: str,
    marker_path: Path,
) -> str:
    """The single turn/start after readiness; the approval it triggers is
    answered exclusively in the native UI."""
    turn = await observer.request(
        "turn/start",
        {
            "threadId": thread_id,
            "input": [{"type": "text", "text": FIRST_TURN_PROMPT}],
        },
    )
    turn_id = turn["turn"]["id"]
    assert turn["turn"]["status"] == "inProgress"
    assert observer.sent.count_outgoing_requests("turn/start") == 1

    # The model demands an escalated command; the unsubscribed control client
    # sees the thread block on approval via the broadcast thread/status/changed
    # (waitingOnApproval flag), while the approval *request* itself goes only
    # to the subscribed connection: the UI.
    await observer.wait_for(
        "notification",
        method="thread/status/changed",
        predicate=lambda event: (
            event.payload.get("threadId") == thread_id
            and "waitingOnApproval" in (event.payload.get("status", {}).get("activeFlags") or [])
        ),
    )
    assert not any(received.kind == "server_request" for received in observer.received), (
        "turn-scoped requests (approvals) must not reach the unsubscribed control client"
    )
    assert not observer.sent.responses_to_server_requests, (
        "the Theater control client must never answer an approval"
    )

    # Drive the UI like the human it belongs to: wait for the overlay,
    # then press the stock approve key.
    await _wait_for_pane_text(tmux_socket, APPROVAL_OVERLAY_TITLE)
    _tmux(tmux_socket, "send-keys", "-t", "cxb:0.0", APPROVE_KEY)

    # The approval resolved and the command ran: marker written, and the
    # broadcast shows the thread active again with the flag cleared.
    await client.wait_until(marker_path.exists, timeout=30.0)
    assert marker_path.read_text().strip() == MARKER_CONTENT
    await observer.wait_for(
        "notification",
        method="thread/status/changed",
        predicate=lambda event: (
            event.payload.get("threadId") == thread_id
            and event.payload.get("status", {}).get("type") == "active"
            and "waitingOnApproval"
            not in (event.payload.get("status", {}).get("activeFlags") or [])
        ),
    )
    return turn_id


async def _reconnect_midturn(
    observer2: client.AppServerClient, thread_id: str, turn_id: str, tmux_socket: str
) -> None:
    """A fresh control client re-subscribes to the exact same thread while the
    turn is provably still active (the mock is holding its continuation)."""
    resume = await observer2.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
    assert resume["thread"]["id"] == thread_id, (
        "a new control client must reconnect to the exact same thread"
    )
    assert resume["thread"]["status"]["type"] == "active", (
        "the reconnected client must see the same live turn still running"
    )
    assert _pane_alive(tmux_socket), "UI died when the control connection was dropped"


async def _witness_completion(
    observer2: client.AppServerClient,
    tmux_socket: str,
    thread_id: str,
    turn_id: str,
) -> None:
    """The reconnected, subscribed client watches the exact same turn finish,
    and the thread history names the same turn with the same items."""
    completed = await observer2.wait_for(
        "notification",
        method="turn/completed",
        predicate=lambda event: event.payload.get("threadId") == thread_id,
        timeout=60.0,
    )
    completed_turn = completed.payload["turn"]
    assert completed_turn["id"] == turn_id, (
        "the reconnected client must see the control client's turn finish"
    )
    assert completed_turn["status"] == "completed"
    assert any(
        item.get("type") == "agentMessage" and item.get("text") == FINAL_ASSISTANT_MESSAGE
        for item in completed_turn["items"]
    ), f"turn/completed items lack the final message: {completed_turn['items']}"
    await observer2.wait_for(
        "notification",
        method="thread/status/changed",
        predicate=lambda event: (
            event.payload.get("threadId") == thread_id
            and event.payload.get("status", {}).get("type") == "idle"
        ),
        timeout=30.0,
    )

    # UI and control client provably shared the same turn: the UI rendered the
    # exact assistant message that ended the Theater-submitted turn, and the
    # post-turn history names the exact same turn id with the same items.
    await _wait_for_pane_text(tmux_socket, FINAL_ASSISTANT_MESSAGE)
    resume = await observer2.request(
        "thread/resume", {"threadId": thread_id, "excludeTurns": False}
    )
    matching = [
        turn_entry for turn_entry in resume["thread"]["turns"] if turn_entry["id"] == turn_id
    ]
    assert len(matching) == 1, f"thread history must contain turn {turn_id} exactly once"
    history_items = matching[0]["items"]
    assert any(item.get("type") == "agentMessage" for item in history_items)
    exec_items = [item for item in history_items if item.get("id") == "call-proof-1"]
    assert len(exec_items) == 1 and exec_items[0]["exitCode"] == 0


async def _teardown(
    observers: list[client.AppServerClient],
    mock: mock_responses.MockResponsesServer,
    backend: subprocess.Popen | None,
    backend_log,
    tmux_socket: str,
    root: Path,
) -> None:
    """Deterministic cleanup: nothing survives the test, nothing is shared."""
    for observer_client in observers:
        await observer_client.aclose()
    mock.release_held_response()
    mock.stop()
    _tmux(tmux_socket, "kill-server", check=False)
    if backend is not None and backend.poll() is None:
        os.killpg(os.getpgid(backend.pid), signal.SIGTERM)
        try:
            backend.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(backend.pid), signal.SIGKILL)
            backend.wait(timeout=5)
    backend_log.close()
    shutil.rmtree(root, ignore_errors=True)


def _make_mock(marker_path: Path) -> mock_responses.MockResponsesServer:
    """Scripted provider: escalated exec (approval proof) then the final
    assistant message, with TUI-internal structured requests routed to
    filler and the second turn stream gated for the mid-turn abrupt close."""
    mock = mock_responses.MockResponsesServer(
        [
            mock_responses.escalated_exec_call_response(
                PROOF_COMMAND, "call-proof-1", str(marker_path)
            ),
            mock_responses.final_assistant_message_response(FINAL_ASSISTANT_MESSAGE),
        ],
        marker=FIRST_TURN_PROMPT,
    )
    mock.hold_second_response()
    return mock


def _assert_sentinels(
    observer: client.AppServerClient,
    observer2: client.AppServerClient,
    mock: mock_responses.MockResponsesServer,
) -> None:
    """Exactly one first-turn prompt, only after readiness; no approval was
    ever answered from a control connection."""
    assert observer.sent.count_outgoing_requests("turn/start") == 1
    assert observer2.sent.count_outgoing_requests("turn/start") == 0
    assert not observer.sent.responses_to_server_requests
    assert not observer2.sent.responses_to_server_requests
    # The mock's dispatch record proves the turn's two scripted model calls
    # happened and the title-generation request got filler.
    matched_requests = [request for request in mock.requests if request.matched]
    assert len(matched_requests) == 2, (
        f"expected exactly two turn model calls, got {len(matched_requests)}"
    )
    assert any(request.structured for request in mock.requests), (
        "expected the TUI's structured title-generation request to hit the mock"
    )


@pytest.mark.tmux
async def test_codex_native_ui_bootstrap_proof() -> None:
    """One full chain, one live backend, one UI, one prompt — asserted at
    every seam against the unmodified installed codex release."""
    _skip_unless_smoke_enabled()
    version = codex_env.codex_version()
    assert version and version.startswith("codex-cli 0.154.0"), (
        f"expected codex-cli 0.154.0, found {version!r}"
    )

    # resolve() up front: the app-server canonicalises paths, and on macOS
    # /tmp is a symlink to /private/tmp — the exact-cwd predicate below must
    # compare canonical with canonical, not symlinked with canonical.
    root = Path(tempfile.mkdtemp(prefix="cxb-", dir="/tmp")).resolve()
    codex_home = root / "codex-home"
    repo = codex_env.make_git_repo(root / "repo")
    socket_path = root / "app.sock"
    tmux_socket = root / "tmux-socket"
    marker_path = repo / MARKER_FILENAME

    backend_log = (root / "backend.log").open("w")
    backend_env = dict(os.environ)
    backend_env.update(CODEX_HOME=str(codex_home), TERM="xterm-256color")
    backend_env.pop("TMUX", None)
    backend_env.pop("TMUX_PANE", None)

    backend: subprocess.Popen | None = None
    mock = _make_mock(marker_path)
    observers: list[client.AppServerClient] = []

    try:
        mock.start()
        codex_env.write_mock_config(codex_home, mock.base_url)

        # 1. isolated backend on a private unix socket
        backend = _spawn_backend(repo, socket_path, backend_log, backend_env)
        await client.wait_until(socket_path.exists, timeout=30.0)
        await client.wait_until(lambda: client.socket_connectable(str(socket_path)), timeout=30.0)

        # 2. Theater-like observer completes initialize/initialized first, so
        #    every later broadcast (thread/started, thread/status/changed)
        #    must be delivered to it.
        observer = client.AppServerClient(str(socket_path), name="theater-observer")
        observers.append(observer)
        await observer.connect()
        await observer.initialize()

        # 3. the real native TUI, promptless, no resume id, on a private tmux
        #    server that belongs to this test alone.
        frontend_command = f"codex --remote unix://{socket_path}"
        assert "resume" not in frontend_command and FIRST_TURN_PROMPT not in frontend_command
        _launch_tui(tmux_socket, codex_home, repo, socket_path, frontend_command)

        # 4. exact thread discovery from native evidence; the zero-turn
        #    thread/resume failure is captured as explicit evidence.
        started_thread, thread_id = await _discover_ui_thread(observer, repo, marker_path)

        # 5. event/state readiness — the broadcast itself plus a live UI
        #    process; no sleep anywhere in this chain
        for line in ui_ready_evidence(started_thread):
            assert line
        assert _pane_alive(str(tmux_socket)), "TUI pane exited before readiness"
        await _wait_for_pane_text(str(tmux_socket), "OpenAI Codex")

        # 6. exactly one turn/start from the control client, after readiness;
        #    the approval it triggers is answered exclusively in the UI.
        turn_id = await _submit_exactly_one_turn(observer, str(tmux_socket), thread_id, marker_path)

        # 7. abrupt close of the control connection mid-turn (the held model
        #    continuation proves the turn is still active), then a fresh
        #    control client re-subscribes to the exact same thread.
        await observer.close_abrupt()
        observer2 = client.AppServerClient(str(socket_path), name="theater-observer-2")
        observers.append(observer2)
        await observer2.connect()
        await observer2.initialize()
        await _reconnect_midturn(observer2, thread_id, turn_id, str(tmux_socket))

        mock.release_held_response()
        await _witness_completion(observer2, str(tmux_socket), thread_id, turn_id)

        _assert_sentinels(observer, observer2, mock)
    finally:
        await _teardown(observers, mock, backend, backend_log, str(tmux_socket), root)
