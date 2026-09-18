"""Public snapshot assembly and durable orchestration-stream synchronization."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import cast

from theater.frontend._results import StateFollowResult
from theater.frontend.client import FrontendClient, FrontendResponseError
from theater.frontend.dto import (
    Event,
    EventCursor,
    EventTransaction,
    Job,
    Operation,
    Participant,
    Provider,
    SnapshotPage,
    Workspace,
)
from theater.frontend.dto._wire import JSONValue
from theater.frontend.transport import FrontendTransportError

_RESNAPSHOT_CODES = frozenset({"resnapshot_required", "snapshot_expired"})


class StateSynchronizationError(RuntimeError):
    """The public state stream cannot safely extend the installed projection."""


class StateResnapshotRequired(StateSynchronizationError):
    """A fresh immutable snapshot is required before further state application."""


@dataclass(frozen=True, slots=True)
class StateProjection:
    """A complete immutable orchestration projection at one durable cursor."""

    cursor: EventCursor
    participants: Mapping[str, Participant]
    operations: Mapping[str, Operation]
    jobs: Mapping[str, Job]
    providers: Mapping[str, Provider]
    workspaces: Mapping[str, Workspace]
    usage: Mapping[str, JSONValue] = field(default_factory=lambda: MappingProxyType({}))
    snapshot_extras: Mapping[int, Mapping[str, JSONValue]] = field(
        default_factory=lambda: MappingProxyType({})
    )
    unapplied_events: tuple[Event, ...] = ()
    entity_revisions: Mapping[tuple[str, str], int] = field(
        default_factory=lambda: MappingProxyType({})
    )
    stale: bool = False


class StateSynchronizer:
    """Build and extend one public state projection without private daemon access."""

    def __init__(self, client: FrontendClient, *, resnapshot_attempts: int = 1) -> None:
        if type(resnapshot_attempts) is not int or resnapshot_attempts < 0:
            raise ValueError("resnapshot_attempts must be a non-negative integer")
        self._client = client
        self._resnapshot_attempts = resnapshot_attempts
        self._projection: StateProjection | None = None

    @property
    def projection(self) -> StateProjection | None:
        """The last fully installed projection, retained as stale after a disconnect."""
        return self._projection

    async def synchronize_once(
        self,
        *,
        page_size: int | None = None,
        wait_seconds: float | None = None,
        limit: int | None = None,
    ) -> StateProjection:
        """Install a snapshot initially, then make one bounded follow attempt."""
        if self._projection is None:
            return await self.refresh(page_size=page_size)
        return await self.follow_once(
            page_size=page_size,
            wait_seconds=wait_seconds,
            limit=limit,
        )

    async def refresh(self, *, page_size: int | None = None) -> StateProjection:
        """Fetch every immutable page and replace the projection only after validation."""
        attempts = 0
        while True:
            try:
                candidate = await self._fetch_snapshot(page_size=page_size)
            except FrontendResponseError as exc:
                if exc.value.code in _RESNAPSHOT_CODES and attempts < self._resnapshot_attempts:
                    attempts += 1
                    continue
                self._mark_stale()
                raise
            except (FrontendTransportError, asyncio.CancelledError):
                self._mark_stale()
                raise
            self._projection = candidate
            return candidate

    async def resnapshot(self, *, page_size: int | None = None) -> StateProjection:
        """Replace the projection atomically from a fresh immutable snapshot."""
        return await self.refresh(page_size=page_size)

    async def follow_once(
        self,
        *,
        page_size: int | None = None,
        wait_seconds: float | None = None,
        limit: int | None = None,
    ) -> StateProjection:
        """Follow from the last complete cursor, reconnecting only on a later call."""
        current = self._projection
        if current is None:
            return await self.refresh(page_size=page_size)
        params: dict[str, object] = {}
        if wait_seconds is not None:
            params["wait_seconds"] = wait_seconds
        if limit is not None:
            params["limit"] = limit
        try:
            result = await self._client.state.follow(current.cursor, **params)
            candidate = _apply_follow(current, result.value)
        except FrontendResponseError as exc:
            if exc.value.code in _RESNAPSHOT_CODES:
                return await self.refresh(page_size=page_size)
            raise
        except StateResnapshotRequired:
            return await self.refresh(page_size=page_size)
        except (FrontendTransportError, asyncio.CancelledError):
            self._mark_stale()
            raise
        self._projection = candidate
        return candidate

    async def _fetch_snapshot(self, *, page_size: int | None) -> StateProjection:
        params: dict[str, object] = {}
        if page_size is not None:
            params["page_size"] = page_size
        first = (await self._client.state.snapshot(**params)).value
        snapshot_id = first.snapshot_id
        try:
            pages = [first]
            _validate_snapshot_page(first, snapshot_id, first.ending_cursor, expected_page=0)
            page = first
            while not page.complete:
                expected_page = len(pages)
                page = (await self._client.state.page(snapshot_id, expected_page)).value
                _validate_snapshot_page(
                    page,
                    snapshot_id,
                    first.ending_cursor,
                    expected_page=expected_page,
                )
                pages.append(page)
            return _projection_from_pages(pages)
        finally:
            await self._release_snapshot(snapshot_id)

    async def _release_snapshot(self, snapshot_id: str) -> None:
        """Release in the background if caller cancellation reaches cleanup."""
        release = asyncio.create_task(self._client.state.release(snapshot_id))
        try:
            await asyncio.shield(release)
        except asyncio.CancelledError:
            release.add_done_callback(_consume_task_exception)
            raise
        except (FrontendResponseError, FrontendTransportError):
            # A missing handle is already released from the daemon cache.
            return

    def _mark_stale(self) -> None:
        if self._projection is not None and not self._projection.stale:
            self._projection = replace(self._projection, stale=True)


def _consume_task_exception[T](task: asyncio.Future[T]) -> None:
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.exception()


def _validate_snapshot_page(
    page: SnapshotPage,
    snapshot_id: str,
    ending_cursor: EventCursor,
    *,
    expected_page: int,
) -> None:
    if page.snapshot_id != snapshot_id:
        raise StateSynchronizationError("snapshot page belongs to a different snapshot handle")
    if page.ending_cursor != ending_cursor:
        raise StateSynchronizationError("snapshot pages do not share one ending cursor")
    if page.page != expected_page:
        raise StateSynchronizationError("snapshot pages are not contiguous from page zero")


def _projection_from_pages(pages: list[SnapshotPage]) -> StateProjection:
    if not pages or not pages[-1].complete:
        raise StateSynchronizationError("snapshot assembly ended without a complete final page")
    participants: dict[str, Participant] = {}
    operations: dict[str, Operation] = {}
    jobs: dict[str, Job] = {}
    providers: dict[str, Provider] = {}
    workspaces: dict[str, Workspace] = {}
    revisions: dict[tuple[str, str], int] = {}
    page_extras: dict[int, Mapping[str, JSONValue]] = {}
    usage: Mapping[str, JSONValue] = MappingProxyType({})
    for page in pages:
        page_extras[page.page] = MappingProxyType(dict(page.extra))
        if "usage" in page.extra:
            value = page.extra["usage"]
            if not isinstance(value, Mapping):
                raise StateSynchronizationError("snapshot usage must be an object")
            if page.page == 0:
                usage = MappingProxyType(dict(value))
            elif usage != value:
                raise StateSynchronizationError("snapshot pages disagree about usage")
        _add_snapshot_entities(participants, page.participants, "participants", revisions)
        _add_snapshot_entities(operations, page.operations, "operations", revisions)
        _add_snapshot_entities(jobs, page.jobs, "jobs", revisions)
        _add_snapshot_entities(providers, page.providers, "providers", revisions)
        _add_snapshot_entities(workspaces, page.workspaces, "workspaces", revisions)
    return StateProjection(
        cursor=pages[0].ending_cursor,
        participants=MappingProxyType(participants),
        operations=MappingProxyType(operations),
        jobs=MappingProxyType(jobs),
        providers=MappingProxyType(providers),
        workspaces=MappingProxyType(workspaces),
        usage=usage,
        snapshot_extras=MappingProxyType(page_extras),
        entity_revisions=MappingProxyType(revisions),
    )


def _add_snapshot_entities[T](
    target: dict[str, T],
    items: tuple[T, ...],
    domain: str,
    revisions: dict[tuple[str, str], int],
) -> None:
    for item in items:
        entity_id = _entity_id(item)
        if entity_id in target:
            raise StateSynchronizationError(f"snapshot has duplicate {domain} id {entity_id!r}")
        target[entity_id] = item
        revisions[(domain, entity_id)] = _projection_revision(item)


def _entity_id(value: object) -> str:
    for name in ("participant_id", "operation_id", "handle", "provider_id", "workspace_id"):
        entity_id = getattr(value, name, None)
        if isinstance(entity_id, str) and entity_id:
            return entity_id
    raise StateSynchronizationError("snapshot entity has no stable public identifier")


def _projection_revision(value: object) -> int:
    extra = getattr(value, "extra", None)
    if not isinstance(extra, Mapping):
        return 0
    revision = extra.get("projection_revision")
    return revision if type(revision) is int and revision >= 0 else 0


def _apply_follow(projection: StateProjection, result: StateFollowResult) -> StateProjection:
    candidate = projection
    for transaction in result.transactions:
        candidate = _apply_transaction(candidate, transaction)
    if result.cursor != candidate.cursor:
        raise StateResnapshotRequired("follow cursor is not the last complete applied transaction")
    return replace(candidate, stale=False)


def _apply_transaction(
    projection: StateProjection, transaction: EventTransaction
) -> StateProjection:
    cursor = projection.cursor
    ending = transaction.ending_cursor
    if ending.stream_id != cursor.stream_id:
        raise StateResnapshotRequired("transaction belongs to a different orchestration stream")
    if ending.sequence <= cursor.sequence:
        return projection
    if ending.sequence != cursor.sequence + len(transaction.events):
        raise StateResnapshotRequired("follow transaction leaves a cursor gap")
    participants = dict(projection.participants)
    operations = dict(projection.operations)
    jobs = dict(projection.jobs)
    providers = dict(projection.providers)
    workspaces = dict(projection.workspaces)
    revisions = dict(projection.entity_revisions)
    unapplied = list(projection.unapplied_events)
    collections: dict[str, dict[str, object]] = {
        "participants": cast(dict[str, object], participants),
        "operations": cast(dict[str, object], operations),
        "jobs": cast(dict[str, object], jobs),
        "providers": cast(dict[str, object], providers),
        "workspaces": cast(dict[str, object], workspaces),
    }
    for event in transaction.events:
        domain = _event_domain(event.kind)
        revision_key = (domain or f"event:{event.kind}", event.entity_id)
        if event.entity_revision <= revisions.get(revision_key, -1):
            continue
        if event.kind in {"participant.removed", "job.removed"}:
            assert domain is not None
            collections[domain].pop(event.entity_id, None)
            revisions[revision_key] = event.entity_revision
            continue
        if domain is None:
            unapplied.append(event)
            revisions[revision_key] = event.entity_revision
            continue
        entity = _decode_event_entity(domain, event)
        if _entity_id(entity) != event.entity_id:
            raise StateResnapshotRequired(
                "event payload identity does not match its stable entity id"
            )
        if _is_active_entity(domain, entity):
            collections[domain][event.entity_id] = entity
        else:
            collections[domain].pop(event.entity_id, None)
        revisions[revision_key] = event.entity_revision
    return StateProjection(
        cursor=ending,
        participants=MappingProxyType(participants),
        operations=MappingProxyType(operations),
        jobs=MappingProxyType(jobs),
        providers=MappingProxyType(providers),
        workspaces=MappingProxyType(workspaces),
        usage=projection.usage,
        snapshot_extras=projection.snapshot_extras,
        unapplied_events=tuple(unapplied),
        entity_revisions=MappingProxyType(revisions),
    )


def _event_domain(kind: str) -> str | None:
    if kind in {
        "participant.updated",
        "participant.controls_changed",
        "participant.owner_changed",
        "terminal.binding_changed",
    }:
        return "participants"
    if kind == "provider.updated":
        return "providers"
    if kind == "operation.updated":
        return "operations"
    if kind == "job.updated":
        return "jobs"
    if kind in {"workspace.updated", "workspace.usage_changed"}:
        return "workspaces"
    if kind in {"participant.removed", "job.removed"}:
        return "participants" if kind.startswith("participant") else "jobs"
    return None


def _decode_event_entity(domain: str, event: Event) -> object:
    try:
        if domain == "participants":
            return Participant.from_wire(event.payload)
        if domain == "operations":
            return Operation.from_wire(event.payload)
        if domain == "jobs":
            return Job.from_wire(event.payload)
        if domain == "providers":
            return Provider.from_wire(event.payload)
        if domain == "workspaces":
            return Workspace.from_wire(event.payload)
    except (TypeError, ValueError) as exc:
        raise StateResnapshotRequired(
            f"{event.kind} does not carry a complete public {domain} projection"
        ) from exc
    raise StateResnapshotRequired(f"unknown public state projection domain {domain!r}")


def _is_active_entity(domain: str, entity: object) -> bool:
    """Mirror the public snapshot's active rows without daemon imports."""
    if domain == "participants" and isinstance(entity, Participant):
        return entity.status != "dead"
    if domain == "operations" and isinstance(entity, Operation):
        return entity.state not in {"succeeded", "failed"}
    if domain == "jobs" and isinstance(entity, Job):
        return entity.state == "running"
    if domain == "providers" and isinstance(entity, Provider):
        return True
    if domain == "workspaces" and isinstance(entity, Workspace):
        return entity.state != "removed"
    raise StateResnapshotRequired(f"invalid public state projection domain {domain!r}")


__all__ = [
    "StateProjection",
    "StateResnapshotRequired",
    "StateSynchronizationError",
    "StateSynchronizer",
]
