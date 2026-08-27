"""Focused tests for bounded channel composition."""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from theater.constants.harness import (
    HARNESS_CHANNEL_HEALTH_COUNTER_MAX,
    HARNESS_CHANNEL_ID_MAX_CHARS,
    HARNESS_DEDUPE_MAX_FACTS,
    HARNESS_ENRICHMENT_READ_TIMEOUT_SECONDS,
)
from theater.constants.trajectory import TRAJECTORY_PAGE_RECORD_LIMIT
from theater.harness.channels import ChannelHealthTracker, CompositeSource, EnrichmentBinding
from theater.harness.channels.composite import CompositeSourceError
from theater.harness.channels.health import merge_channel_health
from theater.harness.contracts.channels import (
    ChannelCapability,
    ChannelDeclaration,
    ChannelHealth,
    ChannelHealthState,
    ChannelKind,
    SignalKind,
    SignalOwnership,
)
from theater.harness.contracts.source import (
    Attachment,
    Batch,
    History,
    HistoryPage,
    IdentityLossEvidence,
    Source,
    SourceContractError,
)
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.models import Status
from theater.trajectory.enums import TrajectoryKind


def _fact(
    native_id: str | None = None,
    revision: int = 0,
    kind: TrajectoryKind = TrajectoryKind.TOOL_CALL,
) -> TrajectoryFact:
    return TrajectoryFact(kind=kind, native_id=native_id, revision=revision)


def _decl(
    channel_id: str = "hook",
    kind: ChannelKind = ChannelKind.HOOK,
    capabilities: tuple[ChannelCapability, ...] = (),
) -> ChannelDeclaration:
    return ChannelDeclaration(id=channel_id, kind=kind, capabilities=capabilities)


class _DelayedSource(Source):
    def __init__(self, batch: Batch, delay: float = 0.0) -> None:
        self._batch = batch
        self._delay = delay

    async def read(self) -> Batch:
        if self._delay:
            await asyncio.sleep(self._delay)
        return self._batch


class _ControlledSource(Source):
    def __init__(self) -> None:
        self._batch: Batch = Batch()
        self.read_count = 0

    async def read(self) -> Batch:
        self.read_count += 1
        return self._batch

    def set_batch(self, batch: Batch) -> None:
        self._batch = batch


class _RecordingSource(Source):
    def __init__(self) -> None:
        self.closed = False
        self.close_count = 0

    async def read(self) -> Batch:
        return Batch()

    async def aclose(self) -> None:
        self.close_count += 1
        self.closed = True


class _BoomSource(Source):
    async def read(self) -> Batch:
        raise ValueError("secret-enrichment-value")


class _SlowSource(Source):
    def __init__(self, delay: float = 10.0) -> None:
        self._delay = delay

    async def read(self) -> Batch:
        await asyncio.sleep(self._delay)
        return Batch()


class _BadReturnTypeSource(Source):
    async def read(self) -> Batch:
        return "not-a-batch"  # type: ignore[return-value]


class _ErrorBatchSource(Source):
    async def read(self) -> Batch:
        return Batch(error_code="enrichment_error", error="secret-enrichment-error")


class _PrimaryWithAttachment(Source):
    collision_domain = "domain"

    def __init__(self) -> None:
        self.committed = False
        self.discarded = False
        self.revoked = False
        self.admitted: tuple[str, str] | None = None
        self._batch = Batch(attached=Attachment(location="/work/t.jsonl"))

    async def read(self) -> Batch:
        return self._batch

    def commit_attachment(self) -> None:
        self.committed = True

    def discard_attachment(self) -> None:
        self.discarded = True

    def revoke_attachment(self) -> None:
        self.revoked = True

    def admit_exact_location(self, *, location: str, session_id: str) -> str:
        self.admitted = (location, session_id)
        return "accepted"

    async def history(self, *, last_n: int) -> History:
        return History(location="/work/t.jsonl")

    async def history_page(self, *, before: str | None = None, limit: int = 100) -> HistoryPage:
        return HistoryPage(location="/work/t.jsonl")


class _PrimaryWithIdentityLoss(Source):
    async def read(self) -> Batch:
        return Batch()

    async def probe_identity_loss(self) -> IdentityLossEvidence | None:
        return IdentityLossEvidence(location="/work/other.jsonl", session_id="other")


