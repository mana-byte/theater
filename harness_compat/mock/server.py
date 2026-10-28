"""Local mock model server: Anthropic Messages, OpenAI Chat and Responses, with accounting."""

from __future__ import annotations

import json
import select
import socket
import threading
import uuid
from collections import deque
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import TracebackType
from typing import Any
from urllib.parse import urlsplit

from ..contract import (
    MOCK_REPLY,
    Hold,
    MockProtocol,
    MockReply,
    MockToolCall,
    RecordedRequest,
    Route,
    ToolShape,
)

#: Routes the mock answers; anything else gets UNHANDLED_STATUS and is recorded as unhandled.
HANDLED_ROUTES: frozenset[Route] = frozenset(
    {
        ("GET", "/v1/models"),
        ("POST", "/v1/messages"),
        ("POST", "/v1/messages/count_tokens"),
        ("POST", "/v1/chat/completions"),
        ("POST", "/v1/responses"),
    }
)
UNHANDLED_STATUS = 599
_MODEL = "mock-model"
#: SSE comment cadence while a response is held; a failed write reveals a dropped client.
_KEEPALIVE_SECONDS = 0.25
#: A hold nobody releases ends by itself, so a stuck scenario cannot pin a handler thread.
_MAX_HOLD_SECONDS = 120.0
_PROTOCOLS: dict[str, MockProtocol] = {
    "/v1/messages": "anthropic",
    "/v1/messages/count_tokens": "anthropic",
    "/v1/chat/completions": "openai-chat",
    "/v1/responses": "openai-responses",
}
_USER_TEXT_CHARS = 300
_MAX_USER_TEXTS = 64  # was 20; resume test needs 34+ prompts recorded
_MAX_USER_PARTS = 12
_MAX_TOOL_NAMES = 200
_MAX_TOOL_PROPERTIES = 24


def _sse(events: list[dict[str, Any]]) -> str:
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events)


def _object_arguments(call: MockToolCall) -> dict[str, Any]:
    """A function call's arguments; a freeform input only exists on the Responses wire."""
    if isinstance(call.arguments, str):
        raise TypeError(
            f"{call.name} is a freeform call, which only the Responses protocol carries"
        )
    return dict(call.arguments)


def _anthropic_content(reply: MockReply) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = [{"type": "text", "text": reply.text}]
    content.extend(
        {
            "type": "tool_use",
            "id": call.call_id or f"toolu_mock_{index}",
            "name": call.name,
            "input": _object_arguments(call),
        }
        for index, call in enumerate(reply.tool_calls)
    )
    return content


def _anthropic_message(reply: MockReply, model: str) -> dict[str, Any]:
    return {
        "id": "msg_" + uuid.uuid4().hex,
        "type": "message",
        "role": "assistant",
        "model": model,
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
                "id": call.call_id or f"toolu_mock_{index - 1}",
                "name": call.name,
                "input": {},
            },
        },
        {
            "type": "content_block_delta",
            "index": index,
            "delta": {
                "type": "input_json_delta",
                "partial_json": json.dumps(_object_arguments(call)),
            },
        },
        {"type": "content_block_stop", "index": index},
    ]


def _anthropic_stream(reply: MockReply, model: str) -> tuple[str, str]:
    """(head up to the first text delta, tail): a hold splits the stream between them."""
    start = {**_anthropic_message(MockReply(""), model), "content": [], "stop_reason": None}
    head: list[dict[str, Any]] = [
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
    ]
    tail: list[dict[str, Any]] = [{"type": "content_block_stop", "index": 0}]
    for index, call in enumerate(reply.tool_calls, start=1):
        tail.extend(_anthropic_tool_events(index, call))
    stop_reason = "tool_use" if reply.tool_calls else "end_turn"
    tail.extend(
        [
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": 2},
            },
            {"type": "message_stop"},
        ]
    )
    return _sse(head), _sse(tail)


