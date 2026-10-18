"""Enrichments merge in declaration order without disturbing the primary, and dedupe."""

from __future__ import annotations

import asyncio

import pytest

from tests.support.composite_sources import ControlledSource, decl, make_fact
from theater.constants.harness import HARNESS_DEDUPE_MAX_FACTS
from theater.harness.channels import CompositeSource, EnrichmentBinding
from theater.harness.contracts.source import Attachment, Batch, Source
from theater.models import Status
from theater.trajectory.enums import TrajectoryKind


class _DelayedSource(Source):
    def __init__(self, batch: Batch, delay: float = 0.0) -> None:
        self._batch = batch
        self._delay = delay

    async def read(self) -> Batch:
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._batch


@pytest.mark.asyncio
async def test_enrichments_merge_in_declaration_order_not_completion_order() -> None:
    fact_a = make_fact("a", 0, TrajectoryKind.TOOL_CALL)
    fact_b = make_fact("b", 0, TrajectoryKind.TOOL_RESULT)
    primary = ControlledSource()
    slow = _DelayedSource(Batch(trajectory=[fact_a]), delay=0.05)
    fast = _DelayedSource(Batch(trajectory=[fact_b]), delay=0.0)
    composite = CompositeSource(
        primary=primary,
        enrichments=[
            EnrichmentBinding(source=slow, declaration=decl("slow")),
            EnrichmentBinding(source=fast, declaration=decl("fast")),
        ],
    )
    batch = await composite.read()
    assert batch.trajectory[0].native_id == "a"
    assert batch.trajectory[1].native_id == "b"
    await composite.aclose()


@pytest.mark.asyncio
async def test_primary_control_fields_preserved_exactly() -> None:
    attachment = Attachment(location="/work/t.jsonl", session_id="s1")
    primary_batch = Batch(
        events=(),
        progressed=True,
        status=Status.WORKING,
        attached=attachment,
        waiting=False,
        error_code=None,
        error=None,
    )
    primary = ControlledSource()
    primary.set_batch(primary_batch)
    enrich = ControlledSource()
    enrich.set_batch(Batch(trajectory=[make_fact("e", 0)]))
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=enrich, declaration=decl())],
    )
    batch = await composite.read()
    assert batch.progressed is True
    assert batch.status is Status.WORKING
    assert batch.attached is attachment
    assert batch.waiting is False
    assert batch.error_code is None
    await composite.aclose()


@pytest.mark.asyncio
async def test_primary_events_untouched() -> None:
    from theater.harness.contracts.events import Event, EventKind

    event = Event(kind=EventKind.USER, text="hello")
    primary = ControlledSource()
    primary.set_batch(Batch(events=[event]))
    composite = CompositeSource(primary=primary)
    batch = await composite.read()
    assert tuple(batch.events) == (event,)
    await composite.aclose()


@pytest.mark.asyncio
async def test_no_false_progressed_from_enrichment() -> None:
    primary = ControlledSource()
    primary.set_batch(Batch(progressed=False))
    enrich = ControlledSource()
    enrich.set_batch(Batch(trajectory=[make_fact("e", 0)]))
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=enrich, declaration=decl())],
    )
    batch = await composite.read()
    assert batch.progressed is False
    await composite.aclose()


@pytest.mark.asyncio
async def test_no_false_status_from_enrichment() -> None:
    primary = ControlledSource()
    primary.set_batch(Batch(status=Status.IDLE))
    enrich = ControlledSource()
    enrich.set_batch(Batch(status=Status.WORKING))
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=enrich, declaration=decl())],
    )
    batch = await composite.read()
    assert batch.status is Status.IDLE
    await composite.aclose()


@pytest.mark.asyncio
async def test_no_false_waiting_from_enrichment() -> None:
    primary = ControlledSource()
    primary.set_batch(Batch(waiting=False))
    enrich = ControlledSource()
    enrich.set_batch(Batch(waiting=True))
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=enrich, declaration=decl())],
    )
    batch = await composite.read()
    assert batch.waiting is False
    await composite.aclose()


@pytest.mark.asyncio
async def test_no_false_attachment_from_enrichment() -> None:
    primary = ControlledSource()
    primary.set_batch(Batch())
    enrich = ControlledSource()
    enrich.set_batch(Batch(attached=Attachment(location="/work/enrich.jsonl")))
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=enrich, declaration=decl())],
    )
    batch = await composite.read()
    assert batch.attached is None
    await composite.aclose()