class _PrimaryWithStatus(Source):
    def __init__(self, status: Status) -> None:
        self._status = status

    async def read(self) -> Batch:
        return Batch(status=self._status)


class _PrimaryWithError(Source):
    async def read(self) -> Batch:
        return Batch(error_code="primary_err", error="secret-primary-error")


class _PrimaryRaises(Source):
    async def read(self) -> Batch:
        raise ValueError("secret-primary-value")


class _CancelCloseSource(Source):
    async def read(self) -> Batch:
        return Batch()

    async def aclose(self) -> None:
        raise asyncio.CancelledError


# --- construction validation -------------------------------------------------


def test_primary_must_be_source_or_none() -> None:
    with pytest.raises(CompositeSourceError, match="primary must implement Source"):
        CompositeSource(primary="not-a-source")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "channel_id", ["", "Primary", "bad id", "a" * (HARNESS_CHANNEL_ID_MAX_CHARS + 1), None]
)
def test_primary_channel_id_must_be_bounded_and_canonical(channel_id: object) -> None:
    with pytest.raises(CompositeSourceError, match="primary_channel_id"):
        CompositeSource(
            primary=_ControlledSource(),
            primary_channel_id=channel_id,  # type: ignore[arg-type]
        )


def test_enrichment_must_be_binding() -> None:
    with pytest.raises(CompositeSourceError, match="enrichments"):
        CompositeSource(enrichments=["not-a-binding"])  # type: ignore[arg-type]


def test_enrichment_source_must_implement_source() -> None:
    with pytest.raises(CompositeSourceError, match="must implement Source"):
        CompositeSource(
            enrichments=[EnrichmentBinding(source="x", declaration=_decl())]  # type: ignore[arg-type]
        )


def test_duplicate_channel_ids_rejected() -> None:
    primary = _ControlledSource()
    binding = EnrichmentBinding(source=_ControlledSource(), declaration=_decl("primary"))
    with pytest.raises(CompositeSourceError, match="duplicates channel id"):
        CompositeSource(primary=primary, enrichments=[binding])


def test_duplicate_enrichment_ids_rejected() -> None:
    b1 = EnrichmentBinding(source=_ControlledSource(), declaration=_decl("same"))
    b2 = EnrichmentBinding(source=_ControlledSource(), declaration=_decl("same"))
    with pytest.raises(CompositeSourceError, match="duplicates"):
        CompositeSource(enrichments=[b1, b2])


def test_durable_enrichment_rejected() -> None:
    binding = EnrichmentBinding(
        source=_ControlledSource(),
        declaration=ChannelDeclaration(id="transcript", kind=ChannelKind.TRANSCRIPT),
    )
    with pytest.raises(CompositeSourceError, match="must not be a durable channel"):
        CompositeSource(enrichments=[binding])


def test_no_primary_no_enrichments_valid() -> None:
    composite = CompositeSource()
    assert composite is not None


# --- enrichment timeout validation -------------------------------------------


@pytest.mark.parametrize("bad_timeout", [0, -1, float("nan"), float("inf"), "x", None])
def test_invalid_enrichment_timeout_rejected(bad_timeout: object) -> None:
    with pytest.raises(CompositeSourceError, match="enrichment_timeout"):
        CompositeSource(enrichment_timeout=bad_timeout)  # type: ignore[arg-type]


# --- signal ownership validation --------------------------------------------


def test_enrichment_primary_ownership_rejected() -> None:
    cap = ChannelCapability(SignalKind.CONTENT, SignalOwnership.PRIMARY)
    binding = EnrichmentBinding(
        source=_ControlledSource(),
        declaration=_decl("hook", capabilities=(cap,)),
    )
    with pytest.raises(CompositeSourceError, match="primary ownership"):
        CompositeSource(enrichments=[binding])


def test_enrichment_primary_ownership_message_names_index_channel_signal() -> None:
    cap = ChannelCapability(SignalKind.TIMING, SignalOwnership.PRIMARY)
    binding = EnrichmentBinding(
        source=_ControlledSource(),
        declaration=_decl("otel", capabilities=(cap,)),
    )
    with pytest.raises(CompositeSourceError) as exc:
        CompositeSource(enrichments=[binding])
    msg = str(exc.value)
    assert "enrichments[0]" in msg
    assert "otel" in msg
    assert "timing" in msg


