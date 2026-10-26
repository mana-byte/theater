"""Primary and enrichment channel health transitions, diagnostics and counters."""

from __future__ import annotations

import asyncio

import pytest

from tests.support.composite_sources import BoomSource, ControlledSource, SlowSource, decl
from theater.constants.harness import HARNESS_CHANNEL_HEALTH_COUNTER_MAX
from theater.harness.channels import ChannelHealthTracker, CompositeSource, EnrichmentBinding
from theater.harness.channels.health import merge_channel_health
from theater.harness.contracts.channels import ChannelHealth, ChannelHealthState
from theater.harness.contracts.source import Batch, Source


class _PrimaryWithError(Source):
    async def read(self) -> Batch:
        return Batch(error_code="primary_err", error="secret-primary-error")


class _PrimaryRaises(Source):
    async def read(self) -> Batch:
        raise ValueError("secret-primary-value")


@pytest.mark.asyncio
async def test_primary_health_starts_as_starting() -> None:
    primary = ControlledSource()
    composite = CompositeSource(primary=primary)
    assert composite.primary_health() is not None
    assert composite.primary_health().state is ChannelHealthState.STARTING


@pytest.mark.asyncio
async def test_primary_health_transitions_to_healthy_after_clean_read() -> None:
    primary = ControlledSource()
    primary.set_batch(Batch(progressed=True))
    composite = CompositeSource(primary=primary)
    await composite.read()
    health = composite.primary_health()
    assert health.state is ChannelHealthState.HEALTHY
    assert health.accepted == 0
    assert health.last_success_at is not None


@pytest.mark.asyncio
async def test_primary_health_transitions_to_degraded_on_error() -> None:
    primary = _PrimaryWithError()
    composite = CompositeSource(primary=primary)
    await composite.read()
    health = composite.primary_health()
    assert health.state is ChannelHealthState.DEGRADED
    assert health.diagnostics == ("primary read returned error (primary_err)",)
    assert "secret-primary-error" not in str(health)


@pytest.mark.asyncio
async def test_primary_exception_is_recorded_without_changing_the_exception() -> None:
    composite = CompositeSource(primary=_PrimaryRaises())

    with pytest.raises(ValueError, match="secret-primary-value"):
        await composite.read()

    health = composite.primary_health()
    assert health.state is ChannelHealthState.FAILED
    assert health.diagnostics == ("primary read failed (ValueError)",)
    assert "secret-primary-value" not in str(health)


@pytest.mark.asyncio
async def test_primary_health_recovers_to_healthy() -> None:
    class FlakyPrimary(Source):
        def __init__(self) -> None:
            self._count = 0

        async def read(self) -> Batch:
            self._count += 1
            if self._count == 1:
                return Batch(error_code="err", error="bad")
            return Batch()

    primary = FlakyPrimary()
    composite = CompositeSource(primary=primary)
    await composite.read()
    assert composite.primary_health().state is ChannelHealthState.DEGRADED
    await composite.read()
    assert composite.primary_health().state is ChannelHealthState.HEALTHY


@pytest.mark.asyncio
async def test_primary_health_none_without_primary() -> None:
    composite = CompositeSource()
    assert composite.primary_health() is None


@pytest.mark.asyncio
async def test_health_starts_as_starting() -> None:
    composite = CompositeSource(
        enrichments=[EnrichmentBinding(source=ControlledSource(), declaration=decl("h"))]
    )
    health = composite.channel_health()
    assert health[0].state is ChannelHealthState.STARTING


@pytest.mark.asyncio
async def test_health_transitions_to_healthy_after_read() -> None:
    primary = ControlledSource()
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=ControlledSource(), declaration=decl("h"))],
    )
    await composite.read()
    assert composite.channel_health()[0].state is ChannelHealthState.HEALTHY


@pytest.mark.asyncio
async def test_health_transitions_to_degraded_on_timeout() -> None:
    primary = ControlledSource()
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=SlowSource(), declaration=decl("h"))],
        enrichment_timeout=0.01,
    )
    await asyncio.wait_for(composite.read(), timeout=30.0)
    assert composite.channel_health()[0].state is ChannelHealthState.DEGRADED


@pytest.mark.asyncio
async def test_health_transitions_to_failed_on_exception() -> None:
    primary = ControlledSource()
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=BoomSource(), declaration=decl("h"))],
    )
    await composite.read()
    health = composite.channel_health()[0]
    assert health.state is ChannelHealthState.FAILED
    assert health.diagnostics == ("enrichment read failed (ValueError)",)
    assert "secret-enrichment-value" not in str(health)


