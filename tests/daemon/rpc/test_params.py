"""RPC parameter validation: numeric params and missing arguments."""

from __future__ import annotations

from functools import partial
from types import SimpleNamespace

import pytest

from tests.support.tables import run_rows_async
from theater.daemon.observation.live import LiveObservationHub
from theater.daemon.rpc import hooks as hooks_mod
from theater.models import BadRequest, Status
from theater.protocol import RemoteError


async def test_missing_parameter_is_a_structured_error(client):
    with pytest.raises(RemoteError) as exc:
        await client.call("participants.get")
    assert exc.value.code == "bad_request"


@pytest.mark.parametrize("value", [None, "abc", True])
async def test_jobs_await_rejects_bad_max_wait(client, value):
    with pytest.raises(RemoteError) as exc:
        await client.call("jobs.await", handles=["missing"], max_wait=value)
    assert exc.value.code == "bad_request"


async def test_bus_tail_rejects_bad_numeric_params(client):
    """limit and after_id refuse non-integers; one connection serves every row."""

    async def rejects(key: str, value: object) -> None:
        with pytest.raises(RemoteError) as exc:
            await client.call("bus.tail", **{key: value})
        assert exc.value.code == "bad_request"

    await run_rows_async(
        (f"{key}={value!r}", partial(rejects, key, value))
        for key in ("limit", "after_id")
        for value in (None, "abc", True, 1.5)
    )


async def test_bus_tail_rejects_huge_after_id(client):
    with pytest.raises(RemoteError) as exc:
        await client.call("bus.tail", after_id=10**400)
    assert exc.value.code == "bad_request"


async def test_usage_rejects_bad_window(client):
    """stats, usage_totals and usage_summary all refuse non-finite windows."""

    async def rejects(method: str, value: object) -> None:
        with pytest.raises(RemoteError) as exc:
            await client.call(method, window=value)
        assert exc.value.code == "bad_request"

    await run_rows_async(
        (f"{method} window={value!r}", partial(rejects, method, value))
        for method in ("stats", "usage_totals", "usage_summary")
        for value in (None, "abc", True, float("nan"), float("inf"))
    )


@pytest.mark.parametrize("value", [None, "abc", True, 1.5])
async def test_recall_rejects_bad_depth(client, value):
    with pytest.raises(RemoteError) as exc:
        await client.call("recall", paths=["file.py"], depth=value)
    assert exc.value.code == "bad_request"


@pytest.mark.parametrize("path", ["../outside", "/outside", 1, ""])
async def test_recall_rejects_unsafe_paths(client, tmp_path, path):
    with pytest.raises(RemoteError) as exc:
        await client.call("recall", paths=[path], caller_cwd=str(tmp_path))
    assert exc.value.code == "bad_request"


@pytest.mark.parametrize("value", [None, 1, True, []])
async def test_usage_summary_rejects_non_string_period(client, value):
    with pytest.raises(RemoteError) as exc:
        await client.call("usage_summary", period=value)
    assert exc.value.code == "bad_request"


def _hook_daemon(token: str):
    participant = SimpleNamespace(
        id="p",
        harness="vibe",
        status=Status.IDLE,
        session_id=None,
        session_correlation=None,
        transcript_location=None,
    )
    credential = SimpleNamespace(harness="vibe", channel_id="chan", token=token)

    class Store:
        def get_participant(self, _pid):
            return participant

        def get_channel_credential(self, *_args):
            return credential

        def bus_append(self, *_args, **_kwargs):
            pass

    class Runtime:
        async def correlate(self, _binding, _context):
            return "native"

        def enqueue(self, **_kwargs):
            return SimpleNamespace(duplicate=False, dropped=False)

    bounds = SimpleNamespace(max_payload_bytes=1000)
    channel = SimpleNamespace(
        unavailable_reason=None,
        bindings=[SimpleNamespace(event="event")],
        declaration=SimpleNamespace(bounds=bounds),
    )
    daemon = SimpleNamespace(
        store=Store(),
        observer=SimpleNamespace(
            harnesses={"vibe": SimpleNamespace(observer=object())}, live=LiveObservationHub()
        ),
        hook_runtime=Runtime(),
    )
    return daemon, channel


async def test_hook_token_rejects_non_ascii_but_accepts_ascii(monkeypatch):
    daemon, channel = _hook_daemon("token")
    wake = daemon.observer.live.wake_signal("p")
    monkeypatch.setattr(hooks_mod, "_hook_channel", lambda _observer, _id: channel)
    params = {
        "id": "p",
        "channel": "chan",
        "event": "event",
        "payload": {"value": 1},
    }

    with pytest.raises(BadRequest, match="credential is invalid"):
        await hooks_mod._harness_event(daemon, {**params, "token": "tökén"})
    assert not wake.is_set()
    assert await hooks_mod._harness_event(daemon, {**params, "token": "token"}) == {
        "ok": True,
        "duplicate": False,
        "dropped": False,
    }
    assert wake.is_set()
