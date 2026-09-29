"""Offline mock OpenAI Responses provider for Codex native qualification.

Drives every Phase-3 behaviour case (handshake, thread lifecycle, steer,
interrupt, concurrent-submission race, approvals, settings gating, UI
topology) with no network and no model quota, against the unmodified codex
app-server binary. It is a sibling of the fixed-sequence bootstrap mock in
``tests/fixtures/codex_native_ui_bootstrap/mock_responses.py`` and follows
the same upstream shape (``app-server/tests/common/mock_model_server.rs`` +
``core/tests/common/responses.rs``).

Observed against codex-cli 0.154.0 (see docs/native-interaction/codex.md):

- ``turn/steer`` does not abort the in-flight model request: the steer text is
  queued and a new request whose *last* user message is the steer text is
  issued only after the current stream completes. A held stream therefore
  keeps the turn active until a bounded deadline, and the steer request must
  match on the last user message, not on the whole body.
- ``turn/start`` during an active turn behaves the same way (start_or_steer).
- ``turn/interrupt`` aborts the in-flight request; the held stream detects the
  disconnect through periodic SSE comment keepalives.
- Stock-TUI title generation sends structured requests carrying
  ``text.format.type == "json_schema"``; they get filler and never consume
  the scripted streams.
- The app-server also POSTs turn-cost analytics under ``<base_url>/analytics``;
  it is answered 204 and never scripted.

Test helper only: no production code may import it.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

SSE_CONTENT_TYPE = "text/event-stream"
MOCK_MODEL = "mock-model"
MOCK_PROVIDER_ID = "mock_provider"

#: Held-stream tuning: keepalive cadence and the default mid-turn hold.
KEEPALIVE_INTERVAL = 0.25
DEFAULT_HOLD_SECONDS = 5.0
#: Bounded request log; a qualification run must not grow it without bound.
MAX_RECORDED_REQUESTS = 256
_PREVIEW_CHARS = 120

_ECHO_REPLY = re.compile(r"Reply with exactly:\s*(\S+)")
_TOUCH_COMMAND = re.compile(r"run exactly this command now:\s*(.+?)\.(?:\s|$)")
_ESCALATED_JUSTIFICATION = (
    "Theater mock qualification: this command writes the approval target the "
    "capture waits for and nothing else."
)

_CONFIG_TEMPLATE = """\
model = "{model}"
model_provider = "{provider_id}"
approval_policy = "on-request"
sandbox_mode = "read-only"
project_trust_level = "trusted"
# Releases that check for updates render a blocking update dialog in the
# stock TUI before the composer; qualification drives that same TUI.
check_for_update_on_startup = false

