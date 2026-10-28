"""Queued tool-call replies from the local harness-compat model server."""

from __future__ import annotations

import json
import threading
import time
from http.client import HTTPConnection, HTTPResponse
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from scripts.harness_compat.contract import MOCK_REPLY, MockReply, MockToolCall
from scripts.harness_compat.mock import MockServer


def test_plugin_acknowledgements_never_consume_a_model_reply(tmp_path: Path) -> None:
    with MockServer(
        tmp_path / "requests.jsonl", acknowledged_routes=(("POST", "/telemetry"),)
    ) as server:
        server.queue("openai-chat", MockReply("scripted"))
        origin = server.base_urls["anthropic"]
        assert _request(f"{origin}/telemetry", {}) == (204, "")
        status, payload = _request(f"{origin}/v1/chat/completions", {})
        assert status == 200
        assert json.loads(payload)["choices"][0]["message"]["content"] == "scripted"
        assert not server.unhandled
    with pytest.raises(ValueError, match="must not replace model protocol routes"):
        MockServer(tmp_path / "requests.jsonl", acknowledged_routes=(("POST", "/v1/responses"),))


def _foreground(body: dict[str, object]) -> dict[str, object]:
    """A request offering at least one tool is foreground work; the mock scripts only those."""
    return body if "tools" in body else {**body, "tools": [{"name": "Bash"}]}


def _request(url: str, body: dict[str, object] | None = None) -> tuple[int, str]:
    parsed = urlsplit(url)
    connection = HTTPConnection(parsed.hostname, parsed.port)
    payload = json.dumps(_foreground(body)).encode() if body is not None else None
    headers = {"content-type": "application/json", "authorization": "Bearer mock"}
    connection.request("POST" if body is not None else "GET", parsed.path, payload, headers)
    response = connection.getresponse()
    result = response.status, response.read().decode()
    connection.close()
    return result


