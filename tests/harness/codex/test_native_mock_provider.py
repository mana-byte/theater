"""Offline tests for the mock provider's dispatch, hold, and steer scripting.

No codex binary and no tmux: these prove the scripting rules the
qualification relies on — last-user-message dispatch (the steer seam),
structured-request filler, FIFO exhaustion, the held stream's
disconnect tolerance, and the approval exec-call derivation.

Everything else here (the suites that need the real binary) is opt-in in
tests/test_codex_native_runtime_proof.py.
"""

from __future__ import annotations

import importlib.util
import json
import socket
import sys
import time
from pathlib import Path
from types import ModuleType
from urllib.parse import urlparse

import pytest

from tests.native.codex_mock_provider import (
    DEFAULT_HOLD_SECONDS,
    MockResponsesProvider,
    Rule,
    escalated_exec_call_stream,
    exec_call_then_ok_factory,
    fifo_script,
    final_message_stream,
    held_essay_stream,
    is_structured_request,
    last_user_message_text,
    standard_rules,
)
from tests.support.paths import fixture

STEER_TEXT = "Stop writing the essay immediately. Reply with exactly: steered"
RIVERS = "Write a long, detailed 1200 word essay about rivers."


def _bootstrap_mock_module() -> ModuleType:
    path = fixture("codex_native_ui_bootstrap", "mock_responses.py")
    spec = importlib.util.spec_from_file_location("codex_bootstrap_mock_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _events_of(script) -> list[dict]:
    return list(script.events)


def test_last_user_message_ignores_conversation_history():
    body = {
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": RIVERS}],
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": STEER_TEXT}],
            },
        ]
    }
    assert last_user_message_text(body) == STEER_TEXT
    assert last_user_message_text({"input": []}) == ""
    assert last_user_message_text(None) == ""


def test_structured_requests_are_detected_not_scripted():
    body = {"text": {"format": {"type": "json_schema", "strict": True}}}
    assert is_structured_request(body)
    assert not is_structured_request({"model": "mock-model", "input": []})


def test_steer_request_dispatches_on_the_last_user_message():
    """The wire fact the steer path depends on: after turn/steer, codex issues
    a request whose last user message is the steer text while the original
    essay prompt is still in the history — the reply must complete the turn,
    not hold it again."""
    provider = MockResponsesProvider(standard_rules())
    script, marker = provider.select_script(STEER_TEXT)
    assert marker == "Reply with exactly"
    assert script.hold_seconds == 0
    done = next(e for e in _events_of(script) if e["type"] == "response.output_item.done")
    assert done["item"]["content"][0]["text"] == "steered"


def test_essay_prompts_get_a_held_stream_then_the_default():
    provider = MockResponsesProvider(standard_rules())
    first, marker = provider.select_script(RIVERS)
    assert marker == "essay about rivers"
    assert first.hold_seconds == DEFAULT_HOLD_SECONDS
    assert first.hold_split_index > 0, "the hold must sit mid-stream, after deltas"
    second, _ = provider.select_script(RIVERS)
    assert second.hold_seconds == 0, "an exhausted rule falls back, never re-holds"


def test_approval_prompt_scripts_the_exact_command_then_the_final_reply(tmp_path):
    target = tmp_path / "approval-target.txt"
    prompt = (
        f"Use the shell tool to run exactly this command now: touch {target}. "
        "You must actually run the command with the shell tool; do not reply before it has run."
    )
    before = exec_call_then_ok_factory(prompt)
    call = next(e for e in _events_of(before) if e["type"] == "response.output_item.done")
    assert call["item"]["type"] == "function_call"
    arguments = json.loads(call["item"]["arguments"])
    assert arguments["cmd"] == f"touch {target}"
    assert arguments["sandbox_permissions"] == "require_escalated"
    target.write_text("approved\n")
    after = exec_call_then_ok_factory(prompt)
    assert after.hold_seconds == 0
    done = next(e for e in _events_of(after) if e["type"] == "response.output_item.done")
    assert done["item"]["content"][0]["text"] == "ok"