def test_duplicate_fallback_for_same_signal_rejected() -> None:
    cap_a = ChannelCapability(SignalKind.CONTENT, SignalOwnership.FALLBACK)
    cap_b = ChannelCapability(SignalKind.CONTENT, SignalOwnership.FALLBACK)
    a = EnrichmentBinding(
        source=_ControlledSource(),
        declaration=_decl("hook", capabilities=(cap_a,)),
    )
    b = EnrichmentBinding(
        source=_ControlledSource(),
        declaration=_decl("otel", capabilities=(cap_b,)),
    )
    with pytest.raises(CompositeSourceError, match="ambiguous fallback"):
        CompositeSource(enrichments=[a, b])


def test_duplicate_fallback_message_names_both_channels_and_signal() -> None:
    cap_a = ChannelCapability(SignalKind.CONTENT, SignalOwnership.FALLBACK)
    cap_b = ChannelCapability(SignalKind.CONTENT, SignalOwnership.FALLBACK)
    a = EnrichmentBinding(
        source=_ControlledSource(),
        declaration=_decl("hook", capabilities=(cap_a,)),
    )
    b = EnrichmentBinding(
        source=_ControlledSource(),
        declaration=_decl("otel", capabilities=(cap_b,)),
    )
    with pytest.raises(CompositeSourceError) as exc:
        CompositeSource(enrichments=[a, b])
    msg = str(exc.value)
    assert "hook" in msg
    assert "otel" in msg
    assert "content" in msg


def test_single_fallback_accepted() -> None:
    cap = ChannelCapability(SignalKind.CONTENT, SignalOwnership.FALLBACK)
    binding = EnrichmentBinding(
        source=_ControlledSource(),
        declaration=_decl("hook", capabilities=(cap,)),
    )
    composite = CompositeSource(enrichments=[binding])
    assert composite is not None


def test_overlapping_enrichment_ownership_accepted() -> None:
    cap_a = ChannelCapability(SignalKind.CONTENT, SignalOwnership.ENRICHMENT)
    cap_b = ChannelCapability(SignalKind.CONTENT, SignalOwnership.ENRICHMENT)
    a = EnrichmentBinding(
        source=_ControlledSource(),
        declaration=_decl("hook", capabilities=(cap_a,)),
    )
    b = EnrichmentBinding(
        source=_ControlledSource(),
        declaration=_decl("otel", capabilities=(cap_b,)),
    )
    composite = CompositeSource(enrichments=[a, b])
    assert composite is not None


def test_mixed_enrichment_and_single_fallback_accepted() -> None:
    cap_a = ChannelCapability(SignalKind.CONTENT, SignalOwnership.ENRICHMENT)
    cap_b = ChannelCapability(SignalKind.CONTENT, SignalOwnership.FALLBACK)
    a = EnrichmentBinding(
        source=_ControlledSource(),
        declaration=_decl("hook", capabilities=(cap_a,)),
    )
    b = EnrichmentBinding(
        source=_ControlledSource(),
        declaration=_decl("otel", capabilities=(cap_b,)),
    )
    composite = CompositeSource(enrichments=[a, b])
    assert composite is not None


# --- inverted completion order ----------------------------------------------


@pytest.mark.asyncio
async def test_enrichments_merge_in_declaration_order_not_completion_order() -> None:
    fact_a = _fact("a", 0, TrajectoryKind.TOOL_CALL)
    fact_b = _fact("b", 0, TrajectoryKind.TOOL_RESULT)
    primary = _ControlledSource()
    slow = _DelayedSource(Batch(trajectory=[fact_a]), delay=0.05)
    fast = _DelayedSource(Batch(trajectory=[fact_b]), delay=0.0)
    composite = CompositeSource(
        primary=primary,
        enrichments=[
            EnrichmentBinding(source=slow, declaration=_decl("slow")),
            EnrichmentBinding(source=fast, declaration=_decl("fast")),
        ],
    )
    batch = await composite.read()
    assert batch.trajectory[0].native_id == "a"
    assert batch.trajectory[1].native_id == "b"
    await composite.aclose()


