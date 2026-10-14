"""Local mock model server: Anthropic Messages and OpenAI chat, with request accounting."""

from __future__ import annotations

import json
import threading
from collections import deque
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import Any
from urllib.parse import urlsplit

from .contract import MOCK_REPLY, MockProtocol, MockReply, MockToolCall, RecordedRequest, Route

#: Routes the mock answers; anything else gets UNHANDLED_STATUS and is recorded as unhandled.
HANDLED_ROUTES: frozenset[Route] = frozenset(
    {
        ("GET", "/v1/models"),
        ("POST", "/v1/messages"),
        ("POST", "/v1/messages/count_tokens"),
        ("POST", "/v1/chat/completions"),
    }
)
UNHANDLED_STATUS = 599
_MODEL = "mock-model"


def _sse(events: list[dict[str, Any]]) -> str:
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)


def _anthropic_content(reply: MockReply) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": reply.text}]
    content.extend(
        {
            "type": "tool_use",
            "id": f"toolu_mock_{index}",
            "name": call.name,
            "input": dict(call.arguments),
        }
        for index, call in enumerate(reply.tool_calls)
    )
    return content


def _anthropic_message(reply: MockReply) -> dict[str, Any]:
    return {
        "id": "msg_mock",
        "type": "message",
        "role": "assistant",
        "model": _MODEL,
        "content": _anthropic_content(reply),
        "stop_reason": "tool_use" if reply.tool_calls else "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _anthropic_tool_events(index: int, call: MockToolCall) -> list[dict[str, Any]]:
    return [
        {
            "type": "content_block_start",
            "index": index,
            "content_block": {
                "type": "tool_use",
                "id": f"toolu_mock_{index - 1}",
                "name": call.name,
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {
                "type": "input_json_delta",
                "partial_json": json.dumps(dict(call.arguments)),
            },
        },
        {"type": "content_block_stop", "index": index},
    ]


def _anthropic_stream(reply: MockReply) -> str:
    start = {**_anthropic_message(MockReply("")), "content": [], "stop_reason": None}
    events: list[dict[str, Any]] = [
        {"type": "message_start", "message": start},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": reply.text},
        },
        {"type": "content_block_stop", "index": 0},
    ]
    for index, call in enumerate(reply.tool_calls, start=1):
        events.extend(_anthropic_tool_events(index, call))
    stop_reason = "tool_use" if reply.tool_calls else "end_turn"
    events.extend(
        [
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": 2},
            },
            {"type": "message_stop"},
        ]
    )
    return _sse(events)


def _openai_tool_call(index: int, call: MockToolCall) -> dict[str, Any]:
    return {
        "id": f"call_mock_{index}",
        "type": "function",
        "function": {
            "name": call.name,
            "arguments": json.dumps(dict(call.arguments)),
        },
    }


def _openai_tool_calls(reply: MockReply) -> list[dict[str, Any]]:
    return [_openai_tool_call(index, call) for index, call in enumerate(reply.tool_calls)]


def _openai_completion(reply: MockReply) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": reply.text}
    if reply.tool_calls:
        message["tool_calls"] = _openai_tool_calls(reply)
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": 1,
        "model": _MODEL,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if reply.tool_calls else "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _openai_stream(reply: MockReply) -> str:
    base = {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "created": 1, "model": _MODEL}
    delta = {
        "index": 0,
        "delta": {"role": "assistant", "content": reply.text},
        "finish_reason": None,
    }
    chunks = [{**base, "choices": [delta]}]
    for index, call in enumerate(reply.tool_calls):
        tool_delta = {
            "index": 0,
            "delta": {
                "tool_calls": [
                    {
                        "index": index,
                        **_openai_tool_call(index, call),
                    }
                ]
            },
            "finish_reason": None,
        }
        chunks.append({**base, "choices": [tool_delta]})
    finish_reason = "tool_calls" if reply.tool_calls else "stop"
    stop = {"index": 0, "delta": {}, "finish_reason": finish_reason}
    usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
    chunks.append({**base, "choices": [stop], "usage": usage})
    return "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"


class MockServer:
    """Serves scripted or default replies on 127.0.0.1 and records every request it sees."""

    def __init__(self, requests_path: Path) -> None:
        self._requests_path = requests_path
        self._replies: dict[MockProtocol, deque[MockReply]] = {
            "anthropic": deque(),
            "openai": deque(),
        }
        self._recorded: list[RecordedRequest] = []
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._httpd = self._thread = None

    def __enter__(self) -> MockServer:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.stop()

    @property
    def base_urls(self) -> dict[str, str]:
        assert self._httpd is not None, "start() the mock before asking for its URLs"
        root = f"http://127.0.0.1:{self._httpd.server_address[1]}"
        return {"anthropic": root, "openai": f"{root}/v1"}

    def queue(self, protocol: MockProtocol, reply: MockReply) -> None:
        with self._lock:
            self._replies[protocol].append(reply)

    @property
    def requests(self) -> tuple[RecordedRequest, ...]:
        with self._lock:
            return tuple(self._recorded)

    @property
    def unhandled(self) -> tuple[RecordedRequest, ...]:
        return tuple(request for request in self.requests if not request.handled)

    def _next_reply(self, protocol: MockProtocol) -> MockReply:
        with self._lock:
            queue = self._replies[protocol]
            return queue.popleft() if queue else MockReply(MOCK_REPLY)

    def _record(self, request: RecordedRequest) -> None:
        with self._lock:
            self._recorded.append(request)
            with self._requests_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(request)) + "\n")

    def _respond(self, route: Route, body: dict[str, Any]) -> tuple[int, str, str]:
        """(status, content type, payload) for one handled route."""
        stream = bool(body.get("stream"))
        _, path = route
        if path == "/v1/models":
            data = [{"id": _MODEL, "type": "model", "display_name": _MODEL}]
            return 200, "application/json", json.dumps({"data": data, "has_more": False})
        if path == "/v1/messages/count_tokens":
            return 200, "application/json", json.dumps({"input_tokens": 1})
        if path == "/v1/messages":
            reply = self._next_reply("anthropic")
            if stream:
                return 200, "text/event-stream", _anthropic_stream(reply)
            return 200, "application/json", json.dumps(_anthropic_message(reply))
        reply = self._next_reply("openai")
        if stream:
            return 200, "text/event-stream", _openai_stream(reply)
        return 200, "application/json", json.dumps(_openai_completion(reply))


def _make_handler(server: MockServer) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("content-length") or 0)
            raw = self.rfile.read(length) if length else b""
            try:
                parsed = json.loads(raw) if raw else {}
            except ValueError:
                return {}
            return parsed if isinstance(parsed, dict) else {}

        def _handle(self, method: str) -> None:
            body = self._body()
            route = (method, urlsplit(self.path).path)
            handled = route in HANDLED_ROUTES
            server._record(
                RecordedRequest(
                    method=method,
                    path=route[1],
                    handled=handled,
                    body_keys=tuple(sorted(body)[:12]),
                    authorized=bool(
                        self.headers.get("authorization") or self.headers.get("x-api-key")
                    ),
                )
            )
            if handled:
                status, content_type, payload = server._respond(route, body)
            else:
                status, content_type = UNHANDLED_STATUS, "application/json"
                payload = json.dumps({"theater_mock": "unhandled"})
            data = payload.encode("utf-8")
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(data)

        def __getattr__(self, name: str) -> Any:
            # Any HTTP method is accounted for, even one the stdlib server has no handler for.
            if name.startswith("do_"):
                return lambda: self._handle(name[3:])
            raise AttributeError(name)

    return Handler
