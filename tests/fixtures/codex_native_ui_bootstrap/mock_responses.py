"""Mock OpenAI Responses-API server for the Codex native UI bootstrap proof.

Replicates, in stdlib Python, the mock the upstream codex-rs test suite runs
against (`app-server/tests/common/mock_model_server.rs` +
`core/tests/common/responses.rs`): an HTTP server whose `/v1/responses`
endpoint answers POSTs with a scripted sequence of SSE streams.

The first scripted stream emits an escalated `exec_command` tool call, which
forces the app-server to send `item/commandExecution/requestApproval` to every
subscribed connection — the native approval whose ownership must stay in the
UI. The second stream emits a final assistant message and completes the turn.

Dispatch is content-aware. The stock TUI spawns extra model requests that are
not part of the turn under test: after the first user message it creates an
ephemeral `ThreadSource::Feature("system")` title-generation thread
(codex-rs `tui/src/app/thread_title.rs` +
`tui/src/temporary_structured_request.rs`) and runs a structured turn through
it. Those requests would otherwise consume the scripted streams out of order, and
they embed the first-turn prompt verbatim, so a marker alone cannot identify
them. They are distinguishable on the wire: every structured request carries
`text.format.type == "json_schema`" (the turn's `output_schema`), which no
ordinary turn request has. Requests whose body contains `marker` (the exact
first-turn prompt) and no json_schema format receive the scripted streams;
every other request receives `filler`.

A gate lets the test hold the second marker-matched stream mid-turn, so the
control connection can be dropped abruptly while the backend is provably
alive and working.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

SSE_CONTENT_TYPE = "text/event-stream"


def ev_response_created(response_id: str) -> dict[str, Any]:
    return {"type": "response.created", "response": {"id": response_id}}


def ev_function_call(call_id: str, name: str, arguments: str) -> dict[str, Any]:
    return {
        "type": "response.output_item.done",
        "item": {"type": "function_call", "call_id": call_id, "name": name, "arguments": arguments},
    }


def ev_assistant_message(message_id: str, text: str) -> dict[str, Any]:
    return {
        "type": "response.output_item.done",
        "item": {
            "type": "message",
            "role": "assistant",
            "id": message_id,
            "content": [{"type": "output_text", "text": text}],
        },
    }


def ev_completed(response_id: str) -> dict[str, Any]:
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


def sse(events: list[dict[str, Any]]) -> str:
    """Encode events exactly like upstream `core/tests/common/responses.rs::sse`."""
    out: list[str] = []
    for event in events:
        out.append(f"event: {event['type']}\n")
        out.append(f"data: {json.dumps(event)}\n\n")
    return "".join(out)


def escalated_exec_call_response(command: str, call_id: str, marker_path: str) -> str:
    """SSE stream whose only output item demands escalated command execution."""
    arguments = json.dumps(
        {
            "cmd": command,
            "workdir": None,
            "yield_time_ms": 500,
            "sandbox_permissions": "require_escalated",
            "justification": (
                "Theater native UI bootstrap proof: this command writes the "
                f"approval marker file {marker_path} and nothing else."
            ),
        }
    )
    return sse(
        [
            ev_response_created("resp-proof-1"),
            ev_function_call(call_id, "exec_command", arguments),
            ev_completed("resp-proof-1"),
        ]
    )


def final_assistant_message_response(text: str) -> str:
    return sse(
        [
            ev_response_created("resp-proof-2"),
            ev_assistant_message("msg-proof-1", text),
            ev_completed("resp-proof-2"),
        ]
    )


@dataclass
class RecordedRequest:
    path: str
    body: dict[str, Any] | None = field(default=None)
    matched: bool = False
    structured: bool = False


def is_structured_request(body: dict[str, Any] | None) -> bool:
    """True for a structured-output request (turn with an `output_schema`).

    The TUI's title/recap generation turns set a JSON schema, which the
    provider request carries as `text.format.type == "json_schema"`. Ordinary
    turn requests have no `text` key at all.
    """
    if not isinstance(body, dict):
        return False
    text = body.get("text")
    return isinstance(text, dict) and text.get("format", {}).get("type") == "json_schema"


class _MockServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with a reference back to the scripted mock."""

    daemon_threads = True

    def __init__(self, mock: MockResponsesServer) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.mock = mock


class _Handler(BaseHTTPRequestHandler):
    server: _MockServer

    @property
    def mock(self) -> MockResponsesServer:
        return self.server.mock

    def _cors_headers(self) -> None:  # pragma: no cover - trivial plumbing
        self.send_header("Access-Control-Allow-Origin", "*")

    def do_OPTIONS(self) -> None:  # pragma: no cover - codex does not preflight
        self.send_response(204)
        self._cors_headers()
        self.end_headers()

    def _sse_reply(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", SSE_CONTENT_TYPE)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            parsed: dict[str, Any] | None = json.loads(raw)
        except ValueError:
            parsed = None
        body_text = raw.decode("utf-8", "replace")
        structured = is_structured_request(parsed)
        matched = self.mock.request_matches(body_text, structured=structured)
        self.mock.requests.append(
            RecordedRequest(path=self.path, body=parsed, matched=matched, structured=structured)
        )
        if self.path.endswith("/responses"):
            stream = self.mock.next_stream(self.path, body_text, structured=structured)
            if stream is None:
                self._sse_reply(sse([ev_completed("resp-proof-overflow")]))
                return
            self._sse_reply(stream)
            return
        self.send_response(404)
        self.end_headers()

    def do_GET(self) -> None:
        self.mock.requests.append(RecordedRequest(path=self.path))
        if self.path.endswith("/models"):
            encoded = json.dumps(
                {"data": [{"id": "mock-model", "object": "model", "owned_by": "theater"}]}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *_args: Any) -> None:  # keep pytest output clean
        return


class MockResponsesServer:
    """Scripted Responses-API server on a private loopback port.

    With `marker` set, only requests whose body contains `marker` consume the
    scripted streams; other requests get `filler`. With `marker` unset, every
    request consumes the next scripted stream (upstream's plain FIFO mock).
    """

    def __init__(
        self,
        streams: list[str],
        *,
        marker: str | None = None,
        filler: str | None = None,
    ) -> None:
        self.streams = list(streams)
        self.marker = marker
        self.filler = (
            filler
            if filler is not None
            else final_assistant_message_response('{"title": "Theater bootstrap proof"}')
        )
        self.requests: list[RecordedRequest] = []
        self._lock = threading.Lock()
        self._gate = threading.Event()
        self._hold_second_response = False
        self._served_count = 0
        self._httpd = _MockServer(self)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}/v1"

    def request_matches(self, body_text: str | None, *, structured: bool = False) -> bool:
        if self.marker is None:
            return not structured
        if structured:
            return False
        return body_text is not None and self.marker in body_text

    def hold_second_response(self) -> None:
        with self._lock:
            self._hold_second_response = True

    def release_held_response(self) -> None:
        self._gate.set()

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def next_stream(
        self, path: str, body_text: str | None = None, *, structured: bool = False
    ) -> str | None:
        with self._lock:
            if not self.request_matches(body_text, structured=structured):
                # A TUI-internal structured request (thread title generation
                # and friends): serve filler without consuming the script.
                return self.filler
            if not self.streams:
                return None
            stream = self.streams.pop(0)
            served = self._served_count
            self._served_count += 1
            hold = self._hold_second_response and served == 1
        if hold:
            # Deterministic mid-turn pause: the turn stays active while the
            # test drops the control connection and reconnects.
            self._gate.wait(timeout=60)
        return stream