# --- exact primary control preservation -------------------------------------


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
    primary = _ControlledSource()
    primary.set_batch(primary_batch)
    enrich = _ControlledSource()
    enrich.set_batch(Batch(trajectory=[_fact("e", 0)]))
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=enrich, declaration=_decl())],
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
    primary = _ControlledSource()
    primary.set_batch(Batch(events=[event]))
    composite = CompositeSource(primary=primary)
    batch = await composite.read()
    assert tuple(batch.events) == (event,)
    await composite.aclose()


@pytest.mark.asyncio
async def test_no_false_progressed_from_enrichment() -> None:
    primary = _ControlledSource()
    primary.set_batch(Batch(progressed=False))
    enrich = _ControlledSource()
    enrich.set_batch(Batch(trajectory=[_fact("e", 0)]))
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=enrich, declaration=_decl())],
    )
    batch = await composite.read()
    assert batch.progressed is False
    await composite.aclose()


@pytest.mark.asyncio
async def test_no_false_status_from_enrichment() -> None:
    primary = _ControlledSource()
    primary.set_batch(Batch(status=Status.IDLE))
    enrich = _ControlledSource()
    enrich.set_batch(Batch(status=Status.WORKING))
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=enrich, declaration=_decl())],
    )
    batch = await composite.read()
    assert batch.status is Status.IDLE
    await composite.aclose()


@pytest.mark.asyncio
async def test_no_false_waiting_from_enrichment() -> None:
    primary = _ControlledSource()
    primary.set_batch(Batch(waiting=False))
    enrich = _ControlledSource()
    enrich.set_batch(Batch(waiting=True))
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=enrich, declaration=_decl())],
    )
    batch = await composite.read()
    assert batch.waiting is False
    await composite.aclose()


@pytest.mark.asyncio
async def test_no_false_attachment_from_enrichment() -> None:
    primary = _ControlledSource()
    primary.set_batch(Batch())
    enrich = _ControlledSource()
    enrich.set_batch(Batch(attached=Attachment(location="/work/enrich.jsonl")))
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=enrich, declaration=_decl())],
    )
    batch = await composite.read()
    assert batch.attached is None
    await composite.aclose()


# --- primary trajectory passthrough ----------------------------------------


@pytest.mark.asyncio
async def test_primary_trajectory_passes_through_unchanged_across_repeated_polls() -> None:
    fact = _fact("p-fact", 0)
    primary = _ControlledSource()
    primary.set_batch(Batch(trajectory=[fact]))
    composite = CompositeSource(primary=primary)
    batch1 = await composite.read()
    batch2 = await composite.read()
    assert tuple(batch1.trajectory) == (fact,)
    assert tuple(batch2.trajectory) == (fact,)
    await composite.aclose()


@pytest.mark.asyncio
async def test_primary_trajectory_with_none_native_id_passes_through() -> None:
    fact = _fact(native_id=None, revision=0)
    primary = _ControlledSource()
    primary.set_batch(Batch(trajectory=[fact]))
    composite = CompositeSource(primary=primary)
    batch = await composite.read()
    assert tuple(batch.trajectory) == (fact,)
    await composite.aclose()


# --- enrichment control rejection / isolation --------------------------------


@pytest.mark.asyncio
async def test_enrichment_exception_does_not_fail_primary() -> None:
    primary = _ControlledSource()
    primary.set_batch(Batch(progressed=True))
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=_BoomSource(), declaration=_decl("boom"))],
    )
    batch = await composite.read()
    assert batch.progressed is True
    assert batch.trajectory == ()
    health = composite.channel_health()
    assert health[0].state is ChannelHealthState.FAILED
    await composite.aclose()


@pytest.mark.asyncio
async def test_enrichment_timeout_does_not_fail_primary() -> None:
    primary = _ControlledSource()
    primary.set_batch(Batch(progressed=True))
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=_SlowSource(delay=10.0), declaration=_decl("slow"))],
        enrichment_timeout=0.01,
    )
    batch = await asyncio.wait_for(composite.read(), timeout=5.0)
    assert batch.progressed is True
    health = composite.channel_health()
    assert health[0].state is ChannelHealthState.DEGRADED
    await composite.aclose()


@pytest.mark.asyncio
async def test_enrichment_malformed_return_does_not_fail_primary() -> None:
    primary = _ControlledSource()
    primary.set_batch(Batch(progressed=True))
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=_BadReturnTypeSource(), declaration=_decl("bad"))],
    )
    batch = await composite.read()
    assert batch.progressed is True
    health = composite.channel_health()
    assert health[0].state is ChannelHealthState.FAILED
    await composite.aclose()