[model_providers.{provider_id}]
name = "Mock provider for Theater Codex qualification"
base_url = "{base_url}"
wire_api = "responses"
request_max_retries = 0
stream_max_retries = 0
"""


# ---------------------------------------------------------------------------
# SSE event builders — the wire shapes of the OpenAI Responses API
# ---------------------------------------------------------------------------


def _ev(event: dict[str, Any]) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


def sse(events: list[dict[str, Any]]) -> str:
    return "".join(_ev(event) for event in events)


def ev_response_created(response_id: str) -> dict[str, Any]:
    return {"type": "response.created", "response": {"id": response_id}}


def ev_message_item_added(item_id: str) -> dict[str, Any]:
    return {
        "type": "response.output_item.added",
        "output_index": 0,
        "item": {"type": "message", "role": "assistant", "id": item_id, "content": []},
    }


def ev_content_part_added(item_id: str) -> dict[str, Any]:
    return {
        "type": "response.content_part.added",
        "item_id": item_id,
        "output_index": 0,
        "content_index": 0,
        "part": {"type": "output_text", "text": ""},
    }


def ev_output_text_delta(item_id: str, text: str) -> dict[str, Any]:
    return {
        "type": "response.output_text.delta",
        "item_id": item_id,
        "output_index": 0,
        "content_index": 0,
        "delta": text,
    }


def ev_message_item_done(item_id: str, text: str) -> dict[str, Any]:
    return {
        "type": "response.output_item.done",
        "item": {
            "type": "message",
            "role": "assistant",
            "id": item_id,
            "content": [{"type": "output_text", "text": text}],
        },
    }


def ev_function_call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {
        "type": "response.output_item.done",
        "item": {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments},
    }


def ev_response_completed(response_id: str) -> dict[str, Any]:
    return {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "usage": {
                "input_tokens": 1,
                "input_tokens_details": None,
                "output_tokens": 1,
                "output_tokens_details": None,
                "total_tokens": 2,
            },
        },
    }


# ---------------------------------------------------------------------------
# Stream scripts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StreamScript:
    """One scripted SSE stream: pre events, an optional hold, post events.

    With ``hold_seconds > 0`` the pre events are written and flushed, then
    the stream blocks on keepalive comments until the deadline passes or the
    client (codex) disconnects — the seam steer/interrupt/race cases need to
    keep a turn provably active.
    """

    events: tuple[dict[str, Any], ...]
    hold_seconds: float = 0.0
    hold_split_index: int = 0

    def pre_events(self) -> list[dict[str, Any]]:
        return list(self.events[: self.hold_split_index])

    def post_events(self) -> list[dict[str, Any]]:
        return list(self.events[self.hold_split_index :])


def final_message_stream(text: str, *, item_id: str = "msg-mock-1") -> StreamScript:
    """A complete assistant message; the ordinary short turn."""
    return StreamScript(
        (
            ev_response_created("resp-mock-1"),
            ev_message_item_added(item_id),
            ev_content_part_added(item_id),
            ev_output_text_delta(item_id, text),
            ev_message_item_done(item_id, text),
            ev_response_completed("resp-mock-1"),
        )
    )


def held_essay_stream(
    delta_text: str, *, hold_seconds: float = DEFAULT_HOLD_SECONDS, item_id: str = "msg-mock-1"
) -> StreamScript:
    """A stream that stays mid-turn (deltas, then blocked) for ``hold_seconds``.

    The post-hold tail still completes the stream cleanly, so an un-steered
    held turn settles instead of hanging the capture.
    """
    events = (
        ev_response_created("resp-mock-1"),
        ev_message_item_added(item_id),
        ev_content_part_added(item_id),
        ev_output_text_delta(item_id, delta_text),
        ev_message_item_done(item_id, delta_text),
        ev_response_completed("resp-mock-1"),
    )
    return StreamScript(events, hold_seconds=hold_seconds, hold_split_index=4)


def escalated_exec_call_stream(command: str, *, call_id: str = "call-mock-1") -> StreamScript:
    """The only output item is an escalated exec call: an approval must fire."""
    arguments = json.dumps(
        {
            "cmd": command,
            "workdir": None,
            "yield_time_ms": 500,
            "sandbox_permissions": "require_escalated",
            "justification": _ESCALATED_JUSTIFICATION,
        }
    )
    return StreamScript(
        (
            ev_response_created("resp-mock-1"),
            ev_function_call(call_id, "exec_command", arguments),
            ev_response_completed("resp-mock-1"),
        )
    )


def structured_filler_stream(text: str = '{"title": "theater mock qualification"}') -> StreamScript:
    """Filler for TUI-internal structured (json_schema) requests."""
    return final_message_stream(text, item_id="msg-mock-filler")


#: A script factory receives the request's last user message and returns the
#: stream to serve; rules are matched on that text (see module docstring).
ScriptFactory = Callable[[str], StreamScript]


def fifo_script(streams: list[StreamScript], exhausted: StreamScript) -> ScriptFactory:
    """Consume the scripted streams in order, then fall back to ``exhausted``."""
    queue = deque(streams)
    lock = threading.Lock()

    def factory(_last_user: str) -> StreamScript:
        with lock:
            return queue.popleft() if queue else exhausted

    return factory


def echo_exact_reply_stream(last_user: str) -> StreamScript:
    """Serve the token a ``Reply with exactly: <token>`` prompt asks for."""
    match = _ECHO_REPLY.search(last_user)
    token = match.group(1) if match else "ok"
    return final_message_stream(token)


@dataclass(frozen=True)
class Rule:
    """Dispatch rule: ``marker`` matched against the request's last user message."""

    marker: str
    script: ScriptFactory

    def matches(self, last_user: str) -> bool:
        return self.marker in last_user