@pytest.mark.asyncio
async def test_primary_trajectory_passes_through_unchanged_across_repeated_polls() -> None:
    fact = make_fact("p-fact", 0)
    primary = ControlledSource()
    primary.set_batch(Batch(trajectory=[fact]))
    composite = CompositeSource(primary=primary)
    batch1 = await composite.read()
    batch2 = await composite.read()
    assert tuple(batch1.trajectory) == (fact,)
    assert tuple(batch2.trajectory) == (fact,)
    await composite.aclose()


@pytest.mark.asyncio
async def test_primary_trajectory_with_none_native_id_passes_through() -> None:
    fact = make_fact(native_id=None, revision=0)
    primary = ControlledSource()
    primary.set_batch(Batch(trajectory=[fact]))
    composite = CompositeSource(primary=primary)
    batch = await composite.read()
    assert tuple(batch.trajectory) == (fact,)
    await composite.aclose()


@pytest.mark.asyncio
async def test_dedup_across_polls_same_native_id_and_revision() -> None:
    fact = make_fact("dup", 0)
    primary = ControlledSource()
    enrich = ControlledSource()
    enrich.set_batch(Batch(trajectory=[fact]))
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=enrich, declaration=decl())],
    )
    batch1 = await composite.read()
    assert len(batch1.trajectory) == 1
    batch2 = await composite.read()
    assert len(batch2.trajectory) == 0
    await composite.aclose()


@pytest.mark.asyncio
async def test_dedup_different_revisions_both_pass() -> None:
    fact_v0 = make_fact("item", 0)
    fact_v1 = make_fact("item", 1)
    primary = ControlledSource()
    enrich = ControlledSource()
    enrich.set_batch(Batch(trajectory=[fact_v0, fact_v1]))
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=enrich, declaration=decl())],
    )
    batch = await composite.read()
    assert len(batch.trajectory) == 2
    await composite.aclose()


@pytest.mark.asyncio
async def test_dedup_different_channels_same_native_id_both_pass() -> None:
    """Different channel IDs produce different dedup keys even with same native_id."""
    fact = make_fact("shared", 0)
    primary = ControlledSource()
    enrich_a = ControlledSource()
    enrich_a.set_batch(Batch(trajectory=[fact]))
    enrich_b = ControlledSource()
    enrich_b.set_batch(Batch(trajectory=[fact]))
    composite = CompositeSource(
        primary=primary,
        enrichments=[
            EnrichmentBinding(source=enrich_a, declaration=decl("a")),
            EnrichmentBinding(source=enrich_b, declaration=decl("b")),
        ],
    )
    batch = await composite.read()
    assert len(batch.trajectory) == 2
    await composite.aclose()


@pytest.mark.asyncio
async def test_native_id_none_always_passes_through() -> None:
    fact_none = make_fact(native_id=None, revision=0)
    primary = ControlledSource()
    enrich = ControlledSource()
    enrich.set_batch(Batch(trajectory=[fact_none, fact_none]))
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=enrich, declaration=decl())],
    )
    batch1 = await composite.read()
    batch2 = await composite.read()
    assert len(batch1.trajectory) == 2
    assert len(batch2.trajectory) == 2
    await composite.aclose()


@pytest.mark.asyncio
async def test_dedupe_eviction_is_bounded_and_deterministic() -> None:
    from theater.harness.channels.composite import _DedupCache

    cache = _DedupCache(max_entries=3)
    f1 = make_fact("a", 0)
    f2 = make_fact("b", 0)
    f3 = make_fact("c", 0)
    f4 = make_fact("d", 0)
    result1 = cache.filter("ch", [f1, f2, f3])
    assert len(result1) == 3
    result2 = cache.filter("ch", [f1])
    assert len(result2) == 0
    result3 = cache.filter("ch", [f4])
    assert len(result3) == 1
    result4 = cache.filter("ch", [f1])
    assert len(result4) == 1


def test_dedupe_max_is_named_constant() -> None:
    from theater.harness.channels.composite import _DedupCache

    cache = _DedupCache()
    assert cache._max == HARNESS_DEDUPE_MAX_FACTS
