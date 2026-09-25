"""Live source for the Codex native runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from theater.harness.contracts.channels import ChannelHealth, ChannelHealthState
from theater.harness.contracts.events import Event
from theater.harness.contracts.runtime import ConnectionHealth, NativeTurnOutcome
from theater.harness.contracts.source import Batch, Source
from theater.models import Status
from theater.trajectory.content import ContentPreview
from theater.trajectory.enums import TrajectoryKind, TrajectoryLane, TrajectoryStatus

from ._runtime_host import CodexRuntimeHost
from .runtime_constants import _LIVE_CHANNEL_ID, CODEX_RUNTIME_EVENTS_PER_BATCH
from .runtime_messages import _fact


class CodexLiveSource(Source):
    """The single live ``Source`` of one Codex runtime."""

    def __init__(self, runtime: CodexRuntimeHost) -> None:
        self._runtime = runtime
        self._last_status: Status | None = None

    def set_activity_callback(self, callback: Callable[[], None] | None) -> None:
        """Forward the optional arrival-driven wake hook to the runtime."""
        self._runtime.set_activity_callback(callback)

    def buffered_terminal_evidence(self) -> tuple[NativeTurnOutcome, ...]:
        return tuple(self._runtime._buffered_outcomes.values())

    async def read(self) -> Batch:
        runtime = self._runtime
        events: list[Event] = []
        while runtime._events and len(events) < CODEX_RUNTIME_EVENTS_PER_BATCH:
            events.append(runtime._events.popleft())
        facts: list = []
        previews = self._drain_delta_previews()
        while runtime._facts and len(facts) + len(previews) < CODEX_RUNTIME_EVENTS_PER_BATCH:
            facts.append(runtime._facts.popleft())
        facts.extend(previews)
        evidence = []
        while True:
            # Each terminal removal releases one backpressured insertion; never discard exact
            # outcomes.
            try:
                outcome = runtime._outcomes.get_nowait()
            except asyncio.QueueEmpty:
                break
            runtime._buffered_outcomes.pop((outcome.native_session_id, outcome.native_turn_id))
            evidence.append(outcome)
        status = self._status()
        status_changed = status != self._last_status
        self._last_status = status
        progressed = bool(events or facts or evidence or status_changed)
        has_more = bool(runtime._events or runtime._facts or not runtime._outcomes.empty())
        return Batch(
            events=events,
            progressed=progressed,
            has_more=has_more,
            status=status,
            trajectory=facts,
            terminal_evidence=evidence,
        )

    def _drain_delta_previews(self) -> list:
        runtime = self._runtime
        previews: list = []
        for item_id, text in list(runtime._delta_items.items()):
            seen = runtime._delta_previewed_chars.get(item_id, 0)
            if len(text) <= seen:
                continue
            runtime._delta_previewed_chars[item_id] = len(text)
            previews.append(
                _fact(
                    kind=TrajectoryKind.ASSISTANT,
                    summary=ContentPreview.from_text(text).text,
                    native_id=item_id,
                    turn_id=runtime._active_turn_id,
                    status=TrajectoryStatus.RUNNING,
                    lane=TrajectoryLane.MODEL,
                )
            )
        return previews

    def _status(self) -> Status | None:
        runtime = self._runtime
        if runtime._pending_interaction is not None:
            # Display hint only; never a control decision input.
            return Status.AWAITING_INPUT
        return runtime._status_hint

    def health_snapshot(self) -> tuple[ChannelHealth, ...]:
        runtime = self._runtime
        if runtime._health is ConnectionHealth.DEGRADED:
            state = ChannelHealthState.DEGRADED
        elif runtime._health is ConnectionHealth.DISCONNECTED:
            state = ChannelHealthState.FAILED
        elif runtime._health is ConnectionHealth.CONNECTED:
            state = ChannelHealthState.HEALTHY
        else:
            state = ChannelHealthState.STARTING
        return (
            ChannelHealth(
                channel_id=_LIVE_CHANNEL_ID,
                state=state,
                diagnostics=tuple(runtime._diagnostics),
                dropped=runtime._dropped,
                accepted=runtime._accepted,
            ),
        )