def test_unmatched_requests_get_the_bounded_default():
    provider = MockResponsesProvider(standard_rules())
    script, marker = provider.select_script("an unrelated prompt")
    assert marker is None
    assert script.hold_seconds == 0


@pytest.mark.parametrize("logging", [False, True])
def test_compat_log_records_all_methods_without_changing_statuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logging: bool
) -> None:
    import http.client

    log = tmp_path / "requests.jsonl"
    if logging:
        monkeypatch.setenv("THEATER_COMPAT_MOCK_LOG", str(log))
    else:
        monkeypatch.delenv("THEATER_COMPAT_MOCK_LOG", raising=False)
    provider = MockResponsesProvider()
    provider.start()
    requests = [
        ("POST", "/v1/responses", 200, True),
        ("POST", "/v1/analytics/codex/turn-costs?x=1", 204, False),
        ("GET", "/v1/models", 200, True),
        ("GET", "/v1/unknown?q=1", 404, False),
        ("PUT", "/v1/unknown", 501, False),
        ("PATCH", "/v1/unknown", 501, False),
        ("DELETE", "/v1/unknown", 501, False),
        ("HEAD", "/v1/unknown", 501, False),
        ("OPTIONS", "/v1/unknown", 501, False),
        ("TRACE", "/v1/unknown", 501, False),
        ("CONNECT", "/v1/unknown", 501, False),
        ("BREW", "/v1/unknown", 501, False),
    ]
    try:
        parsed = urlparse(provider.base_url)
        statuses = []
        for method, path, _status, _scripted in requests:
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
            body = json.dumps({"input": []}) if method == "POST" else None
            connection.request(method, path, body=body)
            response = connection.getresponse()
            statuses.append(response.status)
            response.read()
            connection.close()
    finally:
        provider.stop()
    assert statuses == [status for _method, _path, status, _scripted in requests]
    if not logging:
        assert not log.exists()
        return
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert records == [
        {
            "method": method,
            "path": path.split("?", 1)[0],
            "scripted": scripted,
        }
        for method, path, _status, scripted in requests
    ]


@pytest.mark.parametrize("logging", [False, True])
def test_bootstrap_compat_log_records_every_method_without_changing_responses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, logging: bool
) -> None:
    import http.client

    mock = _bootstrap_mock_module()
    log = tmp_path / "bootstrap.jsonl"
    if logging:
        monkeypatch.setenv("THEATER_COMPAT_MOCK_LOG", str(log))
    else:
        monkeypatch.delenv("THEATER_COMPAT_MOCK_LOG", raising=False)
    server = mock.MockResponsesServer([mock.final_assistant_message_response("ok")])
    server.start()
    requests = [
        ("POST", "/v1/responses", 200, True),
        ("POST", "/v1/new-required-endpoint?x=1", 404, False),
        ("GET", "/v1/models", 200, True),
        ("GET", "/v1/unknown?q=1", 404, False),
        ("OPTIONS", "/v1/unknown", 204, False),
        ("PUT", "/v1/unknown", 501, False),
        ("PATCH", "/v1/unknown", 501, False),
        ("DELETE", "/v1/unknown", 501, False),
        ("HEAD", "/v1/unknown", 501, False),
        ("TRACE", "/v1/unknown", 501, False),
        ("CONNECT", "/v1/unknown", 501, False),
        ("BREW", "/v1/unknown", 501, False),
    ]
    try:
        parsed = urlparse(server.base_url)
        responses = []
        for method, path, _status, _scripted in requests:
            connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=5)
            body = "{}" if method == "POST" else None
            connection.request(method, path, body=body)
            response = connection.getresponse()
            responses.append((response.status, response.read()))
            connection.close()
    finally:
        server.stop()
    assert [status for status, _body in responses] == [
        status for _method, _path, status, _scripted in requests
    ]
    for (method, _path, status, _scripted), (_actual, body) in zip(
        requests, responses, strict=True
    ):
        if status == 501 and method != "HEAD":
            assert f"Unsupported method ('{method}')".encode() in body
    if not logging:
        assert not log.exists()
        return
    records = [json.loads(line) for line in log.read_text().splitlines()]
    assert records == [
        {"method": method, "path": path.split("?", 1)[0], "scripted": scripted}
        for method, path, _status, scripted in requests
    ]


