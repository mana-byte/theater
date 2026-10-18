"""An enrichment failing, timing out or being cancelled never fails the primary read."""

from __future__ import annotations

import asyncio

import pytest

from tests.support.composite_sources import (
    BoomSource,
    ControlledSource,
    SlowSource,
    decl,
    make_fact,
)
from theater.constants.harness import HARNESS_ENRICHMENT_READ_TIMEOUT_SECONDS
from theater.harness.channels import CompositeSource, EnrichmentBinding
from theater.harness.contracts.channels import ChannelHealthState
from theater.harness.contracts.source import Batch, Source


class _BadReturnTypeSource(Source):
    async def read(self) -> Batch:
        return "not-a-batch"  # type: ignore[return-value]


class _ErrorBatchSource(Source):
    async def read(self) -> Batch:
        return Batch(error_code="enrichment_error", error="secret-enrichment-error")


@pytest.mark.asyncio
async def test_enrichment_exception_does_not_fail_primary() -> None:
    primary = ControlledSource()
    primary.set_batch(Batch(progressed=True))
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=BoomSource(), declaration=decl("boom"))],
    )
    batch = await composite.read()
    assert batch.progressed is True
    assert batch.trajectory == ()
    health = composite.channel_health()
    assert health[0].state is ChannelHealthState.FAILED
    await composite.aclose()


@pytest.mark.asyncio
async def test_enrichment_timeout_does_not_fail_primary() -> None:
    primary = ControlledSource()
    primary.set_batch(Batch(progressed=True))
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=SlowSource(delay=10.0), declaration=decl("slow"))],
        enrichment_timeout=0.01,
    )
    batch = await asyncio.wait_for(composite.read(), timeout=5.0)
    assert batch.progressed is True
    health = composite.channel_health()
    assert health[0].state is ChannelHealthState.DEGRADED
    await composite.aclose()


@pytest.mark.asyncio
async def test_enrichment_malformed_return_does_not_fail_primary() -> None:
    primary = ControlledSource()
    primary.set_batch(Batch(progressed=True))
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=_BadReturnTypeSource(), declaration=decl("bad"))],
    )
    batch = await composite.read()
    assert batch.progressed is True
    health = composite.channel_health()
    assert health[0].state is ChannelHealthState.FAILED
    await composite.aclose()


@pytest.mark.asyncio
async def test_enrichment_error_batch_updates_only_that_health() -> None:
    primary = ControlledSource()
    primary.set_batch(Batch(progressed=True))
    ok_source = ControlledSource()
    ok_source.set_batch(Batch(trajectory=[make_fact("ok", 0)]))
    composite = CompositeSource(
        primary=primary,
        enrichments=[
            EnrichmentBinding(source=ok_source, declaration=decl("ok")),
            EnrichmentBinding(source=_ErrorBatchSource(), declaration=decl("err")),
        ],
    )
    batch = await composite.read()
    assert batch.progressed is True
    assert len(batch.trajectory) == 1
    assert batch.trajectory[0].native_id == "ok"
    health = composite.channel_health()
    assert health[0].state is ChannelHealthState.HEALTHY
    assert health[1].state is ChannelHealthState.DEGRADED
    assert health[1].dropped == 0
    assert health[1].diagnostics == ("enrichment read returned error (enrichment_error)",)
    assert "secret-enrichment-error" not in str(health[1])
    await composite.aclose()


@pytest.mark.asyncio
async def test_enrichment_recovers_to_healthy_on_later_valid_read() -> None:
    class FlakySource(Source):
        def __init__(self) -> None:
            self._count = 0

        async def read(self) -> Batch:
            self._count += 1
            if self._count == 1:
                raise RuntimeError("transient")
            return Batch(trajectory=[make_fact("recovered", 0)])

    primary = ControlledSource()
    primary.set_batch(Batch(progressed=True))
    flaky = FlakySource()
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=flaky, declaration=decl("flaky"))],
    )
    await composite.read()
    assert composite.channel_health()[0].state is ChannelHealthState.FAILED
    batch2 = await composite.read()
    assert composite.channel_health()[0].state is ChannelHealthState.HEALTHY
    assert any(f.native_id == "recovered" for f in batch2.trajectory)
    await composite.aclose()


@pytest.mark.asyncio
async def test_cancellation_cancels_and_awaits_child_tasks() -> None:
    primary = ControlledSource()
    primary.set_batch(Batch(progressed=True))
    slow = SlowSource(delay=10.0)
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=slow, declaration=decl("slow"))],
        enrichment_timeout=10.0,
    )
    read_task = asyncio.create_task(composite.read())
    await asyncio.sleep(0.05)
    read_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await read_task
    await composite.aclose()


@pytest.mark.asyncio
async def test_primary_cancellation_does_not_report_failure() -> None:
    class CancelledSource(Source):
        async def read(self) -> Batch:
            raise asyncio.CancelledError

    composite = CompositeSource(primary=CancelledSource())

    with pytest.raises(asyncio.CancelledError):
        await composite.read()

    assert composite.primary_health().state is ChannelHealthState.STARTING


def test_enrichment_health_failure_is_contained_and_redacted() -> None:
    class BrokenHealthSource(Source):
        async def read(self) -> Batch:
            return Batch()

        def channel_health(self):
            raise ValueError("secret health value")

    composite = CompositeSource(
        enrichments=[EnrichmentBinding(source=BrokenHealthSource(), declaration=decl("h"))]
    )

    (health,) = composite.channel_health()

    assert health.state is ChannelHealthState.DEGRADED
    assert health.diagnostics == ("channel health snapshot failed (ValueError)",)
    assert "secret health value" not in str(health)


def test_enrichment_timeout_default_is_named_constant() -> None:
    composite = CompositeSource()
    assert composite._enrichment_timeout == HARNESS_ENRICHMENT_READ_TIMEOUT_SECONDS