@pytest.mark.asyncio
async def test_enrichment_error_batch_updates_only_that_health() -> None:
    primary = _ControlledSource()
    primary.set_batch(Batch(progressed=True))
    ok_source = _ControlledSource()
    ok_source.set_batch(Batch(trajectory=[_fact("ok", 0)]))
    composite = CompositeSource(
        primary=primary,
        enrichments=[
            EnrichmentBinding(source=ok_source, declaration=_decl("ok")),
            EnrichmentBinding(source=_ErrorBatchSource(), declaration=_decl("err")),
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


# --- recovery ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrichment_recovers_to_healthy_on_later_valid_read() -> None:
    class FlakySource(Source):
        def __init__(self) -> None:
            self._count = 0

        async def read(self) -> Batch:
            self._count += 1
            if self._count == 1:
                raise RuntimeError("transient")
            return Batch(trajectory=[_fact("recovered", 0)])

    primary = _ControlledSource()
    primary.set_batch(Batch(progressed=True))
    flaky = FlakySource()
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=flaky, declaration=_decl("flaky"))],
    )
    await composite.read()
    assert composite.channel_health()[0].state is ChannelHealthState.FAILED
    batch2 = await composite.read()
    assert composite.channel_health()[0].state is ChannelHealthState.HEALTHY
    assert any(f.native_id == "recovered" for f in batch2.trajectory)
    await composite.aclose()


# --- cancellation cleanup ----------------------------------------------------


@pytest.mark.asyncio
async def test_cancellation_cancels_and_awaits_child_tasks() -> None:
    primary = _ControlledSource()
    primary.set_batch(Batch(progressed=True))
    slow = _SlowSource(delay=10.0)
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=slow, declaration=_decl("slow"))],
        enrichment_timeout=10.0,
    )
    read_task = asyncio.create_task(composite.read())
    await asyncio.sleep(0.05)
    read_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await read_task
    await composite.aclose()


# --- bounded dedupe / eviction -----------------------------------------------


@pytest.mark.asyncio
async def test_dedup_across_polls_same_native_id_and_revision() -> None:
    fact = _fact("dup", 0)
    primary = _ControlledSource()
    enrich = _ControlledSource()
    enrich.set_batch(Batch(trajectory=[fact]))
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=enrich, declaration=_decl())],
    )
    batch1 = await composite.read()
    assert len(batch1.trajectory) == 1
    batch2 = await composite.read()
    assert len(batch2.trajectory) == 0
    await composite.aclose()


@pytest.mark.asyncio
async def test_dedup_different_revisions_both_pass() -> None:
    fact_v0 = _fact("item", 0)
    fact_v1 = _fact("item", 1)
    primary = _ControlledSource()
    enrich = _ControlledSource()
    enrich.set_batch(Batch(trajectory=[fact_v0, fact_v1]))
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=enrich, declaration=_decl())],
    )
    batch = await composite.read()
    assert len(batch.trajectory) == 2
    await composite.aclose()


@pytest.mark.asyncio
async def test_dedup_across_children_same_channel_and_native_id() -> None:
    """Same (channel_id, native_id, revision) from the same channel dedupes."""
    fact = _fact("shared", 0)
    primary = _ControlledSource()
    enrich = _ControlledSource()
    enrich.set_batch(Batch(trajectory=[fact]))
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=enrich, declaration=_decl("ch"))],
    )
    batch1 = await composite.read()
    batch2 = await composite.read()
    assert len(batch1.trajectory) == 1
    assert len(batch2.trajectory) == 0
    await composite.aclose()


@pytest.mark.asyncio
async def test_dedup_different_channels_same_native_id_both_pass() -> None:
    """Different channel IDs produce different dedup keys even with same native_id."""
    fact = _fact("shared", 0)
    primary = _ControlledSource()
    enrich_a = _ControlledSource()
    enrich_a.set_batch(Batch(trajectory=[fact]))
    enrich_b = _ControlledSource()
    enrich_b.set_batch(Batch(trajectory=[fact]))
    composite = CompositeSource(
        primary=primary,
        enrichments=[
            EnrichmentBinding(source=enrich_a, declaration=_decl("a")),
            EnrichmentBinding(source=enrich_b, declaration=_decl("b")),
        ],
    )
    batch = await composite.read()
    assert len(batch.trajectory) == 2
    await composite.aclose()