def _openai_tool_call(index: int, call: MockToolCall) -> dict[str, Any]:
    return {
        "id": call.call_id or f"call_mock_{index}",
        "type": "function",
        "function": {
            "name": call.name,
            "arguments": json.dumps(_object_arguments(call)),
        },
    }


def _openai_tool_calls(reply: MockReply) -> list[dict[str, Any]]:
    return [_openai_tool_call(index, call) for index, call in enumerate(reply.tool_calls)]


def _openai_completion(reply: MockReply, model: str) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": reply.text}
    if reply.tool_calls:
        message["tool_calls"] = _openai_tool_calls(reply)
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": 1,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if reply.tool_calls else "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _openai_stream(reply: MockReply, model: str) -> tuple[str, str]:
    base = {"id": "chatcmpl-mock", "object": "chat.completion.chunk", "created": 1, "model": model}
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
    frames = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    return frames[0], "".join(frames[1:]) + "data: [DONE]\n\n"


def _responses_stream(reply: MockReply) -> tuple[str, str]:
    """The Responses stream, held before output text so the UI keeps its working indicator.

    A hold splits right after ``response.created``.
    """
    response_id, item_id = "resp-mock", "msg-mock"
    head: list[dict[str, Any]] = [{"type": "response.created", "response": {"id": response_id}}]
    tail: list[dict[str, Any]] = []
    if reply.text:
        tail.extend(
            [
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {"type": "message", "role": "assistant", "id": item_id, "content": []},
                },
                {
                    "type": "response.content_part.added",
                    "item_id": item_id,
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": ""},
                },
                {
                    "type": "response.output_text.delta",
                    "item_id": item_id,
                    "output_index": 0,
                    "content_index": 0,
                    "delta": reply.text,
                },
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "message",
                        "role": "assistant",
                        "id": item_id,
                        "content": [{"type": "output_text", "text": reply.text}],
                    },
                },
            ]
        )
    for index, call in enumerate(reply.tool_calls):
        item: dict[str, Any] = {"call_id": call.call_id or f"call_mock_{index}"}
        if isinstance(call.arguments, str):
            item |= {"type": "custom_tool_call", "name": call.name, "input": call.arguments}
        else:
            # A name offered as "<namespace>/<tool>" goes back as a namespaced function call.
            namespace, _, name = call.name.rpartition("/")
            item |= {"type": "function_call", "name": name}
            item["arguments"] = json.dumps(dict(call.arguments))
            if namespace:
                item["namespace"] = namespace
        tail.append({"type": "response.output_item.done", "item": item})
    tail.append(
        {
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
    )
    return _sse(head), _sse(tail)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _effort(body: dict[str, Any]) -> str | None:
    """The requested reasoning effort, wherever the protocol carries it."""
    for container, key in (
        (body, "reasoning_effort"),
        (body.get("reasoning"), "effort"),
        (body.get("output_config"), "effort"),
        (body, "effort"),
    ):
        if isinstance(container, dict) and isinstance(container.get(key), str):
            return container[key]
    return None


def _tool_names(body: dict[str, Any]) -> tuple[str, ...]:
    names: list[str] = []
    for tool in body.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "namespace":
            # A namespaced call must name its namespace, so each tool is offered as "<ns>/<tool>".
            inner = [t.get("name") for t in tool.get("tools") or [] if isinstance(t, dict)]
            names.extend(f"{tool.get('name')}/{name}" for name in inner if isinstance(name, str))
            continue
        function = tool.get("function")
        name = tool.get("name") or (function.get("name") if isinstance(function, dict) else None)
        if isinstance(name, str):
            names.append(name)
    return tuple(names[:_MAX_TOOL_NAMES])


def _shape(schema: object) -> ToolShape | None:
    if not isinstance(schema, dict):
        return None
    properties, required = schema.get("properties"), schema.get("required")
    return ToolShape(
        tuple(sorted(properties))[:_MAX_TOOL_PROPERTIES] if isinstance(properties, dict) else (),
        tuple(item for item in required if isinstance(item, str))[:_MAX_TOOL_PROPERTIES]
        if isinstance(required, list)
        else (),
        closed=schema.get("additionalProperties") is False,
    )


def _tool_shapes(body: dict[str, Any]) -> dict[str, ToolShape]:
    """Each offered tool's declared top-level arguments, wherever the protocol keeps its schema."""
    shapes: dict[str, ToolShape] = {}
    for tool in (body.get("tools") or [])[:_MAX_TOOL_NAMES]:
        if not isinstance(tool, dict):
            continue
        if tool.get("type") == "namespace":
            for inner in tool.get("tools") or []:
                name = inner.get("name") if isinstance(inner, dict) else None
                shape = _shape(inner.get("parameters")) if isinstance(inner, dict) else None
                if isinstance(name, str) and shape is not None:
                    shapes[f"{tool.get('name')}/{name}"] = shape
            continue
        function = tool["function"] if isinstance(tool.get("function"), dict) else {}
        name = tool.get("name") or function.get("name")
        declared = (tool.get("input_schema"), tool.get("parameters"), function.get("parameters"))
        shape = _shape(next((item for item in declared if item is not None), None))
        if isinstance(name, str) and shape is not None:
            shapes[name] = shape
    return shapes


def _conversation(body: dict[str, Any]) -> list[Any]:
    items = body["messages"] if "messages" in body else body.get("input")
    if isinstance(items, list):
        return items
    return [items] if isinstance(items, str) else []


def _human_parts(item: object) -> tuple[str, ...]:
    """The text parts of a user message; one that only carries tool results has none."""
    if not isinstance(item, dict) or item.get("role") != "user":
        return ()
    content = item.get("content")
    if isinstance(content, str):
        return (content,) if content else ()
    if not isinstance(content, list):
        return ()
    return tuple(
        part["text"]
        for part in content
        if isinstance(part, dict)
        and part.get("type") in ("text", "input_text")
        and isinstance(part.get("text"), str)
        and part["text"]
    )


def _user_texts(body: dict[str, Any]) -> tuple[str, ...]:
    """Every human text part in the conversation, oldest first, clipped and bounded."""
    parts = [part for item in _conversation(body) for part in _human_parts(item)]
    return tuple(part[:_USER_TEXT_CHARS] for part in parts[-_MAX_USER_TEXTS:])


def _last_user_parts(body: dict[str, Any]) -> tuple[str, ...]:
    """The text parts of the newest human message; a CLI may add context beside the prompt."""
    for item in reversed(_conversation(body)):
        if parts := _human_parts(item):
            return tuple(part[:_USER_TEXT_CHARS] for part in parts[-_MAX_USER_PARTS:])
    return ()


def _joined_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        part["text"]
        for part in content
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


def _last_tool_result(body: dict[str, Any]) -> str | None:
    """The newest tool result the client sent back, in whichever shape the protocol carries it."""
    for item in reversed(_conversation(body)):
        if not isinstance(item, dict):
            continue
        if item.get("role") == "tool":
            return _joined_text(item.get("content"))[:_USER_TEXT_CHARS]
        if item.get("type") in ("function_call_output", "custom_tool_call_output"):
            return _joined_text(item.get("output"))[:_USER_TEXT_CHARS]
        content = item.get("content")
        if item.get("role") == "user" and isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "tool_result":
                    return _joined_text(part.get("content"))[:_USER_TEXT_CHARS]
    return None


class _Hold:
    """The concrete hold: set by the handler thread, driven by the scenario."""

    def __init__(self) -> None:
        self._reached = threading.Event()
        self._released = threading.Event()
        self._cancelled = threading.Event()

    def wait_reached(self, timeout: float) -> bool:
        return self._reached.wait(timeout)

    def release(self) -> None:
        self._released.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def mark_reached(self) -> None:
        self._reached.set()

    def mark_cancelled(self) -> None:
        self._cancelled.set()

    def wait_released(self, timeout: float) -> bool:
        return self._released.wait(timeout)


@dataclass(frozen=True, slots=True)
class _Response:
    """One answer: ``body`` alone, or ``body`` then ``tail`` once its hold is released."""

    status: int
    content_type: str
    body: str
    tail: str = ""
    streaming: bool = False
    hold: _Hold | None = None


class MockServer:
    """Serves scripted or default replies on 127.0.0.1 and records every request it sees."""

    def __init__(
        self,
        requests_path: Path,
        *,
        models: Sequence[str] = (_MODEL,),
        acknowledged_routes: Sequence[Route] = (),
    ) -> None:
        self._requests_path = requests_path
        self._models = tuple(models)
        self._acknowledged_routes = frozenset(acknowledged_routes)
        if self._acknowledged_routes & HANDLED_ROUTES:
            raise ValueError("acknowledged routes must not replace model protocol routes")
        self._replies: dict[MockProtocol, deque[MockReply]] = {
            "anthropic": deque(),
            "openai-chat": deque(),
            "openai-responses": deque(),
        }
        self._recorded: list[RecordedRequest] = []
        self._holds: list[_Hold] = []
        self._lock = threading.Lock()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self))
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        with self._lock:
            holds = list(self._holds)
        for hold in holds:
            hold.release()
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._httpd = self._thread = None

    @property
    def serving(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

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
        return {"anthropic": root, "openai-chat": f"{root}/v1", "openai-responses": f"{root}/v1"}

    def queue(self, protocol: MockProtocol, reply: MockReply) -> None:
        with self._lock:
            self._replies[protocol].append(reply)

    def hold(self) -> Hold:
        hold = _Hold()
        with self._lock:
            self._holds.append(hold)
        return hold

    @property
    def requests(self) -> tuple[RecordedRequest, ...]:
        with self._lock:
            return tuple(self._recorded)

    @property
    def unhandled(self) -> tuple[RecordedRequest, ...]:
        return tuple(request for request in self.requests if not request.handled)

    def _next_reply(self, protocol: MockProtocol, body: dict[str, Any]) -> MockReply:
        """The scripted reply for a foreground turn; a request offering no tools is background
        work (a session title, a summary) and gets the default without consuming the script."""
        with self._lock:
            queue = self._replies[protocol]
            if queue and body.get("tools"):
                return queue.popleft()
            return MockReply(MOCK_REPLY)

    def _record(self, request: RecordedRequest) -> None:
        with self._lock:
            self._recorded.append(request)
            with self._requests_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(asdict(request)) + "\n")

    def _respond(self, route: Route, body: dict[str, Any]) -> _Response:
        """The answer for one handled route."""
        stream = bool(body.get("stream"))
        _, path = route
        model = _text(body.get("model")) or _MODEL
        if path == "/v1/models":
            data = [
                {"id": name, "object": "model", "type": "model", "display_name": name}
                for name in self._models
            ]
            payload = {"object": "list", "data": data, "has_more": False}
            return _Response(200, "application/json", json.dumps(payload))
        if path == "/v1/messages/count_tokens":
            return _Response(200, "application/json", json.dumps({"input_tokens": 1}))
        if route in self._acknowledged_routes:
            return _Response(204, "application/json", "")
        if path == "/v1/messages":
            reply = self._next_reply("anthropic", body)
            if stream:
                return _streamed(reply, _anthropic_stream(reply, model))
            return _complete(reply, json.dumps(_anthropic_message(reply, model)))
        if path == "/v1/responses":
            reply = self._next_reply("openai-responses", body)
            return _streamed(reply, _responses_stream(reply))
        reply = self._next_reply("openai-chat", body)
        if stream:
            return _streamed(reply, _openai_stream(reply, model))
        return _complete(reply, json.dumps(_openai_completion(reply, model)))