def _sse_data(payload: str) -> list[dict[str, object]]:
    return [
        json.loads(line.removeprefix("data: "))
        for line in payload.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


def test_a_call_can_carry_its_own_id_instead_of_the_positional_one(tmp_path: Path) -> None:
    reply = MockReply("x", (MockToolCall("search", {"q": 1}, call_id="own-id"),))

    with MockServer(tmp_path / "requests.jsonl") as server:
        server.queue("anthropic", reply)
        server.queue("anthropic", reply)
        root = server.base_urls["anthropic"]
        _, payload = _request(f"{root}/v1/messages", {"stream": False})
        assert json.loads(payload)["content"][1]["id"] == "own-id"
        _, payload = _request(f"{root}/v1/messages", {"stream": True})
        assert _sse_data(payload)[4]["content_block"]["id"] == "own-id"  # type: ignore[index]


def test_queued_tool_calls_cover_protocol_shapes_and_unknown_accounting(tmp_path: Path) -> None:
    requests_path = tmp_path / "requests.jsonl"
    reply = MockReply(
        "thinking",
        (
            MockToolCall("lookup", {"path": "src/app.py"}),
            MockToolCall("finish", {"ok": True}),
        ),
    )

    with MockServer(requests_path) as server:
        for protocol in ("anthropic", "openai-chat"):
            server.queue(protocol, reply)
            server.queue(protocol, reply)

        root = server.base_urls["anthropic"]
        status, payload = _request(f"{root}/v1/messages", {"stream": False})
        anthropic = json.loads(payload)
        assert status == 200
        assert anthropic["stop_reason"] == "tool_use"
        assert anthropic["content"][1] == {
            "type": "tool_use",
            "id": "toolu_mock_0",
            "name": "lookup",
            "input": {"path": "src/app.py"},
        }
        assert anthropic["content"][2]["id"] == "toolu_mock_1"

        status, payload = _request(f"{root}/v1/messages", {"stream": True})
        anthropic_events = _sse_data(payload)
        assert status == 200
        assert anthropic_events[4]["content_block"]["type"] == "tool_use"  # type: ignore[index]
        assert anthropic_events[7]["content_block"]["id"] == "toolu_mock_1"  # type: ignore[index]
        assert anthropic_events[-2]["delta"]["stop_reason"] == "tool_use"  # type: ignore[index]

        chat_url = f"{server.base_urls['openai-chat']}/chat/completions"
        status, payload = _request(chat_url, {"stream": False})
        openai = json.loads(payload)
        assert status == 200
        assert openai["choices"][0]["finish_reason"] == "tool_calls"
        assert openai["choices"][0]["message"]["tool_calls"][0]["function"] == {
            "name": "lookup",
            "arguments": '{"path": "src/app.py"}',
        }
        assert openai["choices"][0]["message"]["tool_calls"][1]["id"] == "call_mock_1"

        status, payload = _request(chat_url, {"stream": True})
        openai_chunks = _sse_data(payload)
        assert status == 200
        assert openai_chunks[1]["choices"][0]["delta"]["tool_calls"][0]["index"] == 0  # type: ignore[index]
        assert openai_chunks[2]["choices"][0]["delta"]["tool_calls"][0]["id"] == "call_mock_1"  # type: ignore[index]
        assert openai_chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"  # type: ignore[index]

        status, payload = _request(f"{root}/unknown")
        assert (status, json.loads(payload)) == (599, {"theater_mock": "unhandled"})
        assert len(server.requests) == 5
        assert [(request.path, request.handled) for request in server.unhandled] == [
            ("/unknown", False)
        ]

    lines = requests_path.read_text().splitlines()
    assert len(lines) == 5
    assert json.loads(lines[-1])["handled"] is False


def test_every_http_method_is_accounted_for_and_unhandled_ones_get_599(tmp_path: Path) -> None:
    with MockServer(tmp_path / "requests.jsonl") as server:
        parsed = urlsplit(server.base_urls["anthropic"])
        methods = ("PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CUSTOM")
        for method in methods:
            connection = HTTPConnection(parsed.hostname, parsed.port)
            connection.request(method, "/v1/new-route", b"{}", {"content-type": "application/json"})
            response = connection.getresponse()
            response.read()
            connection.close()
            assert response.status == 599, method
        seen = {(request.method, request.path) for request in server.unhandled}
        assert seen == {(method, "/v1/new-route") for method in methods}


def _open(url: str, body: dict[str, object]) -> tuple[HTTPConnection, HTTPResponse]:
    parsed = urlsplit(url)
    connection = HTTPConnection(parsed.hostname, parsed.port, timeout=5)
    headers = {"content-type": "application/json", "authorization": "Bearer mock"}
    connection.request("POST", parsed.path, json.dumps(_foreground(body)).encode(), headers)
    return connection, connection.getresponse()


def _read_events(response: HTTPResponse, *, count: int | None = None, until: str | None = None):
    """SSE `data:` events as they arrive; keepalive comments are skipped, not returned."""
    events: list[dict[str, object]] = []
    while len(events) != count:
        line = response.readline().decode()
        if not line:
            break
        if line.startswith("data: ") and line.strip() != "data: [DONE]":
            events.append(json.loads(line.removeprefix("data: ")))
            if until is not None and events[-1].get("type") == until:
                break
    return events


def _read_until_keepalive(response: HTTPResponse) -> bool:
    return any(response.readline().decode().startswith(": keepalive") for _ in range(8))


def _wait(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return bool(predicate())


def test_responses_codec_streams_text_and_tool_calls(
    tmp_path: Path,
) -> None:
    reply = MockReply("hello", (MockToolCall("exec_command", {"cmd": "touch x"}),))
    with MockServer(tmp_path / "requests.jsonl") as server:
        server.queue("openai-responses", reply)
        server.queue("openai-responses", MockReply("", (MockToolCall("only_call", {}),)))
        url = f"{server.base_urls['openai-responses']}/responses"

        status, payload = _request(url, {"stream": True, "model": "m"})
        events = _sse_data(payload)
        assert status == 200
        assert [event["type"] for event in events] == [
            "response.created",
            "response.output_item.added",
            "response.content_part.added",
            "response.output_text.delta",
            "response.output_item.done",
            "response.output_item.done",
            "response.completed",
        ]
        assert events[3]["delta"] == "hello"
        assert events[5]["item"] == {
            "type": "function_call",
            "call_id": "call_mock_0",
            "name": "exec_command",
            "arguments": '{"cmd": "touch x"}',
        }

        _, payload = _request(url, {"stream": True})
        assert [event["type"] for event in _sse_data(payload)] == [
            "response.created",
            "response.output_item.done",
            "response.completed",
        ]

        assert not server.unhandled


def test_responses_codec_streams_a_freeform_tool_call_and_reads_its_output(tmp_path: Path) -> None:
    patch = "*** Begin Patch\n*** Add File: x\n+y\n*** End Patch\n"
    reply = MockReply("", (MockToolCall("apply_patch", patch),))
    output = {"type": "custom_tool_call_output", "call_id": "call_mock_0", "output": "patched"}
    with MockServer(tmp_path / "requests.jsonl") as server:
        server.queue("openai-responses", reply)
        url = f"{server.base_urls['openai-responses']}/responses"

        _, payload = _request(url, {"stream": True})
        _request(url, {"stream": True, "input": [output]})

    assert _sse_data(payload)[1]["item"] == {
        "type": "custom_tool_call",
        "call_id": "call_mock_0",
        "name": "apply_patch",
        "input": patch,
    }
    assert server.requests[1].last_tool_result == "patched"


def test_responses_codec_offers_namespaced_tools_and_calls_them_with_their_namespace(
    tmp_path: Path,
) -> None:
    tools = [
        {
            "type": "namespace",
            "name": "mcp__probe",
            "tools": [
                {
                    "type": "function",
                    "name": "mark",
                    "parameters": {"properties": {"label": {}}, "required": ["label"]},
                }
            ],
        },
        {"type": "function", "name": "exec_command"},
    ]
    reply = MockReply("", (MockToolCall("mcp__probe/mark", {"label": "x"}),))
    with MockServer(tmp_path / "requests.jsonl") as server:
        server.queue("openai-responses", reply)

        _, payload = _request(
            f"{server.base_urls['openai-responses']}/responses", {"stream": True, "tools": tools}
        )

    assert server.requests[0].tools == ("mcp__probe/mark", "exec_command")
    assert server.requests[0].tool_shapes["mcp__probe/mark"].required == ("label",)
    assert _sse_data(payload)[1]["item"] == {
        "type": "function_call",
        "call_id": "call_mock_0",
        "name": "mark",
        "namespace": "mcp__probe",
        "arguments": '{"label": "x"}',
    }


def test_a_held_responses_stream_has_sent_no_output_when_it_is_held(tmp_path: Path) -> None:
    with MockServer(tmp_path / "requests.jsonl") as server:
        hold = server.hold()
        server.queue("openai-responses", MockReply("partial", hold=hold))
        connection, response = _open(
            f"{server.base_urls['openai-responses']}/responses", {"stream": True}
        )

        assert [event["type"] for event in _read_events(response, count=1)] == ["response.created"]
        assert hold.wait_reached(5)
        assert _read_until_keepalive(response), "the held stream stays open on keepalive comments"

        hold.release()
        tail = _read_events(response, until="response.completed")
        assert [event["type"] for event in tail][-2:] == [
            "response.output_item.done",
            "response.completed",
        ]
        connection.close()


def test_requests_are_described_by_protocol_model_effort_tools_and_last_user_text(
    tmp_path: Path,
) -> None:
    tools = [{"name": "Bash"}, {"function": {"name": "write_file"}}, "ignored"]
    tool_result = {"role": "user", "content": [{"type": "tool_result", "content": "done"}]}
    with MockServer(tmp_path / "requests.jsonl") as server:
        _request(
            f"{server.base_urls['anthropic']}/v1/messages",
            {
                "model": "sonnet",
                "output_config": {"effort": "high"},
                "tools": tools,
                "messages": [{"role": "user", "content": "first"}, tool_result],
            },
        )
        _request(
            f"{server.base_urls['openai-chat']}/chat/completions",
            {
                "model": "gpt",
                "reasoning_effort": "low",
                "stream": True,
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "hi "}]},
                    {"role": "assistant", "content": "ok"},
                ],
            },
        )
        _request(
            f"{server.base_urls['openai-responses']}/responses",
            {
                "model": "gpt-r",
                "reasoning": {"effort": "max"},
                "stream": True,
                "input": [{"type": "message", "role": "user", "content": "latest"}],
            },
        )
        _request(f"{server.base_urls['anthropic']}/v1/models")
        anthropic, chat, responses, models = server.requests

    assert (anthropic.protocol, anthropic.model, anthropic.effort) == (
        "anthropic",
        "sonnet",
        "high",
    )
    assert anthropic.tools == ("Bash", "write_file")
    assert (anthropic.messages, anthropic.last_user, anthropic.stream) == (2, "first", False)
    assert (chat.protocol, chat.model, chat.effort) == ("openai-chat", "gpt", "low")
    assert (chat.messages, chat.last_user, chat.stream) == (2, "hi ", True)
    assert (responses.protocol, responses.model, responses.effort) == (
        "openai-responses",
        "gpt-r",
        "max",
    )
    assert (responses.messages, responses.last_user) == (1, "latest")
    assert (models.protocol, models.model, models.tools, models.last_user) == ("", None, (), None)
    recorded = [json.loads(line) for line in (tmp_path / "requests.jsonl").read_text().splitlines()]
    assert recorded[0]["effort"] == "high" and recorded[0]["tools"] == ["Bash", "write_file"]