@pytest.mark.asyncio
async def test_native_id_none_always_passes_through() -> None:
    fact_none = _fact(native_id=None, revision=0)
    primary = _ControlledSource()
    enrich = _ControlledSource()
    enrich.set_batch(Batch(trajectory=[fact_none, fact_none]))
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=enrich, declaration=_decl())],
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
    f1 = _fact("a", 0)
    f2 = _fact("b", 0)
    f3 = _fact("c", 0)
    f4 = _fact("d", 0)
    result1 = cache.filter("ch", [f1, f2, f3])
    assert len(result1) == 3
    result2 = cache.filter("ch", [f1])
    assert len(result2) == 0
    result3 = cache.filter("ch", [f4])
    assert len(result3) == 1
    result4 = cache.filter("ch", [f1])
    assert len(result4) == 1


# --- no-primary enrichment read ---------------------------------------------


@pytest.mark.asyncio
async def test_no_primary_still_reads_enrichments() -> None:
    fact_a = _fact("a", 0)
    fact_b = _fact("b", 0)
    enrich_a = _ControlledSource()
    enrich_a.set_batch(Batch(trajectory=[fact_a]))
    enrich_b = _ControlledSource()
    enrich_b.set_batch(Batch(trajectory=[fact_b]))
    composite = CompositeSource(
        enrichments=[
            EnrichmentBinding(source=enrich_a, declaration=_decl("a")),
            EnrichmentBinding(source=enrich_b, declaration=_decl("b")),
        ],
    )
    batch = await composite.read()
    assert batch.progressed is False
    assert batch.status is None
    assert len(batch.trajectory) == 2
    assert batch.trajectory[0].native_id == "a"
    assert batch.trajectory[1].native_id == "b"
    await composite.aclose()


@pytest.mark.asyncio
async def test_no_primary_read_with_no_enrichments_returns_empty() -> None:
    composite = CompositeSource()
    batch = await composite.read()
    assert batch == Batch()
    await composite.aclose()


# --- primary-only delegation -------------------------------------------------


@pytest.mark.asyncio
async def test_no_primary_delegates_to_source_defaults() -> None:
    composite = CompositeSource()
    batch = await composite.read()
    assert batch.trajectory == ()
    refresh = await composite.refresh()
    assert refresh == Batch()
    identity = await composite.probe_identity_loss()
    assert identity is None
    history = await composite.history(last_n=10)
    assert history == History()
    page = await composite.history_page()
    assert page == HistoryPage()


@pytest.mark.asyncio
async def test_no_primary_refuses_attachment_operations() -> None:
    composite = CompositeSource()
    with pytest.raises(SourceContractError):
        composite.commit_attachment()
    with pytest.raises(SourceContractError):
        composite.discard_attachment()
    with pytest.raises(SourceContractError):
        composite.revoke_attachment()
    with pytest.raises(SourceContractError):
        composite.admit_exact_location(location="x", session_id="s")


@pytest.mark.asyncio
async def test_no_primary_refuses_history_paging() -> None:
    composite = CompositeSource()
    page = await composite.history_page(before="cursor")
    assert page.error_code == "history_paging_unavailable"


@pytest.mark.asyncio
async def test_no_primary_collision_domain_none() -> None:
    composite = CompositeSource()
    assert composite.collision_domain is None


# --- delegation to primary ---------------------------------------------------


@pytest.mark.asyncio
async def test_commit_attachment_delegates_to_primary() -> None:
    primary = _PrimaryWithAttachment()
    composite = CompositeSource(primary=primary)
    batch = await composite.read()
    assert batch.attached is not None
    composite.commit_attachment()
    assert primary.committed is True
    await composite.aclose()


@pytest.mark.asyncio
async def test_discard_attachment_delegates_to_primary() -> None:
    primary = _PrimaryWithAttachment()
    composite = CompositeSource(primary=primary)
    composite.discard_attachment()
    assert primary.discarded is True
    await composite.aclose()


@pytest.mark.asyncio
async def test_revoke_attachment_delegates_to_primary() -> None:
    primary = _PrimaryWithAttachment()
    composite = CompositeSource(primary=primary)
    composite.revoke_attachment()
    assert primary.revoked is True
    await composite.aclose()