def _held(reply: MockReply) -> _Hold | None:
    if reply.hold is None:
        return None
    if not isinstance(reply.hold, _Hold):
        raise TypeError("a reply's hold must come from MockServer.hold()")
    return reply.hold


def _streamed(reply: MockReply, parts: tuple[str, str]) -> _Response:
    head, tail = parts
    hold = _held(reply)
    if hold is None:
        return _Response(200, "text/event-stream", head + tail)
    return _Response(200, "text/event-stream", head, tail, streaming=True, hold=hold)


def _complete(reply: MockReply, payload: str) -> _Response:
    return _Response(200, "application/json", payload, hold=_held(reply))


def _peer_closed(connection: socket.socket) -> bool:
    readable, _, _ = select.select([connection], [], [], 0)
    if not readable:
        return False
    try:
        return connection.recv(1, socket.MSG_PEEK) == b""
    except OSError:
        return True


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
            handled = route in HANDLED_ROUTES or route in server._acknowledged_routes
            server._record(
                RecordedRequest(
                    method=method,
                    path=route[1],
                    handled=handled,
                    body_keys=tuple(sorted(body)[:12]),
                    authorized=bool(
                        self.headers.get("authorization") or self.headers.get("x-api-key")
                    ),
                    protocol=_PROTOCOLS.get(route[1], ""),
                    model=_text(body.get("model")),
                    effort=_effort(body),
                    tools=_tool_names(body),
                    tool_shapes=_tool_shapes(body),
                    messages=len(_conversation(body)),
                    user_texts=_user_texts(body),
                    last_user_parts=(parts := _last_user_parts(body)),
                    last_user=parts[-1] if parts else None,
                    last_tool_result=_last_tool_result(body),
                    stream=bool(body.get("stream")),
                )
            )
            if handled:
                response = server._respond(route, body)
            else:
                payload = json.dumps({"theater_mock": "unhandled"})
                response = _Response(UNHANDLED_STATUS, "application/json", payload)
            self._send(response, method)

        def _send(self, response: _Response, method: str) -> None:
            hold = response.hold
            if hold is None:
                self._write_whole(response, method)
            elif response.streaming:
                self._write_held_stream(response, hold)
            else:
                hold.mark_reached()
                if self._wait_idle(hold):
                    self._write_whole(response, method)

        def _write_whole(self, response: _Response, method: str) -> None:
            data = response.body.encode("utf-8")
            self.send_response(response.status)
            self.send_header("content-type", response.content_type)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            if method != "HEAD":
                self.wfile.write(data)

        def _write_held_stream(self, response: _Response, hold: _Hold) -> None:
            self.close_connection = True
            try:
                self.send_response(response.status)
                self.send_header("content-type", response.content_type)
                self.send_header("connection", "close")
                self.end_headers()
                self.wfile.write(response.body.encode("utf-8"))
                self.wfile.flush()
                hold.mark_reached()
                if self._wait_released(hold):
                    self.wfile.write(response.tail.encode("utf-8"))
                    self.wfile.flush()
            except OSError:
                hold.mark_cancelled()

        def _wait_released(self, hold: _Hold) -> bool:
            """Block on keepalive comments until released; a failed write means the client left."""
            waited = 0.0
            while waited < _MAX_HOLD_SECONDS:
                if hold.wait_released(_KEEPALIVE_SECONDS):
                    return True
                self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                waited += _KEEPALIVE_SECONDS
            return True

        def _wait_idle(self, hold: _Hold) -> bool:
            """Withhold a JSON body until released; False once the client dropped the connection."""
            waited = 0.0
            while waited < _MAX_HOLD_SECONDS:
                if hold.wait_released(_KEEPALIVE_SECONDS):
                    return True
                if _peer_closed(self.connection):
                    hold.mark_cancelled()
                    self.close_connection = True
                    return False
                waited += _KEEPALIVE_SECONDS
            return True

        def __getattr__(self, name: str) -> Any:
            # Any HTTP method is accounted for, even one the stdlib server has no handler for.
            if name.startswith("do_"):
                return lambda: self._handle(name[3:])
            raise AttributeError(name)

    return Handler
