"""Primary-plus-bounded-enrichment composition preserving the Source seam."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from dataclasses import dataclass

from theater.constants.harness import (
    HARNESS_DEDUPE_MAX_FACTS,
    HARNESS_ENRICHMENT_READ_TIMEOUT_SECONDS,
)
from theater.constants.trajectory import TRAJECTORY_PAGE_RECORD_LIMIT
from theater.harness.channels.health import ChannelHealthTracker
from theater.harness.contracts.channels import (
    ChannelDeclaration,
    ChannelHealth,
    ChannelKind,
    SignalKind,
    SignalOwnership,
)
from theater.harness.contracts.source import (
    Batch,
    History,
    HistoryPage,
    IdentityLossEvidence,
    ReceiptAdmission,
    Source,
    SourceContractError,
)
from theater.harness.contracts.trajectory import TrajectoryFact

_DURABLE_KINDS = frozenset({ChannelKind.TRANSCRIPT, ChannelKind.DATABASE})
_DEFAULT_TIMEOUT = HARNESS_ENRICHMENT_READ_TIMEOUT_SECONDS
_DEDUPE_MAX = HARNESS_DEDUPE_MAX_FACTS
_PRIMARY_ID = "primary"


class CompositeSourceError(ValueError):
    """Construction-time validation failure."""


@dataclass(frozen=True, slots=True)
class EnrichmentBinding:
    """A typed binding between a Source and its validated ChannelDeclaration."""

    source: Source
    declaration: ChannelDeclaration

    def __post_init__(self) -> None:
        if not isinstance(self.source, Source):
            raise CompositeSourceError("enrichment source must implement Source")
        if not isinstance(self.declaration, ChannelDeclaration):
            raise CompositeSourceError("enrichment declaration must be ChannelDeclaration")


def _fact_dedupe_key(channel_id: str, fact: TrajectoryFact) -> tuple[str, str | None, int]:
    return (channel_id, fact.native_id, fact.revision)


class _DedupCache:
    """Bounded dedupe keyed by (channel_id, native_id, revision)."""

    def __init__(self, max_entries: int = _DEDUPE_MAX) -> None:
        self._max = max_entries
        self._seen: dict[tuple[str, str | None, int], None] = {}

    def filter(self, channel_id: str, facts: Sequence[TrajectoryFact]) -> list[TrajectoryFact]:
        result: list[TrajectoryFact] = []
        for fact in facts:
            if fact.native_id is None:
                result.append(fact)
                continue
            key = _fact_dedupe_key(channel_id, fact)
            if key in self._seen:
                continue
            self._seen[key] = None
            if len(self._seen) > self._max:
                self._seen.pop(next(iter(self._seen)))
            result.append(fact)
        return result


class CompositeSource(Source):
    """One optional primary source plus ordered bounded enrichments."""

    def __init__(
        self,
        *,
        primary: Source | None = None,
        enrichments: Sequence[EnrichmentBinding] = (),
        enrichment_timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._validate_timeout(enrichment_timeout)
        self._validate_construction(primary, enrichments)
        self._primary = primary
        self._enrichments = tuple(enrichments)
        self._enrichment_timeout = enrichment_timeout
        self._health: dict[str, ChannelHealthTracker] = {}
        if primary is not None:
            tracker = ChannelHealthTracker(_PRIMARY_ID)
            tracker.mark_starting()
            self._health[_PRIMARY_ID] = tracker
        for binding in self._enrichments:
            tracker = ChannelHealthTracker(binding.declaration.id)
            tracker.mark_starting()
            self._health[binding.declaration.id] = tracker
        self._dedupe = _DedupCache()
        self._closed = False

    @property
    def collision_domain(self) -> str | None:  # type: ignore[override]
        if self._primary is not None:
            return self._primary.collision_domain
        return None

    @staticmethod
    def _validate_timeout(timeout: float) -> None:
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise CompositeSourceError("enrichment_timeout must be a finite number > 0")

    @staticmethod
    def _validate_construction(
        primary: Source | None,
        enrichments: Sequence[EnrichmentBinding],
    ) -> None:
        if primary is not None and not isinstance(primary, Source):
            raise CompositeSourceError("primary must implement Source or be None")
        if not isinstance(enrichments, Sequence):
            raise CompositeSourceError("enrichments must be a sequence")
        bindings = tuple(enrichments)
        for i, binding in enumerate(bindings):
            if not isinstance(binding, EnrichmentBinding):
                raise CompositeSourceError(f"enrichments[{i}] must be EnrichmentBinding")
            if not isinstance(binding.source, Source):
                raise CompositeSourceError(f"enrichments[{i}].source must implement Source")
        ids: set[str] = set()
        if primary is not None:
            ids.add(_PRIMARY_ID)
        for i, binding in enumerate(bindings):
            bid = binding.declaration.id
            if bid in ids:
                raise CompositeSourceError(
                    f"enrichments[{i}].declaration.id duplicates channel id {bid!r}"
                )
            ids.add(bid)
            if binding.declaration.kind in _DURABLE_KINDS:
                raise CompositeSourceError(f"enrichments[{i}] must not be a durable channel")
        CompositeSource._validate_ownership(bindings)

    @staticmethod
    def _validate_ownership(bindings: tuple[EnrichmentBinding, ...]) -> None:
        fallbacks: dict[SignalKind, list[str]] = {}
        for i, binding in enumerate(bindings):
            for cap in binding.declaration.capabilities:
                if cap.ownership is SignalOwnership.PRIMARY:
                    raise CompositeSourceError(
                        f"enrichments[{i}].declaration id={binding.declaration.id!r} "
                        f"signal={cap.signal.value!r} must not declare primary ownership"
                    )
                if cap.ownership is SignalOwnership.FALLBACK:
                    fallbacks.setdefault(cap.signal, []).append(binding.declaration.id)
        for signal, channel_ids in fallbacks.items():
            if len(channel_ids) > 1:
                raise CompositeSourceError(
                    f"ambiguous fallback for signal {signal.value!r}: "
                    f"channels {channel_ids[0]!r} and {channel_ids[1]!r}"
                )

    def channel_health(self) -> tuple[ChannelHealth, ...]:
        return tuple(
            self._health[binding.declaration.id].snapshot() for binding in self._enrichments
        )

    def primary_health(self) -> ChannelHealth | None:
        if self._primary is None:
            return None
        return self._health[_PRIMARY_ID].snapshot()

    async def read(self) -> Batch:
        if self._primary is None:
            enrichment_facts = await self._read_enrichments()
            return Batch(
                trajectory=tuple(enrichment_facts) if enrichment_facts else (),
            )
        batch = await self._primary.read()
        tracker = self._health[_PRIMARY_ID]
        if batch.error_code is not None:
            tracker.mark_degraded(batch.error or batch.error_code)
        else:
            tracker.mark_healthy()
        enrichment_facts = await self._read_enrichments()
        all_facts = list(batch.trajectory)
        all_facts.extend(enrichment_facts)
        return Batch(
            events=batch.events,
            progressed=batch.progressed,
            status=batch.status,
            attached=batch.attached,
            waiting=batch.waiting,
            error_code=batch.error_code,
            error=batch.error,
            trajectory=tuple(all_facts) if all_facts else (),
            trajectory_events=batch.trajectory_events,
        )

    async def _read_enrichments(self) -> list[TrajectoryFact]:
        if not self._enrichments:
            return []
        loop = asyncio.get_running_loop()
        tasks: dict[asyncio.Task[Batch], EnrichmentBinding] = {}
        for binding in self._enrichments:
            task = loop.create_task(self._read_one_enrichment(binding))
            tasks[task] = binding
        results: dict[str, list[TrajectoryFact]] = {}
        try:
            done, _pending = await asyncio.wait(set(tasks), return_when=asyncio.ALL_COMPLETED)
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        for task in done:
            binding = tasks[task]
            if task.cancelled():
                raise asyncio.CancelledError()
            exc = task.exception()
            if exc is not None:
                self._handle_enrichment_failure(binding, exc)
                continue
            batch_result = task.result()
            facts = self._extract_enrichment_facts(binding, batch_result)
            results[binding.declaration.id] = facts
        ordered: list[TrajectoryFact] = []
        for binding in self._enrichments:
            facts = results.get(binding.declaration.id, [])
            ordered.extend(facts)
        return ordered

    async def _read_one_enrichment(self, binding: EnrichmentBinding) -> Batch:
        tracker = self._health[binding.declaration.id]
        try:
            batch = await asyncio.wait_for(binding.source.read(), timeout=self._enrichment_timeout)
        except TimeoutError:
            tracker.mark_degraded(f"enrichment read timeout after {self._enrichment_timeout}s")
            return Batch(error_code="enrichment_timeout", error="enrichment read timed out")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            tracker.mark_failed(str(exc) or type(exc).__name__)
            return Batch(error_code="enrichment_error", error=str(exc) or type(exc).__name__)
        if not isinstance(batch, Batch):
            tracker.mark_failed(f"enrichment returned {type(batch).__name__}, not Batch")
            return Batch(error_code="enrichment_malformed", error="enrichment returned non-Batch")
        if batch.error_code is not None:
            tracker.mark_degraded(batch.error or batch.error_code)
            return batch
        tracker.mark_healthy()
        return batch

    def _extract_enrichment_facts(
        self, binding: EnrichmentBinding, batch: Batch
    ) -> list[TrajectoryFact]:
        if not batch.trajectory:
            return []
        return self._dedupe.filter(binding.declaration.id, batch.trajectory)

    def _handle_enrichment_failure(self, binding: EnrichmentBinding, exc: BaseException) -> None:
        tracker = self._health[binding.declaration.id]
        tracker.mark_failed(str(exc) or type(exc).__name__)

    async def refresh(self) -> Batch:
        if self._primary is None:
            return Batch()
        return await self._primary.refresh()

    async def probe_identity_loss(self) -> IdentityLossEvidence | None:
        if self._primary is None:
            return None
        return await self._primary.probe_identity_loss()

    def commit_attachment(self) -> None:
        if self._primary is None:
            raise _source_contract_error("commit_attachment")
        self._primary.commit_attachment()

    def discard_attachment(self) -> None:
        if self._primary is None:
            raise _source_contract_error("discard_attachment")
        self._primary.discard_attachment()

    def revoke_attachment(self) -> None:
        if self._primary is None:
            raise _source_contract_error("revoke_attachment")
        self._primary.revoke_attachment()

    def admit_exact_location(self, *, location: str, session_id: str) -> ReceiptAdmission:
        if self._primary is None:
            raise _source_contract_error("admit_exact_location")
        return self._primary.admit_exact_location(location=location, session_id=session_id)

    async def history(self, *, last_n: int) -> History:
        if self._primary is None:
            return History()
        return await self._primary.history(last_n=last_n)

    async def history_page(
        self,
        *,
        before: str | None = None,
        limit: int = TRAJECTORY_PAGE_RECORD_LIMIT,
    ) -> HistoryPage:
        if self._primary is None:
            if before is not None:
                return HistoryPage(
                    error_code="history_paging_unavailable",
                    error="this source cannot page older history",
                )
            return HistoryPage()
        return await self._primary.history_page(before=before, limit=limit)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        all_sources: list[Source] = [b.source for b in self._enrichments]
        if self._primary is not None:
            all_sources.append(self._primary)
        results = await asyncio.gather(*(s.aclose() for s in all_sources), return_exceptions=True)
        first_error: BaseException | None = None
        first_cancel: BaseException | None = None
        for result in results:
            if isinstance(result, BaseException):
                if isinstance(result, asyncio.CancelledError):
                    if first_cancel is None:
                        first_cancel = result
                elif first_error is None:
                    first_error = result
        if first_error is not None:
            raise first_error
        if first_cancel is not None:
            raise first_cancel


def _source_contract_error(method: str) -> SourceContractError:
    return SourceContractError(f"CompositeSource has no primary and cannot delegate {method}()")


__all__ = ["CompositeSource", "CompositeSourceError", "EnrichmentBinding"]