def test_requests_carry_the_conversation_humans_and_the_last_tool_result(tmp_path: Path) -> None:
    chat_history = [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "ack"},
        {"role": "tool", "content": [{"type": "text", "text": "chat result"}]},
        {"role": "user", "content": "two"},
        {"role": "assistant", "content": "ack"},
        {"role": "tool", "content": "newest chat result"},
    ]
    anthropic_history = [
        {"role": "user", "content": "one"},
        {"role": "user", "content": [{"type": "tool_result", "content": "anthropic result"}]},
    ]
    responses_history = [
        {"type": "message", "role": "user", "content": "one"},
        {"type": "function_call_output", "call_id": "c", "output": "responses result"},
    ]
    with MockServer(tmp_path / "requests.jsonl") as server:
        _request(f"{server.base_urls['openai-chat']}/chat/completions", {"messages": chat_history})
        _request(f"{server.base_urls['anthropic']}/v1/messages", {"messages": anthropic_history})
        _request(
            f"{server.base_urls['openai-responses']}/responses",
            {"stream": True, "input": responses_history},
        )
        _request(f"{server.base_urls['openai-chat']}/chat/completions", {"messages": []})
        chat, anthropic, responses, empty = server.requests

    assert (chat.user_texts, chat.last_user) == (("one", "two"), "two")
    assert chat.last_tool_result == "newest chat result"
    assert anthropic.last_tool_result == "anthropic result"
    assert responses.last_tool_result == "responses result"
    assert (empty.user_texts, empty.last_user, empty.last_tool_result) == ((), None, None)


