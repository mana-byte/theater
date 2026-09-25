"""Source composition and channel health: open, read, and validate one participant's source."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from theater.daemon.observation.attachment import AttachmentManager
from theater.daemon.observation.failures import FailureTracker
from theater.daemon.observation.live import LiveObservationHub, LiveRegistration
from theater.daemon.observation.process import observation_process_id
from theater.harness import HarnessObserver
from theater.harness.channels.composite import CompositeSource, EnrichmentBinding
from theater.harness.channels.health import (
    ChannelHealthTracker,
    read_error_diagnostic,
    read_exception_diagnostic,
)
from theater.harness.channels.hooks import HookRuntime
from theater.harness.channels.hybrid import HybridSource
from theater.harness.channels.otel import NativeOtelRuntime
from theater.harness.contracts.channels import ChannelHealth
from theater.harness.source import Batch, Source, SourceContractError
from theater.models import Tier
from theater.provenance import normalize_provenance

if TYPE_CHECKING:
    from theater.daemon.store import Store

logger = logging.getLogger("theater.observer")


class SourceChannels:
    if TYPE_CHECKING:
        _attachments: AttachmentManager
        _channel_health: dict[str, tuple[ChannelHealth, ...]]
        _failures: FailureTracker
        hook_runtime: HookRuntime | None
        live: LiveObservationHub
        _open_participant_source: Callable[..., Source]
        otel_runtime: NativeOtelRuntime | None
        _pending_transcripts: set[str]
        _primary_channel_health: dict[tuple[str, str], ChannelHealthTracker]
        store: Store

    def channel_health_snapshot(self, participant_id: str) -> tuple[ChannelHealth, ...]:
        return self._channel_health.get(participant_id, ())

    def _open_source(self, pid: str, observer: HarnessObserver) -> Source | None:
        return self._open_source_for_registration(pid, observer, self.live.registration_for(pid))

    def _open_source_for_registration(  # noqa: PLR0912
        self,
        pid: str,
        observer: HarnessObserver,
        registration: LiveRegistration | None,
    ) -> Source | None:
        """Compose a source against one immutable live-registration snapshot."""
        p = self.store.get_participant(pid)
        if p is None:
            return None
        source: Source | None = None
        if observer.has_transcript and p.cwd is not None:
            after = p.created_at if p.tier is Tier.SPAWNED else None
            source = self._open_participant_source(
                observer,
                participant_id=p.id,
                cwd=p.cwd,
                session_id=p.session_id,
                after=after,
                session_provenance=normalize_provenance(p.session_correlation),
                known_location=p.transcript_location,
                transcript_domain=p.transcript_domain,
                source_checkpoint=p.source_checkpoint,
                pane_pid=observation_process_id(self.store, p),
            )
        bindings: tuple[EnrichmentBinding, ...] = ()
        if self.hook_runtime is not None:
            bindings = self.hook_runtime.enrichment_bindings(p.id, observer.enrichment_manifests())
        otel_bindings: tuple[EnrichmentBinding, ...] = ()
        if self.otel_runtime is not None:
            otel_bindings = self.otel_runtime.enrichment_bindings(
                p.id,
                p.harness,
                observer.enrichment_manifests(),
            )
        bindings = bindings + otel_bindings
        primary_method = getattr(observer, "primary_channel_declaration", None)
        primary = primary_method() if callable(primary_method) else None
        primary_tracker: ChannelHealthTracker | None = None
        if source is None and registration is None and not bindings:
            self._clear_primary_channel_health(pid)
            return None
        composed_hybrid = False
        if registration is not None:
            if source is None:
                # Live-only wiring: the runtime's live source carries status
                # and exact evidence without a durable reader.
                source = registration.live_source
            else:
                # Native wiring: the durable reader keeps attachment, identity, floors, history;
                # the live channel is authoritative for current turn, status and terminal evidence.
                composed_hybrid = True
                source = HybridSource(
                    durable=source,
                    live=registration.live_source,
                    live_channel=registration.channel,
                    durable_channel=primary,
                    durable_channel_id=primary.id if primary is not None else "primary",
                    wakeup=self.live.wake_signal(p.id),
                )
        if bindings:
            self._clear_primary_channel_health(pid)
            source = CompositeSource(
                primary=source,
                primary_channel_id=primary.id if primary is not None else "primary",
                enrichments=bindings,
            )
        elif source is not None and primary is not None and not composed_hybrid:
            primary_tracker = ChannelHealthTracker(primary.id)
            primary_tracker.mark_starting()
            self._clear_primary_channel_health(pid)
        else:
            self._clear_primary_channel_health(pid)
        if source is None:
            return None
        if source.collision_domain is not None and p.transcript_domain != source.collision_domain:
            p.transcript_domain = source.collision_domain
            self.store.upsert_participant(p)
        if primary_tracker is not None and primary is not None:
            self._primary_channel_health[(pid, primary.id)] = primary_tracker
        return source

    def _record_channel_health(self, participant_id: str, source: Source) -> None:
        health: tuple[ChannelHealth, ...] = ()
        try:
            snapshot = source.health_snapshot()
        except Exception:
            snapshot = ()
        if isinstance(snapshot, tuple) and all(
            isinstance(item, ChannelHealth) for item in snapshot
        ):
            health = snapshot
        primary = self._primary_health_tracker(participant_id)
        if primary is not None:
            primary_health = primary.snapshot()
            health = (
                primary_health,
                *(item for item in health if item.channel_id != primary_health.channel_id),
            )
        if health:
            self._channel_health[participant_id] = health
        else:
            self._channel_health.pop(participant_id, None)

    async def _read_source(self, participant_id: str, source: Source) -> Batch:
        try:
            batch = await source.read()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._pending_transcripts.discard(participant_id)
            self._record_primary_failure(participant_id, exc)
            raise
        else:
            self._record_primary_batch(participant_id, batch)
            return batch
        finally:
            self._record_channel_health(participant_id, source)

    def _record_primary_batch(self, participant_id: str, batch: Batch) -> None:
        tracker = self._primary_health_tracker(participant_id)
        if tracker is None:
            return
        if batch.error_code is not None:
            tracker.mark_degraded(read_error_diagnostic("primary", batch.error_code))
            return
        tracker.record_success()
        tracker.mark_healthy()

    def _record_primary_failure(self, participant_id: str, exc: BaseException) -> None:
        tracker = self._primary_health_tracker(participant_id)
        if tracker is not None:
            tracker.mark_failed(read_exception_diagnostic("primary read failed", exc))

    def _primary_health_tracker(self, participant_id: str) -> ChannelHealthTracker | None:
        return next(
            (
                tracker
                for (current_id, _channel_id), tracker in self._primary_channel_health.items()
                if current_id == participant_id
            ),
            None,
        )

    def _clear_primary_channel_health(self, participant_id: str) -> None:
        for key in tuple(self._primary_channel_health):
            if key[0] == participant_id:
                self._primary_channel_health.pop(key, None)

    def _register_source(self, pid: str, source: Source) -> None:
        self._attachments.register_source(
            pid,
            source,
            clear_source_errors_fn=self._failures.clear_source_errors,
        )

    @staticmethod
    def _validate_batch(source: Source, batch: Batch) -> None:
        if not (batch.waiting and batch.attached is not None):
            return
        source.discard_attachment()
        raise SourceContractError(
            f"{type(source).__name__} returned a batch that is both waiting and attached"
        )
