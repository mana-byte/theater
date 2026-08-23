"""Generation-safe snapshot, paging, and long-poll coordination for trajectory views."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Callable
from typing import Protocol

from theater.regie.trajectory.models import (
    MAX_PAGE_RECORDS,
    PanelInfo,
    PanelStatus,
    TrajectoryFollow,
    TrajectoryPage,
    WireDecodeError,
)
from theater.regie.trajectory.state import ParticipantTrajectoryState, TrajectoryStateStore


class DaemonClientCompatible(Protocol):
    """The tiny async surface needed from a régie daemon client."""

    async def call(self, method: str, **params: object) -> object: ...


StateListener = Callable[[ParticipantTrajectoryState], None]


async def _maybe_await(value: object) -> object:
    if inspect.isawaitable(value):
        return await value
    return value


def _ensure_participant(value: object, participant_id: str, noun: str) -> object:
    if getattr(value, "participant_id", None) != participant_id:
        raise WireDecodeError(f"trajectory {noun} participant does not match its request")
    return value


class TrajectoryController:
    """Own disposable query/follow clients and reject every stale response."""

    def __init__(
        self,
        query_client: DaemonClientCompatible | object,
        follow_client: DaemonClientCompatible | object,
        *,
        state_store: TrajectoryStateStore | None = None,
        page_limit: int = MAX_PAGE_RECORDS,
        follow_wait: float = 20.0,
    ) -> None:
        if query_client is follow_client:
            raise ValueError("trajectory query and follow clients must be distinct")
        if not 1 <= page_limit <= MAX_PAGE_RECORDS:
            raise ValueError(f"page_limit must be in [1, {MAX_PAGE_RECORDS}]")
        if not 0 <= follow_wait <= 20:
            raise ValueError("follow_wait must be in [0, 20]")
        self.query_client = query_client
        self.follow_client = follow_client
        self.state_store = state_store or TrajectoryStateStore()
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
        before = set(self.state_store.participant_ids())
        state = self.state_store.get(participant_id)
        for evicted in before - set(self.state_store.participant_ids()):
            self._schedule_close_hint(evicted)
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

    @staticmethod
    def _page(value: object, participant_id: str | None = None) -> TrajectoryPage:
        if isinstance(value, TrajectoryPage):
            return value
        return TrajectoryPage.from_wire(value, participant_id=participant_id)

    @staticmethod
    def _follow(value: object, participant_id: str | None = None) -> TrajectoryFollow:
        if isinstance(value, TrajectoryFollow):
            return value
        return TrajectoryFollow.from_wire(value, participant_id=participant_id)

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
        if not state.records:
            state.panel = PanelInfo(PanelStatus.LOADING)
        state.retry_kind = None
        state.retry_message = ""
        self._publish(state)
        params: dict[str, object] = {"id": participant_id, "limit": self.page_limit}
        if before is not None:
            params["before"] = before
        try:
            response = await self._call(self.query_client, "trajectory.snapshot", **params)
            page = self._page(response, participant_id)
            _ensure_participant(page, participant_id, "snapshot")
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
        if start_follow and page.panel.status not in {
            PanelStatus.DEAD,
            PanelStatus.EXTERNAL,
            PanelStatus.MISSING,
            PanelStatus.UNAVAILABLE,
            PanelStatus.UNTRUSTED,
        }:
            await self.start_follow(participant_id, expected_generation=generation)
        return page

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
        try:
            response = await self._call(
                self.query_client,
                "trajectory.snapshot",
                id=participant_id,
                before=state.older_cursor,
                limit=self.page_limit,
            )
            page = self._page(response, participant_id)
            _ensure_participant(page, participant_id, "page")
        except asyncio.CancelledError:
            state.loading_older = False
            raise
        except Exception as exc:
            if self._is_current(participant_id, generation):
                state.mark_retry("older", str(exc) or "Older trajectory page failed.")
                self._publish(state)
            return None
        if not self._is_current(participant_id, generation):
            state.loading_older = False
            return None
        state.apply_older(page)
        self._publish(state)
        return page

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
        await self._cancel_follow()
        generation = self._generation
        self._follow_task = asyncio.create_task(self._follow_loop(participant_id, generation))
        return True

    async def _follow_loop(self, participant_id: str, generation: int) -> None:
        state = self.state_for(participant_id)
        try:
            while self._is_current(participant_id, generation):
                response = await self._call(
                    self.follow_client,
                    "trajectory.follow",
                    id=participant_id,
                    stream_id=state.stream_id,
                    after=state.cursor,
                    wait=min(20.0, self.follow_wait),
                    limit=self.page_limit,
                )
                delta = self._follow(response, participant_id)
                if not self._is_current(participant_id, generation):
                    return
                _ensure_participant(delta, participant_id, "follow")
                if delta.resync_required:
                    state.mark_resync(
                        delta.reason or "The trajectory stream requires a fresh snapshot."
                    )
                    self._publish(state)
                    return
                state.apply_follow(delta)
                self._publish(state)
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

    async def pause_follow(self, participant_id: str | None = None) -> None:
        """Pause only tail movement; the long poll may continue collecting records."""
        participant_id = participant_id or self._active_participant
        if participant_id is None:
            return
        self.state_for(participant_id).pause_follow()
        self._publish(self.state_for(participant_id))

    async def resume_follow(self, participant_id: str | None = None) -> bool:
        participant_id = participant_id or self._active_participant
        if participant_id is None or participant_id != self._active_participant:
            return False
        state = self.state_for(participant_id)
        state.resume_follow()
        self._publish(state)
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

    async def _best_effort_close(self, participant_id: str) -> None:
        try:
            await self._call(self.query_client, "trajectory.close", id=participant_id)
        except Exception:
            return

    def _schedule_close_hint(self, participant_id: str) -> None:
        if self._closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._best_effort_close(participant_id))
        self._close_hint_tasks.add(task)
        task.add_done_callback(self._close_hint_tasks.discard)

    async def close(self) -> None:
        """Cancel follow and close both disposable client connections."""
        if self._closed:
            return
        self._closed = True
        self._generation += 1
        await self._cancel_follow()
        participants = set(self.state_store.participant_ids())
        if self._active_participant is not None:
            participants.add(self._active_participant)
        for participant_id in participants:
            await self._best_effort_close(participant_id)
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