@pytest.mark.asyncio
async def test_health_transitions_back_to_healthy_after_recovery() -> None:
    class FlakySource(Source):
        def __init__(self) -> None:
            self._count = 0

        async def read(self) -> Batch:
            self._count += 1
            if self._count == 1:
                raise RuntimeError("once")
            return Batch()

    primary = ControlledSource()
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=FlakySource(), declaration=decl("h"))],
    )
    await composite.read()
    assert composite.channel_health()[0].state is ChannelHealthState.FAILED
    await composite.read()
    assert composite.channel_health()[0].state is ChannelHealthState.HEALTHY


@pytest.mark.asyncio
async def test_health_in_declaration_order() -> None:
    primary = ControlledSource()
    composite = CompositeSource(
        primary=primary,
        enrichments=[
            EnrichmentBinding(source=ControlledSource(), declaration=decl("alpha")),
            EnrichmentBinding(source=BoomSource(), declaration=decl("beta")),
        ],
    )
    await composite.read()
    health = composite.channel_health()
    assert health[0].channel_id == "alpha"
    assert health[0].state is ChannelHealthState.HEALTHY
    assert health[1].channel_id == "beta"
    assert health[1].state is ChannelHealthState.FAILED


def test_health_tracker_diagnostics_bounded_and_sanitized() -> None:
    tracker = ChannelHealthTracker("h")
    for i in range(20):
        tracker.mark_failed(f"error-{i:04d} with padding " * 10)
    snap = tracker.snapshot()
    assert len(snap.diagnostics) <= 8
    for d in snap.diagnostics:
        assert len(d) <= 240


def test_health_tracker_diagnostics_are_terminal_safe() -> None:
    tracker = ChannelHealthTracker("h")
    tracker.mark_failed("\n\t\x00")
    tracker.mark_failed("first\nsecond\tthird")

    assert tracker.snapshot().diagnostics == (
        "channel diagnostic unavailable",
        "first second third",
    )


def test_health_tracker_dropped_counter() -> None:
    tracker = ChannelHealthTracker("h")
    tracker.drop()
    tracker.drop()
    snap = tracker.snapshot()
    assert snap.dropped == 2


def test_health_tracker_counters_saturate() -> None:
    tracker = ChannelHealthTracker("h")
    tracker.record_accepted(HARNESS_CHANNEL_HEALTH_COUNTER_MAX)
    tracker.record_accepted()
    tracker.drop(HARNESS_CHANNEL_HEALTH_COUNTER_MAX)
    tracker.drop()

    snap = tracker.snapshot()
    assert snap.accepted == HARNESS_CHANNEL_HEALTH_COUNTER_MAX
    assert snap.dropped == HARNESS_CHANNEL_HEALTH_COUNTER_MAX


def test_merged_health_does_not_double_count_shared_tracker_snapshots() -> None:
    earlier = ChannelHealth(
        channel_id="h",
        state=ChannelHealthState.HEALTHY,
        diagnostics=("earlier",),
        accepted=HARNESS_CHANNEL_HEALTH_COUNTER_MAX,
        dropped=2,
        last_success_at=1,
    )
    later = ChannelHealth(
        channel_id="h",
        state=ChannelHealthState.DEGRADED,
        diagnostics=("earlier", "later"),
        accepted=HARNESS_CHANNEL_HEALTH_COUNTER_MAX,
        dropped=3,
        last_success_at=2,
    )

    merged = merge_channel_health(earlier, later)

    assert merged.state is ChannelHealthState.DEGRADED
    assert merged.diagnostics == ("earlier", "later")
    assert merged.accepted == HARNESS_CHANNEL_HEALTH_COUNTER_MAX
    assert merged.dropped == 3
    assert merged.last_success_at == 2


@pytest.mark.parametrize("field", ["accepted", "dropped"])
def test_channel_health_rejects_unbounded_counters(field: str) -> None:
    with pytest.raises(ValueError, match="bounded"):
        ChannelHealth(channel_id="h", **{field: HARNESS_CHANNEL_HEALTH_COUNTER_MAX + 1})


@pytest.mark.parametrize("at", [True, float("nan"), float("inf"), float("-inf"), -1])
def test_health_tracker_rejects_invalid_success_times(at: object) -> None:
    with pytest.raises(ValueError, match="success time"):
        ChannelHealthTracker("h").record_success(at=at)  # type: ignore[arg-type]


def test_health_snapshot_is_immutable() -> None:
    tracker = ChannelHealthTracker("h")
    tracker.mark_healthy()
    snap = tracker.snapshot()
    assert isinstance(snap, ChannelHealth)
    assert snap.state is ChannelHealthState.HEALTHY
