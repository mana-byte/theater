"""Offline tests for the mock provider's dispatch, hold, and steer scripting.

No codex binary and no tmux: these prove the scripting rules the
qualification relies on — last-user-message dispatch (the steer seam),
structured-request filler, FIFO exhaustion, the held stream's
disconnect tolerance, and the approval exec-call derivation.

Everything else here (the suites that need the real binary) is opt-in in
tests/test_codex_native_runtime_proof.py.
"""

from __future__ import annotations

import json
import socket
import time
from urllib.parse import urlparse

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

STEER_TEXT = "Stop writing the essay immediately. Reply with exactly: steered"
RIVERS = "Write a long, detailed 1200 word essay about rivers."


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