def standard_rules(*, hold_seconds: float = DEFAULT_HOLD_SECONDS) -> list[Rule]:
    """The script set covering every collector and smoke-suite Phase-3 case.

    Order is significant: the approval prompt carries no ``Reply with exactly``
    token, and the race prompt contains the word ``essay``; every marker is
    matched against the last user message only, so each request is
    deterministic even with conversation history accumulated in the body.
    """
    return [
        # Approval: content-derived from the capture's own prompt — the exec
        # call runs the exact named command; the post-approval follow-up (the
        # target now exists) gets the final "ok" message.
        Rule("Use the shell tool to run exactly this command now", exec_call_then_ok_factory),
        Rule("Reply with exactly", echo_exact_reply_stream),
        Rule(
            "essay about rivers",
            fifo_script(
                [
                    held_essay_stream(
                        "the wide rivers run toward the sea", hold_seconds=hold_seconds
                    )
                ],
                final_message_stream("ok"),
            ),
        ),
        Rule(
            "essay about mountains",
            fifo_script(
                [held_essay_stream("the high mountains hold the snow", hold_seconds=hold_seconds)],
                final_message_stream("ok"),
            ),
        ),
        Rule(
            "essay about the sea",
            fifo_script(
                [held_essay_stream("the grey sea keeps its silence", hold_seconds=hold_seconds)],
                final_message_stream("ok"),
            ),
        ),
        Rule(
            "queued and run",
            fifo_script([final_message_stream("queued")], final_message_stream("ok")),
        ),
    ]


def exec_call_then_ok_factory(last_user: str) -> StreamScript:
    """Stateless per-request choice: exec call while the target file is absent.

    The approval capture deletes the target before the turn, so the first
    request (prompt only) scripts the exec call; the follow-up request after
    the approved command ran (target now exists) gets the final message. The
    command is taken verbatim from the prompt, so the same rule serves both
    the collector and the smoke suite.
    """
    match = _TOUCH_COMMAND.search(last_user)
    if match is None:
        return final_message_stream("ok")
    target = match.group(1).strip()
    target_path = Path(target.removeprefix("touch ").strip().strip("'\""))
    if target.startswith("touch") and target_path.exists():
        return final_message_stream("ok")
    return escalated_exec_call_stream(target)


# ---------------------------------------------------------------------------
# Request parsing (pure; unit-tested offline)
# ---------------------------------------------------------------------------


def last_user_message_text(body: dict[str, Any] | None) -> str:
    """The final user message's text: the current prompt, history excluded.

    Steer and start_or_steer requests append the new input after the prior
    turn's messages, so dispatching on the last user message — not the whole
    body — is what keeps concurrent and follow-up requests deterministic.
    """
    if not isinstance(body, dict):
        return ""
    for item in reversed(body.get("input") or []):
        if item.get("type") == "message" and item.get("role") == "user":
            content = item.get("content")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                for part in content:
                    if part.get("type") in ("input_text", "text"):
                        return part.get("text") or ""
    return ""


def is_structured_request(body: dict[str, Any] | None) -> bool:
    """True for structured-output requests (``text.format.type == json_schema``)."""
    if not isinstance(body, dict):
        return False
    text = body.get("text")
    return isinstance(text, dict) and text.get("format", {}).get("type") == "json_schema"


@dataclass
class RecordedRequest:
    """Bounded, redacted request log entry for debugging and assertions."""

    path: str
    structured: bool
    marker: str | None
    last_user_preview: str


# ---------------------------------------------------------------------------
# The server
# ---------------------------------------------------------------------------


class _MockServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, provider: MockResponsesProvider) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.provider = provider


