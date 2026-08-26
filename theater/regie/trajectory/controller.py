"""Generation-safe snapshot, paging, and long-poll coordination."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Callable
from typing import Protocol

from theater.constants.trajectory import (
    TRAJECTORY_FOLLOW_TIMEOUT_SECONDS,
    TRAJECTORY_PAGE_RECORD_LIMIT,
)
from theater.regie.trajectory.models import decode_delta, decode_location, decode_page
from theater.regie.trajectory.state import ParticipantTrajectoryState, TrajectoryStateStore
from theater.trajectory import (
    PanelState,
    PanelStateInfo,
    TrajectoryDelta,
    TrajectoryLocation,
    TrajectoryPage,
    TrajectoryParticipantState,
    TrajectoryValidationError,
)


class DaemonClientCompatible(Protocol):
    """The tiny async surface needed from a Régie daemon client."""

    async def call(self, method: str, **params: object) -> object: ...


StateListener = Callable[[ParticipantTrajectoryState], None]


def _can_follow(state: ParticipantTrajectoryState) -> bool:
    return (
        not state.resyncing
        and state.retry_kind != "resync"
        and state.panel.participant_state
        not in {
            TrajectoryParticipantState.DEAD,
            TrajectoryParticipantState.EXTERNAL,
            TrajectoryParticipantState.MISSING,
        }
        and state.panel.state not in {PanelState.UNAVAILABLE, PanelState.UNTRUSTED}
        and state.stream_id is not None
        and state.cursor is not None
    )


async def _maybe_await(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


def _validate_page(page: TrajectoryPage, participant_id: str) -> None:
    if any(record.participant_id != participant_id for record in page.records):
        raise TrajectoryValidationError("trajectory page contains a record for another participant")


def _validate_delta(delta: TrajectoryDelta, participant_id: str) -> None:
    if any(upsert.record.participant_id != participant_id for upsert in delta.upserts):
        raise TrajectoryValidationError(
            "trajectory delta contains an upsert for another participant"
        )


def _validate_stream(expected: str | None, actual: str | None, noun: str) -> None:
    if actual != expected:
        raise TrajectoryValidationError(f"{noun} stream does not match active stream")


class TrajectoryController:
    """Own disposable query/follow clients and reject stale responses."""

    def __init__(
        self,
        query_client: DaemonClientCompatible | object,
        follow_client: DaemonClientCompatible | object,
        *,
        state_store: TrajectoryStateStore | None = None,
        page_limit: int = TRAJECTORY_PAGE_RECORD_LIMIT,
        follow_wait: float = TRAJECTORY_FOLLOW_TIMEOUT_SECONDS,
    ) -> None:
        if query_client is follow_client:
            raise ValueError("trajectory query and follow clients must be distinct")
        if not 1 <= page_limit <= TRAJECTORY_PAGE_RECORD_LIMIT:
            raise ValueError(f"page_limit must be in [1, {TRAJECTORY_PAGE_RECORD_LIMIT}]")
        if not 0 <= follow_wait <= TRAJECTORY_FOLLOW_TIMEOUT_SECONDS:
            raise ValueError(f"follow_wait must be in [0, {TRAJECTORY_FOLLOW_TIMEOUT_SECONDS}]")
        self.query_client = query_client
        self.follow_client = follow_client
        self.state_store = state_store if state_store is not None else TrajectoryStateStore()
        self.page_limit = page_limit
        self.follow_wait = follow_wait
        self._generation = 0
        self._active_participant: str | None = None
        self._follow_task: asyncio.Task[None] | None = None
        self._listeners: dict[int, StateListener] = {}
        self._next_listener = 0
        self._closed = False
        self._close_hint_tasks: set[asyncio.Task[None]] = set()

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def active_participant(self) -> str | None:
        return self._active_participant

    @property
    def follow_task(self) -> asyncio.Task[None] | None:
        return self._follow_task

    def state_for(self, participant_id: str) -> ParticipantTrajectoryState:
        before = {
            stored_id: state.stream_id
            for stored_id in self.state_store.participant_ids()
            if (state := self.state_store.peek(stored_id)) is not None
        }
        state = self.state_store.get(participant_id)
        after = set(self.state_store.participant_ids())
        for evicted in before.keys() - after:
            self._schedule_close_hint(evicted, before[evicted])
        return state

    def subscribe(self, listener: StateListener) -> Callable[[], None]:
        """Register a synchronous repaint hook and return its removal function."""
        token = self._next_listener
        self._next_listener += 1
        self._listeners[token] = listener

        def unsubscribe() -> None:
            self._listeners.pop(token, None)

        return unsubscribe

    def _publish(self, state: ParticipantTrajectoryState) -> None:
        for listener in tuple(self._listeners.values()):
            with contextlib.suppress(Exception):
                listener(state)

    def _is_current(self, participant_id: str, generation: int) -> bool:
        return (
            not self._closed
            and generation == self._generation
            and participant_id == self._active_participant
        )

    async def _call(self, client: object, method: str, **params: object) -> object:
        call = getattr(client, "call", None)
        if callable(call):
            return await _maybe_await(call(method, **params))
        method_name = method.replace(".", "_")
        operation = getattr(client, method_name, None)
        if not callable(operation):
            raise TypeError(f"trajectory client does not implement {method}")
        return await _maybe_await(operation(**params))

    async def open(
        self,
        participant_id: str,
        *,
        before: str | None = None,
        start_follow: bool = True,
    ) -> TrajectoryPage | None:
        """Load one snapshot and optionally begin its ordinary follow loop."""
        if self._closed:
            raise RuntimeError("trajectory controller is closed")
        await self._cancel_follow()
        previous = self._active_participant
        if previous is not None and previous != participant_id:
            self._schedule_close_hint(previous)
        self._generation += 1
        generation = self._generation
        self._active_participant = participant_id
        state = self.state_for(participant_id)
        state.loading = True
        if not state.records:
            state.panel = PanelStateInfo(PanelState.WAITING, "Loading trajectory…")
        state.retry_kind = None
        state.retry_message = ""
        self._publish(state)
        params: dict[str, object] = {"id": participant_id, "limit": self.page_limit}
        if before is not None:
            params["before"] = before
        try:
            page = decode_page(await self._call(self.query_client, "trajectory.snapshot", **params))
            _validate_page(page, participant_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._is_current(participant_id, generation):
                state.mark_stale(str(exc) or "Trajectory snapshot failed.")
                self._publish(state)
            return None
        if not self._is_current(participant_id, generation):
            return None
        state.apply_snapshot(page)
        self._publish(state)
        if start_follow and _can_follow(state):
            await self.start_follow(participant_id, expected_generation=generation)
        return page

    async def locate(self, participant_id: str, record_id: str) -> TrajectoryLocation:
        """Resolve one exact record through the daemon's bounded lookup."""
        response = await self._call(
            self.query_client,
            "trajectory.locate",
            id=participant_id,
            record_id=record_id,
        )
        location = decode_location(response)
        if location.participant_id != participant_id:
            raise TrajectoryValidationError("trajectory location participant does not match target")
        if location.requested_record_id != record_id:
            raise TrajectoryValidationError("trajectory location record does not match target")
        return location

    async def load_older(self, participant_id: str | None = None) -> TrajectoryPage | None:
        """Request exactly one older page; this method never chain-loads."""
        participant_id = participant_id or self._active_participant
        if participant_id is None or participant_id != self._active_participant:
            return None
        state = self.state_for(participant_id)
        if state.loading_older or not state.has_older or state.older_cursor is None:
            return None
        generation = self._generation
        state.loading_older = True
        state.retry_kind = None
        state.retry_message = ""
        self._publish(state)
        result: TrajectoryPage | None = None
        try:
            response = await self._call(
                self.query_client,
                "trajectory.snapshot",
                id=participant_id,
                before=state.older_cursor,
                limit=self.page_limit,
            )
            page = decode_page(response)
            _validate_page(page, participant_id)
            _validate_stream(state.stream_id, page.stream_id, "older page")
            if self._is_current(participant_id, generation):
                state.apply_older(page)
                self._publish(state)
                result = page
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._is_current(participant_id, generation):
                state.mark_retry("older", str(exc) or "Older trajectory page failed.")
                self._publish(state)
        finally:
            state.loading_older = False
        return result

    async def start_follow(
        self,
        participant_id: str | None = None,
        *,
        expected_generation: int | None = None,
    ) -> bool:
        """Start one cancellable long-poll loop for the active participant."""
        participant_id = participant_id or self._active_participant
        if participant_id is None or participant_id != self._active_participant or self._closed:
            return False
        if expected_generation is not None and expected_generation != self._generation:
            return False
        state = self.state_for(participant_id)
        if not _can_follow(state):
            return False
        await self._cancel_follow()
        generation = self._generation
        self._follow_task = asyncio.create_task(self._follow_loop(participant_id, generation))
        return True

    async def _follow_loop(self, participant_id: str, generation: int) -> None:
        state = self.state_for(participant_id)
        try:
            while self._is_current(participant_id, generation):
                if state.stream_id is None or state.cursor is None:
                    return
                response = await self._call(
                    self.follow_client,
                    "trajectory.follow",
                    id=participant_id,
                    stream_id=state.stream_id,
                    after=state.cursor,
                    wait=min(TRAJECTORY_FOLLOW_TIMEOUT_SECONDS, self.follow_wait),
                    limit=self.page_limit,
                )
                delta = decode_delta(response)
                if not self._is_current(participant_id, generation):
                    return
                _validate_delta(delta, participant_id)
                _validate_stream(state.stream_id, delta.stream_id, "follow")
                if delta.resync_required:
                    await self._resync_from_follow(
                        participant_id,
                        generation,
                        delta.reason or "The trajectory stream requires a fresh snapshot.",
                    )
                    return
                state.apply_follow(delta)
                self._publish(state)
                if not _can_follow(state):
                    return
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            if self._is_current(participant_id, generation):
                state.mark_stale(str(exc) or "Trajectory follow failed.")
                self._publish(state)
        finally:
            if self._follow_task is asyncio.current_task():
                self._follow_task = None

    async def _resync_from_follow(self, participant_id: str, generation: int, message: str) -> None:
        if not self._is_current(participant_id, generation):
            return
        self._generation += 1
        fresh_generation = self._generation
        state = self.state_for(participant_id)
        state.mark_resync(message)
        state.resyncing = True
        self._publish(state)
        try:
            page = decode_page(
                await self._call(
                    self.query_client,
                    "trajectory.snapshot",
                    id=participant_id,
                    limit=self.page_limit,
                )
            )
            _validate_page(page, participant_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._is_current(participant_id, fresh_generation):
                state.resyncing = False
                state.mark_resync(str(exc) or "Trajectory resync failed.")
                self._publish(state)
            return
        if not self._is_current(participant_id, fresh_generation):
            return
        state.apply_snapshot(page)
        self._publish(state)
        if _can_follow(state):
            self._follow_task = asyncio.create_task(
                self._follow_loop(participant_id, fresh_generation)
            )

    async def pause_follow(self, participant_id: str | None = None) -> None:
        """Pause tail movement while the long poll remains cancellable."""
        participant_id = participant_id or self._active_participant
        if participant_id is None:
            return
        state = self.state_for(participant_id)
        state.pause_follow()
        self._publish(state)

    async def resume_follow(self, participant_id: str | None = None) -> bool:
        participant_id = participant_id or self._active_participant
        if participant_id is None or participant_id != self._active_participant:
            return False
        state = self.state_for(participant_id)
        if state.resyncing:
            return True
        resync_required = state.retry_kind == "resync"
        state.resume_follow()
        self._publish(state)
        if resync_required:
            return await self.resync(participant_id) is not None
        if self._follow_task is None or self._follow_task.done():
            return await self.start_follow(participant_id)
        return True

    async def resync(self, participant_id: str | None = None) -> TrajectoryPage | None:
        """Discard the stream cursor through one guarded fresh snapshot."""
        participant_id = participant_id or self._active_participant
        if participant_id is None:
            return None
        return await self.open(participant_id, start_follow=True)

    async def retry(self, participant_id: str | None = None) -> TrajectoryPage | None:
        participant_id = participant_id or self._active_participant
        if participant_id is None:
            return None
        state = self.state_for(participant_id)
        if state.retry_kind == "older":
            await self.load_older(participant_id)
            return None
        return await self.resync(participant_id)

    async def _cancel_follow(self) -> None:
        task, self._follow_task = self._follow_task, None
        if task is None:
            return
        if not task.done():
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    async def _best_effort_close(self, participant_id: str, stream_id: str | None = None) -> None:
        params = {"id": participant_id}
        if stream_id is not None:
            params["stream_id"] = stream_id
        try:
            await self._call(self.query_client, "trajectory.close", **params)
        except Exception:
            return

    def _schedule_close_hint(self, participant_id: str, stream_id: str | None = None) -> None:
        if self._closed:
            return
        if stream_id is None:
            state = self.state_store.peek(participant_id)
            stream_id = state.stream_id if state is not None else None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._best_effort_close(participant_id, stream_id))
        self._close_hint_tasks.add(task)
        task.add_done_callback(self._close_hint_tasks.discard)

    async def close(self) -> None:
        """Cancel follow, issue close hints, and close both disposable clients."""
        if self._closed:
            return
        self._closed = True
        self._generation += 1
        await self._cancel_follow()
        participants = set(self.state_store.participant_ids())
        if self._active_participant is not None:
            participants.add(self._active_participant)
        for participant_id in participants:
            state = self.state_store.peek(participant_id)
            await self._best_effort_close(
                participant_id,
                state.stream_id if state is not None else None,
            )
        if self._close_hint_tasks:
            await asyncio.gather(*self._close_hint_tasks, return_exceptions=True)
            self._close_hint_tasks.clear()
        seen: set[int] = set()
        for client in (self.query_client, self.follow_client):
            if id(client) in seen:
                continue
            seen.add(id(client))
            close = getattr(client, "aclose", None) or getattr(client, "close", None)
            if callable(close):
                with contextlib.suppress(Exception):
                    await _maybe_await(close())
        self._listeners.clear()

    aclose = close


__all__ = ["DaemonClientCompatible", "TrajectoryController"]
