"""require_absent waits out provider churn within a bound, never focus that cannot be read."""

from __future__ import annotations

import asyncio

import pytest

from tests.test_rc10_provider_presence import Clock, Registry, TerminalService, binding, result
from theater.constants.refusals import NEXT_FOCUS_EVENTS_OFF, NEXT_RETRY_ONCE
from theater.daemon.presence import PresenceMonitor
from theater.models import HumanPresent, Participant


def _monitor(*, transient_wait: float = 5.0):
    registry = Registry(Participant(id="participant-a", harness="pi"), binding())
    service = TerminalService(registry)
    monitor = PresenceMonitor(
        registry, clock=Clock(), stale_after=5.0, transient_wait=transient_wait
    )
    monitor.configure_terminal_service(service)
    return monitor, service


def _go_stale(service, monkeypatch) -> None:
    service.connections.generation = 8
    monkeypatch.setattr(service.connections, "current_generation", lambda _provider: None)


async def test_stale_generation_that_recovers_inside_the_bound_is_admitted(monkeypatch):
    monitor, service = _monitor()
    _go_stale(service, monkeypatch)

    async def provider_reconnects() -> None:
        await asyncio.sleep(0.3)
        service.connections.generation = 7
        service.responses.append(result("absent", 1))

    reconnect = asyncio.create_task(provider_reconnects())
    try:
        await asyncio.wait_for(monitor.require_absent("participant-a"), 2)
    finally:
        await reconnect
        await monitor.aclose()
    assert service.inspect_calls == 1  # absence came from one fresh inspection


async def test_stale_generation_past_the_bound_refuses_with_retry_once(monkeypatch):
    monitor, service = _monitor(transient_wait=0.3)
    _go_stale(service, monkeypatch)
    with pytest.raises(HumanPresent) as refused:
        await asyncio.wait_for(monitor.require_absent("participant-a"), 2)
    assert "provider-generation-stale" in str(refused.value)
    assert refused.value.details == {"next": NEXT_RETRY_ONCE}
    await monitor.aclose()


async def test_focus_events_unavailable_is_refused_without_waiting():
    monitor, service = _monitor()
    unreadable = result("unknown", 1)
    unreadable["presence"]["reason"] = "focus_events_unavailable"
    service.responses.append(unreadable)
    with pytest.raises(HumanPresent) as refused:
        await asyncio.wait_for(monitor.require_absent("participant-a"), 1)
    assert refused.value.details == {"next": NEXT_FOCUS_EVENTS_OFF}
    assert service.inspect_calls == 1
    await monitor.aclose()