class _Handler(BaseHTTPRequestHandler):
    server: _MockServer

    def log_message(self, *_args: Any) -> None:  # keep pytest output clean
        return

    @property
    def provider(self) -> MockResponsesProvider:
        return self.server.provider

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body: dict[str, Any] | None = json.loads(raw) if raw else {}
        except ValueError:
            body = None
        structured = is_structured_request(body)
        last_user = "" if structured else last_user_message_text(body)
        script, marker = self.provider.select_script(last_user, structured=structured)
        self.provider.record(
            RecordedRequest(self.path, structured, marker, last_user[:_PREVIEW_CHARS])
        )
        if not self.path.endswith("/responses"):
            # Turn-cost analytics and other side channels: harmless, never scripted.
            self.send_response(204)
            self.end_headers()
            return
        self._serve(script)

    def _serve(self, script: StreamScript) -> None:
        self.send_response(200)
        self.send_header("Content-Type", SSE_CONTENT_TYPE)
        self.end_headers()
        try:
            if script.hold_seconds <= 0:
                self.wfile.write(sse(list(script.events)).encode())
                self.wfile.flush()
                return
            self.wfile.write(sse(script.pre_events()).encode())
            self.wfile.flush()
            if not self._hold(script.hold_seconds):
                return  # codex aborted the request (steer/interrupt raced us)
            self.wfile.write(sse(script.post_events()).encode())
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return  # client disconnected mid-stream; nothing to salvage

    def _hold(self, seconds: float) -> bool:
        """Block mid-stream until the deadline; False once the client is gone."""
        deadline = time.monotonic() + seconds
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return True
            time.sleep(min(KEEPALIVE_INTERVAL, remaining))
            try:
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

    def do_GET(self) -> None:
        if self.path.endswith("/models"):
            encoded = json.dumps(
                {"data": [{"id": MOCK_MODEL, "object": "model", "owned_by": "theater"}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        self.send_response(404)
        self.end_headers()


class MockResponsesProvider:
    """Scripted Responses provider on a private loopback port.

    Structured (json_schema) requests always get filler; ordinary requests are
    matched against the rules' markers in order, on the last user message;
    anything unmatched gets the default short reply.
    """

    def __init__(
        self,
        rules: list[Rule] | None = None,
        *,
        default: StreamScript | None = None,
        filler: StreamScript | None = None,
    ) -> None:
        self.rules = standard_rules() if rules is None else list(rules)
        self.default = default if default is not None else final_message_stream("ok")
        self.filler = filler if filler is not None else structured_filler_stream()
        self.requests: list[RecordedRequest] = []
        self._lock = threading.Lock()
        self._httpd: _MockServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._httpd = _MockServer(self)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    @property
    def base_url(self) -> str:
        assert self._httpd is not None, "start() the provider before asking its base_url"
        host = self._httpd.server_address[0]
        port = self._httpd.server_address[1]
        host_text = host.decode() if isinstance(host, bytes) else str(host)
        return f"http://{host_text}:{port}/v1"

    def select_script(
        self, last_user: str, *, structured: bool = False
    ) -> tuple[StreamScript, str | None]:
        """Pure dispatch: (script, matched marker or None). Never serves."""
        if structured:
            return self.filler, "<structured>"
        for rule in self.rules:
            if rule.matches(last_user):
                return rule.script(last_user), rule.marker
        return self.default, None

    def record(self, request: RecordedRequest) -> None:
        with self._lock:
            if len(self.requests) >= MAX_RECORDED_REQUESTS:
                self.requests.pop(0)
            self.requests.append(request)

    def matched_requests(self, marker: str) -> list[RecordedRequest]:
        return [request for request in self.requests if request.marker == marker]


def write_mock_home(
    root: Path, *, trusted_paths: list[Path], base_url: str, model: str = MOCK_MODEL
) -> Path:
    """An isolated CODEX_HOME pointing at the mock provider.

    Mirrors the ambient isolation writer's trust entries so the app-server and
    the stock TUI never touch the developer's real ``~/.codex``.
    """
    codex_home = root / "home"
    codex_home.mkdir(parents=True, exist_ok=True)
    config = codex_home / "config.toml"
    config.write_text(
        _CONFIG_TEMPLATE.format(model=model, provider_id=MOCK_PROVIDER_ID, base_url=base_url)
    )
    with config.open("a") as handle:
        handle.write("\n# theater mock-provider qualification isolation\n")
        for trusted in trusted_paths:
            handle.write(f'\n[projects."{trusted}"]\ntrust_level = "trusted"\n')
    return codex_home


@dataclass
class MockSession:
    """A started provider plus the codex home wired to it; teardown in ``close``."""

    provider: MockResponsesProvider
    codex_home: Path

    @classmethod
    def open(
        cls, root: Path, *, trusted_paths: list[Path], rules: list[Rule] | None = None
    ) -> MockSession:
        provider = MockResponsesProvider(rules)
        provider.start()
        codex_home = write_mock_home(root, trusted_paths=trusted_paths, base_url=provider.base_url)
        return cls(provider, codex_home)

    def close(self) -> None:
        self.provider.stop()