@pytest.mark.asyncio
async def test_admit_exact_location_delegates_to_primary() -> None:
    primary = _PrimaryWithAttachment()
    composite = CompositeSource(primary=primary)
    result = composite.admit_exact_location(location="/work/t.jsonl", session_id="s1")
    assert result == "accepted"
    assert primary.admitted == ("/work/t.jsonl", "s1")
    await composite.aclose()


@pytest.mark.asyncio
async def test_history_delegates_to_primary() -> None:
    primary = _PrimaryWithAttachment()
    composite = CompositeSource(primary=primary)
    history = await composite.history(last_n=10)
    assert history.location == "/work/t.jsonl"
    await composite.aclose()


@pytest.mark.asyncio
async def test_history_page_delegates_to_primary() -> None:
    primary = _PrimaryWithAttachment()
    composite = CompositeSource(primary=primary)
    page = await composite.history_page(limit=10)
    assert page.location == "/work/t.jsonl"
    await composite.aclose()


@pytest.mark.asyncio
async def test_history_page_default_limit_matches_constant() -> None:
    import inspect

    from theater.harness.channels.composite import CompositeSource

    sig = inspect.signature(CompositeSource.history_page)
    assert sig.parameters["limit"].default == TRAJECTORY_PAGE_RECORD_LIMIT


@pytest.mark.asyncio
async def test_probe_identity_loss_delegates_to_primary() -> None:
    primary = _PrimaryWithIdentityLoss()
    composite = CompositeSource(primary=primary)
    evidence = await composite.probe_identity_loss()
    assert evidence is not None
    assert evidence.location == "/work/other.jsonl"
    await composite.aclose()


@pytest.mark.asyncio
async def test_collision_domain_from_primary() -> None:
    primary = _PrimaryWithAttachment()
    composite = CompositeSource(primary=primary)
    assert composite.collision_domain == "domain"
    await composite.aclose()


@pytest.mark.asyncio
async def test_collision_domain_reflects_primary_updates() -> None:
    class _MutableDomain(Source):
        collision_domain: str | None = None

        async def read(self) -> Batch:
            return Batch()

    primary = _MutableDomain()
    composite = CompositeSource(primary=primary)
    assert composite.collision_domain is None
    primary.collision_domain = "updated"
    assert composite.collision_domain == "updated"
    await composite.aclose()


# --- primary health ---------------------------------------------------------


@pytest.mark.asyncio
async def test_primary_health_starts_as_starting() -> None:
    primary = _ControlledSource()
    composite = CompositeSource(primary=primary)
    assert composite.primary_health() is not None
    assert composite.primary_health().state is ChannelHealthState.STARTING


@pytest.mark.asyncio
async def test_primary_health_transitions_to_healthy_after_clean_read() -> None:
    primary = _ControlledSource()
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


# --- enrichment health transitions and bounds --------------------------------


@pytest.mark.asyncio
async def test_health_starts_as_starting() -> None:
    composite = CompositeSource(
        enrichments=[EnrichmentBinding(source=_ControlledSource(), declaration=_decl("h"))]
    )
    health = composite.channel_health()
    assert health[0].state is ChannelHealthState.STARTING


@pytest.mark.asyncio
async def test_health_transitions_to_healthy_after_read() -> None:
    primary = _ControlledSource()
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=_ControlledSource(), declaration=_decl("h"))],
    )
    await composite.read()
    assert composite.channel_health()[0].state is ChannelHealthState.HEALTHY


@pytest.mark.asyncio
async def test_health_transitions_to_degraded_on_timeout() -> None:
    primary = _ControlledSource()
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=_SlowSource(), declaration=_decl("h"))],
        enrichment_timeout=0.01,
    )
    await asyncio.wait_for(composite.read(), timeout=5.0)
    assert composite.channel_health()[0].state is ChannelHealthState.DEGRADED


@pytest.mark.asyncio
async def test_health_transitions_to_failed_on_exception() -> None:
    primary = _ControlledSource()
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=_BoomSource(), declaration=_decl("h"))],
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

    primary = _ControlledSource()
    composite = CompositeSource(
        primary=primary,
        enrichments=[EnrichmentBinding(source=FlakySource(), declaration=_decl("h"))],
    )
    await composite.read()
    assert composite.channel_health()[0].state is ChannelHealthState.FAILED
    await composite.read()
    assert composite.channel_health()[0].state is ChannelHealthState.HEALTHY


