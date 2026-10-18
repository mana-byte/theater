"""Régie bridge durable state files and callback timing logs."""

from __future__ import annotations

import asyncio
import stat
import threading
from pathlib import Path

import pytest
from regie.bridge import timing
from regie.bridge.persistence import BridgePersistence
from regie.bridge.state import BridgeAlreadyRunning, BridgeStateStore

from theater.frontend import CallbackRequest, CallbackResponse


@pytest.mark.parametrize("cancel_at", ["executing", "queued"])
async def test_persistence_cancellation_drains_io_before_releasing_its_fence(cancel_at):
    persistence = BridgePersistence()
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    operations = []

    def write():
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        operations.append("write")

    first = asyncio.create_task(persistence.run(write))
    tasks = [first]
    try:
        await started.wait()
        second = asyncio.create_task(persistence.run(operations.append, "next"))
        tasks.append(second)
        await asyncio.sleep(0)
        cancelled = first if cancel_at == "executing" else second
        cancelled.cancel()
        await asyncio.sleep(0)
        cancelled.cancel()
        await asyncio.sleep(0)
        assert not first.done() and not second.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        await (second if cancel_at == "executing" else first)
        assert operations == ["write", "next"]
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_bridge_state_is_private_durable_and_exclusively_locked(tmp_path: Path) -> None:
    root = tmp_path / "bridge"
    first = BridgeStateStore(root)
    state = first.acquire()
    try:
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert stat.S_IMODE(first.state_path.stat().st_mode) == 0o600
        assert "provider_credential" not in repr(state)

        first.update(provider_id="provider-a", tmux_server_identity="server-a")
        first.write_receipt(
            "terminal.deliver",
            "operation-a",
            {"operation_id": "operation-a", "delivery": "accepted"},
        )
        assert first.receipts()[0]["delivery"] == "accepted"

        with pytest.raises(BridgeAlreadyRunning):
            BridgeStateStore(root).acquire()
    finally:
        first.release()

    reopened = BridgeStateStore(root)
    persisted = reopened.acquire()
    try:
        assert persisted.provider_id == "provider-a"
        assert persisted.provider_credential == state.provider_credential
        assert stat.S_IMODE(next(reopened.receipt_dir.iterdir()).stat().st_mode) == 0o600
        reopened.clear_receipts()
        assert reopened.receipts() == ()
    finally:
        reopened.release()


def test_receipts_are_removed_only_after_explicit_daemon_acknowledgment(tmp_path: Path) -> None:
    state = BridgeStateStore(tmp_path / "bridge")
    state.acquire()
    try:
        for operation_id in ("accepted", "ignored", "deferred"):
            state.write_receipt(
                "terminal.deliver",
                operation_id,
                {"operation_id": operation_id, "delivery": "accepted"},
            )
        receipts = state.receipts()

        state.acknowledge_receipts(
            receipts,
            acknowledged_operation_ids=["accepted", "ignored"],
            ignored_operation_ids=["ignored"],
        )

        assert [receipt["operation_id"] for receipt in state.receipts()] == ["deferred"]
        quarantined = state.state_dir / "unmatched-receipts"
        assert len(tuple(quarantined.glob("*.json"))) == 1
    finally:
        state.release()


def _call(monkeypatch, method: str, handler, *ticks: float):
    clock = iter(ticks)
    monkeypatch.setattr(timing, "monotonic", lambda: next(clock))
    request = CallbackRequest("1", method, {"operation_id": "op", "terminal_id": "%1"}, 1)
    return asyncio.run(timing.timed_handlers({method: handler})[method](request))


def test_mutation_logs_phases_and_result(monkeypatch, caplog):
    caplog.set_level("INFO", logger="regie.bridge.latency")

    async def handler(_request):
        with timing.phase("effect"):
            pass
        return {"delivery": "accepted"}

    result = _call(monkeypatch, "terminal.terminate", handler, 1.0, 1.010, 1.030, 1.050)
    assert result == {"delivery": "accepted"}
    assert caplog.messages == [
        "callback.terminal.terminate 50.0ms operation_id=op terminal_id=%1 "
        "result=success effect_ms=20.0"
    ]


def test_create_logs_unknown_outcome(monkeypatch, caplog):
    caplog.set_level("INFO", logger="regie.bridge.latency")

    async def handler(_request):
        return {"outcome": "unknown"}

    _call(monkeypatch, "terminal.create", handler, 1.0, 1.001)
    assert caplog.messages[0].endswith("result=unknown")


def test_fast_successful_reads_are_not_logged(monkeypatch, caplog):
    caplog.set_level("INFO", logger="regie.bridge.latency")

    async def handler(_request):
        return {"terminal": {}}

    _call(monkeypatch, "terminal.inspect", handler, 1.0, 1.020)
    assert caplog.messages == []


def test_slow_or_refused_reads_are_logged(monkeypatch, caplog):
    caplog.set_level("INFO", logger="regie.bridge.latency")

    async def refuse(_request):
        return CallbackResponse(error={"code": "human_present", "message": "no"})

    _call(monkeypatch, "terminal.inspect", refuse, 1.0, 1.001)
    assert caplog.messages[0].endswith("result=human_present")


def test_exceptions_propagate_and_are_recorded(monkeypatch, caplog):
    caplog.set_level("INFO", logger="regie.bridge.latency")
    error = RuntimeError("tmux failed")

    async def fail(_request):
        raise error

    with pytest.raises(RuntimeError) as caught:
        _call(monkeypatch, "terminal.deliver", fail, 1.0, 1.001)
    assert caught.value is error
    assert caplog.messages[0].endswith("result=exception")


def test_phase_outside_a_callback_is_a_noop():
    with timing.phase("orphan"):
        pass