def test_held_stream_serves_pre_events_then_completes_and_survives_disconnect():
    """End to end over a real socket: the held stream writes its pre events,
    keeps the connection open on keepalives, completes after the hold, and a
    client that drops mid-hold (steer/interrupt) never kills the provider."""
    provider = MockResponsesProvider(
        [
            Rule(
                "essay about rivers",
                fifo_script(
                    [held_essay_stream("delta", hold_seconds=1.0)], final_message_stream("ok")
                ),
            )
        ]
    )
    provider.start()
    try:
        first = _post(provider.base_url + "/responses", RIVERS, timeout=15.0)
        assert first.startswith("event: response.created")
        assert "output_text.delta" in first
        assert "keepalive" in first, "the hold must emit SSE comments while blocked"
        assert '"type": "response.completed"' in first, "the tail completes the stream"

        port = urlparse(provider.base_url).port
        dropped = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        dropped.settimeout(5.0)
        dropped.connect(("127.0.0.1", port))
        dropped.sendall(_post_bytes(RIVERS))
        dropped.recv(1024)  # the pre events arrive, the stream is now held
        dropped.close()  # abort mid-hold, the codex steer/interrupt shape
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if len(provider.requests) == 2:
                break
            time.sleep(0.05)
        assert len(provider.requests) == 2, "the provider must survive an aborted held stream"

        after = _post(provider.base_url + "/responses", RIVERS, timeout=15.0)
        assert '"type": "response.completed"' in after, "exhausted rule serves the fallback"
        assert [r.marker for r in provider.requests] == ["essay about rivers"] * 3
    finally:
        provider.stop()


def _post_bytes(prompt: str) -> bytes:
    """A raw /v1/responses POST the socket-level abort test can send by hand."""
    body = json.dumps(
        {
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                }
            ]
        }
    ).encode()
    return (
        "POST /v1/responses HTTP/1.1\r\nHost: 127.0.0.1\r\n"
        f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n\r\n"
    ).encode() + body


def _post(base_url: str, prompt: str, *, timeout: float) -> str:
    """POST /v1/responses and read the whole (streaming) body."""
    import http.client

    parsed = urlparse(base_url)
    connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=timeout)
    connection.request(
        "POST",
        "/v1/responses",
        body=_post_bytes(prompt)[_post_bytes(prompt).index(b"\r\n\r\n") + 4 :],
        headers={"Content-Type": "application/json"},
    )
    response = connection.getresponse()
    payload = response.read().decode()
    connection.close()
    return payload


def test_fifo_script_ordering_is_deterministic():
    one = escalated_exec_call_stream("touch a")
    two = final_message_stream("ok")
    factory = fifo_script([one, two], final_message_stream("fallback"))
    assert factory("") is one
    assert factory("") is two
    assert factory("") is not one


def test_fetch_refuses_an_archive_that_does_not_match_the_published_digest(
    tmp_path, monkeypatch
) -> None:
    from tests.native import codex_fetch_release as fetch

    archive = tmp_path / "codex.tar.gz"
    archive.write_bytes(b"not the published bytes")
    monkeypatch.setattr(fetch, "asset_name", lambda version: "codex.tar.gz")
    monkeypatch.setattr(fetch, "sigstore_assets", lambda tag: [])
    monkeypatch.setattr(fetch, "download_asset", lambda tag, asset, dest: archive)
    monkeypatch.setattr(fetch, "published_digest", lambda tag, asset: "0" * 64)
    unpacked: list[Path] = []
    monkeypatch.setattr(fetch, "unpack", lambda archive, dest: unpacked.append(archive))
    with pytest.raises(fetch.FetchError, match="does not match the digest"):
        fetch.fetch_release("0.0.0", tmp_path / "bin")
    assert unpacked == []