@pytest.mark.asyncio
async def test_health_in_declaration_order() -> None:
    primary = _ControlledSource()
    composite = CompositeSource(
        primary=primary,
        enrichments=[
            EnrichmentBinding(source=_ControlledSource(), declaration=_decl("alpha")),
            EnrichmentBinding(source=_BoomSource(), declaration=_decl("beta")),
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
        enrichments=[EnrichmentBinding(source=BrokenHealthSource(), declaration=_decl("h"))]
    )

    (health,) = composite.channel_health()

    assert health.state is ChannelHealthState.DEGRADED
    assert health.diagnostics == ("channel health snapshot failed (ValueError)",)
    assert "secret health value" not in str(health)


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


# --- idempotent close / first error -----------------------------------------


@pytest.mark.asyncio
async def test_aclose_is_idempotent() -> None:
    source = _RecordingSource()
    composite = CompositeSource(enrichments=[EnrichmentBinding(source=source, declaration=_decl())])
    await composite.aclose()
    await composite.aclose()
    assert source.close_count == 1


@pytest.mark.asyncio
async def test_aclose_attempts_every_source_exactly_once() -> None:
    s1 = _RecordingSource()
    s2 = _RecordingSource()
    composite = CompositeSource(
        enrichments=[
            EnrichmentBinding(source=s1, declaration=_decl("a")),
            EnrichmentBinding(source=s2, declaration=_decl("b")),
        ]
    )
    await composite.aclose()
    assert s1.close_count == 1
    assert s2.close_count == 1


@pytest.mark.asyncio
async def test_aclose_raises_first_real_error_in_declaration_order() -> None:
    class CloseErrorSource(Source):
        def __init__(self, msg: str) -> None:
            self._msg = msg

        async def read(self) -> Batch:
            return Batch()

        async def aclose(self) -> None:
            raise RuntimeError(self._msg)

    err1 = CloseErrorSource("err1")
    err2 = CloseErrorSource("err2")
    composite = CompositeSource(
        enrichments=[
            EnrichmentBinding(source=err1, declaration=_decl("first")),
            EnrichmentBinding(source=err2, declaration=_decl("second")),
        ]
    )
    with pytest.raises(RuntimeError, match="err1"):
        await composite.aclose()


@pytest.mark.asyncio
async def test_aclose_preserves_cancellation() -> None:
    class CancelMeSource(Source):
        async def read(self) -> Batch:
            return Batch()

        async def aclose(self) -> None:
            await asyncio.sleep(10.0)

    composite = CompositeSource(
        enrichments=[EnrichmentBinding(source=CancelMeSource(), declaration=_decl())]
    )
    close_task = asyncio.create_task(composite.aclose())
    await asyncio.sleep(0.05)
    close_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close_task


@pytest.mark.asyncio
async def test_aclose_propagates_child_cancelled_error() -> None:
    composite = CompositeSource(
        enrichments=[EnrichmentBinding(source=_CancelCloseSource(), declaration=_decl("c"))]
    )
    with pytest.raises(asyncio.CancelledError):
        await composite.aclose()


# --- import boundaries ------------------------------------------------------


def test_composite_modules_do_not_import_runtime_layers() -> None:
    root = Path(__file__).parents[1]
    files = (
        root / "theater/harness/channels/__init__.py",
        root / "theater/harness/channels/composite.py",
        root / "theater/harness/channels/health.py",
    )
    forbidden = (
        "theater.daemon",
        "theater.regie",
        "theater.tmux",
        "theater.harness.builtin",
    )
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        imported.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert not any(name.startswith(forbidden) for name in imported), path


# --- enrichment timeout default is named constant ---------------------------


def test_enrichment_timeout_default_is_named_constant() -> None:
    composite = CompositeSource()
    assert composite._enrichment_timeout == HARNESS_ENRICHMENT_READ_TIMEOUT_SECONDS


def test_dedupe_max_is_named_constant() -> None:
    from theater.harness.channels.composite import _DedupCache

    cache = _DedupCache()
    assert cache._max == HARNESS_DEDUPE_MAX_FACTS