def test_each_offered_tool_records_the_arguments_its_schema_declares(tmp_path: Path) -> None:
    schema = {"properties": {"path": {}, "content": {}}, "required": ["path"]}
    tools = [
        {"name": "Write", "input_schema": {**schema, "additionalProperties": False}},
        {"type": "function", "function": {"name": "write_file", "parameters": schema}},
        {"type": "function", "name": "patch", "parameters": {}},
        {"type": "custom", "name": "apply_patch"},
    ]
    with MockServer(tmp_path / "requests.jsonl") as server:
        _request(f"{server.base_urls['anthropic']}/v1/messages", {"tools": tools})
        (request,) = server.requests

    shapes = request.tool_shapes
    assert (shapes["Write"].properties, shapes["Write"].required) == (
        ("content", "path"),
        ("path",),
    )
    assert shapes["Write"].closed and not shapes["write_file"].closed
    assert shapes["write_file"].properties == ("content", "path")
    assert shapes["patch"].properties == () and "apply_patch" not in shapes
    recorded = json.loads((tmp_path / "requests.jsonl").read_text())
    assert recorded["tool_shapes"]["Write"]["closed"] is True


def test_the_prompt_is_found_beside_context_the_cli_adds_to_the_same_message(
    tmp_path: Path,
) -> None:
    reminder = "<system-reminder>" + "x" * 600 + "</system-reminder>"
    message = {
        "role": "user",
        "content": [
            {"type": "text", "text": reminder},
            {"type": "text", "text": "Reply with NONCE-1"},
        ],
    }
    with MockServer(tmp_path / "requests.jsonl") as server:
        _request(f"{server.base_urls['anthropic']}/v1/messages", {"messages": [message]})
        (request,) = server.requests

    assert request.last_user == "Reply with NONCE-1"
    assert len(request.last_user_parts) == 2 and len(request.last_user_parts[0]) == 300
    assert request.carries("NONCE-1") and not request.carries("NONCE-2")
    assert request.user_texts[-1] == "Reply with NONCE-1"


