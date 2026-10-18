"""Queued tool-call replies from the local harness-compat model server."""

from __future__ import annotations

import json
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlsplit

from scripts.harness_compat.contract import MockReply, MockToolCall
from scripts.harness_compat.mock import MockServer


def _request(url: str, body: dict[str, object] | None = None) -> tuple[int, str]:
    parsed = urlsplit(url)
    connection = HTTPConnection(parsed.hostname, parsed.port)
    payload = json.dumps(body).encode() if body is not None else None
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
        for protocol in ("anthropic", "openai"):
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

        chat_url = f"{server.base_urls['openai']}/chat/completions"
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
