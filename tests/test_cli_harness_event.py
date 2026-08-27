"""Focused hidden generic hook CLI tests."""

from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace

import pytest

from theater.cli.commands import identity


def _args(token_file, *, strict: bool = False):
    return SimpleNamespace(
        id="participant",
        channel="native-hooks",
        event="tool.finished",
        delivery_id="delivery-1",
        token_file=str(token_file),
        strict_exit=strict,
    )


def test_harness_event_forwards_opaque_object_without_autostart(monkeypatch, tmp_path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("secret\n")
    seen = {}

    async def send(args, *, token, payload) -> None:
        seen["args"] = args
        seen["token"] = token
        seen["payload"] = payload

    monkeypatch.setattr(identity, "_send_harness_event", send)
    monkeypatch.setattr(identity.sys, "stdin", io.StringIO('{"native_id":"n-1"}'))

    assert identity.cmd_harness_event(_args(token_file)) == 0
    assert seen["token"] == "secret"
    assert seen["payload"] == {"native_id": "n-1"}


def test_harness_event_sender_disables_daemon_autostart(monkeypatch, tmp_path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("secret\n")
    seen = {}

    class Client:
        def __init__(self, *, autostart: bool) -> None:
            seen["autostart"] = autostart

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def call(self, method, **params) -> None:
            seen["method"] = method
            seen["params"] = params

    monkeypatch.setattr(identity, "DaemonClient", Client)
    asyncio.run(
        identity._send_harness_event(
            _args(token_file), token="secret", payload={"native_id": "n-1"}
        )
    )
    assert seen["autostart"] is False
    assert seen["method"] == "harness.event"


def test_harness_event_rejects_invalid_utf8_quietly_or_strictly(monkeypatch, tmp_path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("secret\n")
    invalid = io.TextIOWrapper(io.BytesIO(b"\xff"), encoding="utf-8")
    monkeypatch.setattr(identity.sys, "stdin", invalid)

    assert identity.cmd_harness_event(_args(token_file)) == 0
    invalid = io.TextIOWrapper(io.BytesIO(b"\xff"), encoding="utf-8")
    monkeypatch.setattr(identity.sys, "stdin", invalid)
    assert identity.cmd_harness_event(_args(token_file, strict=True)) == 1


def test_harness_event_rejects_oversized_token_file(monkeypatch, tmp_path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("x" * 128 + "\nextra")
    monkeypatch.setattr(identity.sys, "stdin", io.StringIO('{"native_id":"n-1"}'))
    assert identity.cmd_harness_event(_args(token_file)) == 0
    monkeypatch.setattr(identity.sys, "stdin", io.StringIO('{"native_id":"n-1"}'))
    assert identity.cmd_harness_event(_args(token_file, strict=True)) == 1


def _deep_payload() -> str:
    payload: dict[str, object] = {}
    for _ in range(10):
        payload = {"next": payload}
    return json.dumps(payload)


@pytest.mark.parametrize(
    "payload",
    (
        "not json",
        "[]",
        _deep_payload(),
        json.dumps({f"key-{index}": index for index in range(200)}),
    ),
)
def test_harness_event_rejects_malformed_or_unbounded_payloads(
    monkeypatch, tmp_path, payload
) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("secret\n")
    monkeypatch.setattr(identity.sys, "stdin", io.StringIO(payload))
    assert identity.cmd_harness_event(_args(token_file)) == 0
    monkeypatch.setattr(identity.sys, "stdin", io.StringIO(payload))
    assert identity.cmd_harness_event(_args(token_file, strict=True)) == 1