def test_a_request_offering_no_tools_is_background_work_and_never_consumes_the_script(
    tmp_path: Path,
) -> None:
    with MockServer(tmp_path / "requests.jsonl") as server:
        hold = server.hold()
        server.queue("anthropic", MockReply("scripted", hold=hold))
        url = f"{server.base_urls['anthropic']}/v1/messages"

        _, title = _request(url, {"tools": [], "messages": []})
        connection, response = _open(url, {"stream": True})
        try:
            assert hold.wait_reached(5), "the scripted reply was spent on a background request"
        finally:
            hold.release()
            response.close()
            connection.close()

    assert MOCK_REPLY in title and "scripted" not in title


def test_every_anthropic_message_gets_its_own_id_so_answers_never_merge(tmp_path: Path) -> None:
    with MockServer(tmp_path / "requests.jsonl") as server:
        url = f"{server.base_urls['anthropic']}/v1/messages"
        ids = {json.loads(_request(url, {"stream": False})[1])["id"] for _ in range(3)}
        streamed = [
            event["message"]["id"]
            for _ in range(2)
            for event in _sse_data(_request(url, {"stream": True})[1])
            if event.get("type") == "message_start"
        ]

    assert len(ids) == 3 and len(set(streamed)) == 2
    assert not ids & set(streamed)


def test_a_mock_serves_only_between_start_and_stop(tmp_path: Path) -> None:
    server = MockServer(tmp_path / "requests.jsonl")
    assert not server.serving

    with server:
        assert server.serving

    assert not server.serving


def test_a_held_stream_withholds_its_tail_until_released(tmp_path: Path) -> None:
    with MockServer(tmp_path / "requests.jsonl") as server:
        hold = server.hold()
        server.queue("anthropic", MockReply("partial", hold=hold))
        connection, response = _open(
            f"{server.base_urls['anthropic']}/v1/messages", {"stream": True}
        )

        head = _read_events(response, count=3)
        assert [event["type"] for event in head] == [
            "message_start",
            "content_block_start",
            "content_block_delta",
        ]
        assert hold.wait_reached(5)
        assert _read_until_keepalive(response), "the held stream stays open on keepalive comments"

        hold.release()
        tail = _read_events(response, until="message_stop")
        assert [event["type"] for event in tail][-2:] == ["message_delta", "message_stop"]
        assert not hold.cancelled
        connection.close()


def test_a_held_stream_reports_a_client_that_drops_the_connection(tmp_path: Path) -> None:
    with MockServer(tmp_path / "requests.jsonl") as server:
        hold = server.hold()
        server.queue("openai-chat", MockReply("partial", hold=hold))
        connection, response = _open(
            f"{server.base_urls['openai-chat']}/chat/completions", {"stream": True}
        )
        assert len(_read_events(response, count=1)) == 1
        assert hold.wait_reached(5)

        response.close()  # the response keeps the socket open until it is closed too
        connection.close()

        assert _wait(lambda: hold.cancelled)


def test_a_held_json_reply_is_withheld_until_released(tmp_path: Path) -> None:
    with MockServer(tmp_path / "requests.jsonl") as server:
        hold = server.hold()
        server.queue("anthropic", MockReply("late", hold=hold))
        answers: list[str] = []
        worker = threading.Thread(
            target=lambda: answers.append(
                _request(f"{server.base_urls['anthropic']}/v1/messages", {})[1]
            )
        )
        worker.start()

        assert hold.wait_reached(5)
        worker.join(timeout=0.6)
        assert worker.is_alive() and not answers, "the body stays withheld while held"

        hold.release()
        worker.join(timeout=5)
        assert json.loads(answers[0])["content"][0]["text"] == "late"


def test_the_model_catalog_is_configurable_and_requested_models_are_echoed(tmp_path: Path) -> None:
    with MockServer(tmp_path / "requests.jsonl", models=("mock-a", "mock-b")) as server:
        _, payload = _request(f"{server.base_urls['anthropic']}/v1/models")
        assert [item["id"] for item in json.loads(payload)["data"]] == ["mock-a", "mock-b"]

        _, payload = _request(f"{server.base_urls['anthropic']}/v1/messages", {"model": "mock-b"})
        assert json.loads(payload)["model"] == "mock-b"
