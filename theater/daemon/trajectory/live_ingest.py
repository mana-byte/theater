"""Live transcript-batch ingestion for warm trajectory streams."""

from __future__ import annotations

from theater.daemon.trajectory.history import source_epoch_for
from theater.daemon.trajectory.mutations import TrajectoryMutationHooks
from theater.daemon.trajectory.observed_timing import apply_live_observation
from theater.daemon.trajectory.project import project_batch
from theater.daemon.trajectory.stream import CapturedBatch, TrajectoryStream
from theater.provenance import is_trusted_provenance
from theater.trajectory import PanelState
from theater.transcript_identity import (
    TRANSCRIPT_IDENTITY_LOST_CODE,
    transcript_identity_recovery_message,
)


def apply_live(
    stream: TrajectoryStream,
    captured: CapturedBatch,
    *,
    notify: bool,
    hooks: TrajectoryMutationHooks,
) -> None:
    batch = captured.batch
    attachment = batch.attached
    if attachment is not None:
        if not is_trusted_provenance(attachment.correlation):
            stream.live_allowed = False
            gap_added = hooks.add_gap(
                stream, "transcript", "live transcript attachment is untrusted"
            )
            panel_changed = hooks.set_panel(
                stream,
                PanelState.UNTRUSTED,
                "live transcript identity is untrusted",
                notify=notify,
            )
            if gap_added and notify and not panel_changed:
                hooks.wake_followers(stream)
            return
        stream.live_allowed = True
        epoch = source_epoch_for(stream.participant, attachment.location)
        if stream.source_epoch is not None and stream.source_epoch != epoch:
            hooks.add_boundary(stream, stream.source_epoch, epoch)
            hooks.add_gap(
                stream, "transcript", "transcript session rotated", stream.source_epoch, epoch
            )
        stream.source_epoch = epoch
    if not stream.live_allowed:
        return
    if batch.error_code is not None:
        reason = batch.error or batch.error_code
        gap_added = hooks.add_gap(stream, "transcript", reason)
        if batch.error_code == TRANSCRIPT_IDENTITY_LOST_CODE:
            panel_changed = hooks.set_panel(
                stream,
                PanelState.UNTRUSTED,
                transcript_identity_recovery_message(stream.participant.id, reason),
                notify=notify,
            )
        else:
            panel_changed = hooks.set_panel(
                stream,
                PanelState.STALE,
                f"live trajectory read failed: {reason}; request a fresh snapshot to retry",
                notify=notify,
            )
        if gap_added and notify and not panel_changed:
            hooks.wake_followers(stream)
        return
    epoch = stream.source_epoch or source_epoch_for(stream.participant, None)
    records = project_batch(batch, participant_id=stream.participant.id, source_epoch=epoch)
    previous = {
        record.record_id: existing
        for record in records
        if (existing := stream.ring.get(record.record_id)) is not None
    }
    records = apply_live_observation(records, captured.observed_at, previous)
    changes = hooks.merge_records(stream, records, notify=notify)
    if changes:
        stream.live_updates_observed = True
    if records and stream.panel_state.state in {
        PanelState.WAITING,
        PanelState.UNAVAILABLE,
        PanelState.STALE,
        PanelState.UNTRUSTED,
    }:
        hooks.set_panel(
            stream,
            PanelState.READY,
            "live transcript records are available",
            notify=False,
        )


__all__ = ["apply_live"]
