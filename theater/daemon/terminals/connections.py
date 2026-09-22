"""Live provider generations and daemon-to-provider callback correlation."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import math
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from jsonschema.exceptions import ValidationError

from theater import protocol
from theater.daemon.events.publication import (
    catalog_invalidated_event,
    terminal_binding_event,
)
from theater.daemon.plugins.credentials import credential_verifier
from theater.daemon.terminals.registry import provider_event
from theater.frontend.capabilities import CALLBACK_CATALOG, MAX_FRAME_BYTES, PUBLIC_LIMITS
from theater.frontend.schemas import validate_callback_request, validate_callback_response
from theater.models import ProviderRecord, TheaterError, new_id, now

logger = logging.getLogger("theater.daemon.terminals")

PENDING_CALLBACK_LIMIT = int(PUBLIC_LIMITS["provider_pending_callbacks"])
CALLBACK_TIMEOUT_SECONDS = float(PUBLIC_LIMITS["provider_callback_timeout_seconds"])
PROVIDER_LEASE_SECONDS = float(PUBLIC_LIMITS["provider_lease_seconds"])


class ProviderUnavailable(TheaterError):
    code = "provider_unavailable"

    def __init__(self, provider_id: str, reason: str) -> None:
        self.details: dict[str, object] = {"provider_id": provider_id, "reason": reason}
        super().__init__(f"provider {provider_id!r} is unavailable: {reason}")


class ProviderBusy(TheaterError):
    code = "provider_busy"

    def __init__(self, provider_id: str, reason: str) -> None:
        self.details = {"provider_id": provider_id, "reason": reason}
        super().__init__(f"provider {provider_id!r} is busy: {reason}")


class StaleGeneration(TheaterError):
    code = "stale_generation"

    def __init__(self, provider_id: str, generation: int) -> None:
        self.details = {"provider_id": provider_id, "provider_generation": generation}
        super().__init__(
            f"provider {provider_id!r} generation {generation} is not the active generation"
        )


class CallbackOutcomeUnknown(ProviderUnavailable):
    """The request was written, so losing its response cannot prove non-execution."""

    def __init__(self, provider_id: str, callback_id: str, reason: str) -> None:
        super().__init__(provider_id, reason)
        self.callback_id = callback_id
        self.details = {**self.details, "callback_id": callback_id, "possibly_executed": True}


class ProviderCallbackRejected(TheaterError):
    def __init__(self, error: Mapping[str, object]) -> None:
        code = error.get("code")
        message = error.get("message")
        self.code = str(code) if isinstance(code, str) and code else "provider_unavailable"
        self.details = error.get("details") if isinstance(error.get("details"), Mapping) else None
        super().__init__(str(message) if isinstance(message, str) else "provider callback failed")


@dataclass(slots=True)
class _Pending:
    method: str
    params: Mapping[str, object]
    future: asyncio.Future[Mapping[str, object]]
    dispatched: bool = False


@dataclass(slots=True)
class _Peer:
    provider_id: str
    generation: int
    token: str
    lease_deadline: float
    pending_limit: int
    callback_timeout: float
    frame_limit: int
    state: str = "reconciling"
    reader: Any | None = None
    writer: Any | None = None
    attached: bool = False
    closed: bool = False
    pending: dict[str, _Pending] = field(default_factory=dict)
    abandoned: OrderedDict[str, None] = field(default_factory=OrderedDict)
    active_requests: int = 0
    write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    terminal_locks: dict[tuple[str, str], asyncio.Lock] = field(default_factory=dict)
    lease_task: asyncio.Task[None] | None = None


class ProviderConnectionService:
    """Own current callback peers; persisted timestamps are never lease clocks."""

    def __init__(
        self,
        store,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = now,
        id_factory: Callable[[], str] = new_id,
        lease_seconds: float = PROVIDER_LEASE_SECONDS,
        callback_timeout: float = CALLBACK_TIMEOUT_SECONDS,
        pending_limit: int = PENDING_CALLBACK_LIMIT,
    ) -> None:
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("provider lease must be a finite positive number")
        if not math.isfinite(callback_timeout) or callback_timeout <= 0:
            raise ValueError("provider callback timeout must be a finite positive number")
        if type(pending_limit) is not int or not 1 <= pending_limit <= PENDING_CALLBACK_LIMIT:
            raise ValueError(
                f"provider pending limit must be from 1 through {PENDING_CALLBACK_LIMIT}"
            )
        self._store = store
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._id_factory = id_factory
        self._lease_seconds = lease_seconds
        self._callback_timeout = callback_timeout
        self._pending_limit = pending_limit
        self._peers: dict[str, _Peer] = {}
        self._callback_sequence = 0

    def authenticate(self, provider_id: str, credential: str) -> ProviderRecord:
        record = self._store.providers.get(provider_id)
        if record is None or not hmac.compare_digest(
            credential_verifier(credential), record.credential_verifier
        ):
            raise ProviderUnavailable(provider_id, "authentication_failed")
        return record

    def acquire_callback(self, provider_id: str, credential: str) -> tuple[int, str]:
        record = self.authenticate(provider_id, credential)
        current = self._current_peer(provider_id)
        if current is not None:
            raise ProviderBusy(provider_id, "live_callback_owner")
        timestamp = self._wall_clock()
        with self._store.write_unit() as unit:
            generation = self._store.providers.claim_generation(
                provider_id, updated_at=timestamp, connection=unit.connection
            )
            record = self._store.providers.get(provider_id, connection=unit.connection)
            assert record is not None
            changed_bindings = self._store.terminal_bindings.mark_provider_health(
                provider_id,
                health="reconciling",
                updated_at=timestamp,
                connection=unit.connection,
            )
            revision = self._store.journal.current_sequence(connection=unit.connection) + 1
            events = [
                provider_event(record, "reconciling", timestamp, revision=revision),
                catalog_invalidated_event(
                    provider_id,
                    revision=revision + 1,
                    recorded_at=timestamp,
                    reason="provider_generation_acquired",
                ),
            ]
            for participant_id in changed_bindings:
                binding = self._store.terminal_bindings.get(
                    participant_id, connection=unit.connection
                )
                assert binding is not None
                events.append(
                    terminal_binding_event(
                        self._store,
                        binding,
                        unit.connection,
                        revision=revision + len(events),
                        recorded_at=timestamp,
                    )
                )
            self._store.journal.append_group(
                unit,
                events,
            )
        token = self._id_factory()
        self._peers[provider_id] = _Peer(
            provider_id=provider_id,
            generation=generation,
            token=token,
            lease_deadline=self._monotonic() + self._lease_seconds,
            pending_limit=self._integer_limit(
                record.limits, "provider_pending_callbacks", self._pending_limit
            ),
            callback_timeout=self._numeric_limit(
                record.limits, "provider_callback_timeout_seconds", self._callback_timeout
            ),
            frame_limit=self._integer_limit(record.limits, "max_frame_bytes", MAX_FRAME_BYTES),
        )
        return generation, token

    def rpc_generation(self, provider_id: str, credential: str) -> int:
        record = self.authenticate(provider_id, credential)
        if record.generation == 0:
            raise ProviderUnavailable(provider_id, "callback_generation_not_acquired")
        return record.generation

    def health(self, provider_id: str) -> str:
        peer = self._peers.get(provider_id)
        if peer is None or peer.closed or self._monotonic() >= peer.lease_deadline:
            return "offline"
        return peer.state

    def negotiated_limits(self, provider_id: str, generation: int) -> dict[str, int | float]:
        peer = self._require_current(provider_id, generation)
        return {
            "max_frame_bytes": peer.frame_limit,
            "provider_pending_callbacks": peer.pending_limit,
            "provider_callback_timeout_seconds": peer.callback_timeout,
            "provider_lease_seconds": self._lease_seconds,
        }

    def is_current(self, provider_id: str, generation: int) -> bool:
        peer = self._current_peer(provider_id)
        return peer is not None and peer.generation == generation

    def current_generation(self, provider_id: str) -> int | None:
        peer = self._current_peer(provider_id)
        return None if peer is None else peer.generation

    def renew(self, provider_id: str, generation: int) -> None:
        peer = self._require_current(provider_id, generation)
        peer.lease_deadline = self._monotonic() + self._lease_seconds

    def mark_online(self, provider_id: str, generation: int) -> None:
        peer = self._require_current(provider_id, generation)
        peer.state = "online"

    async def serve(self, context, reader, writer) -> None:
        provider_id = context.provider_id
        generation = context.provider_generation
        token = context.provider_connection_token
        if provider_id is None or generation is None or token is None:
            raise ProviderUnavailable(provider_id or "unknown", "invalid_callback_context")
        peer = self._peers.get(provider_id)
        if peer is None or peer.token != token or peer.generation != generation or peer.closed:
            raise StaleGeneration(provider_id, generation)
        peer.reader = reader
        peer.writer = writer
        peer.attached = True
        peer.lease_task = asyncio.create_task(
            self._watch_lease(peer),
            name=f"theater-provider-lease-{provider_id}",
        )
        try:
            while self._current_peer(provider_id) is peer:
                line = await protocol.read_message(reader)
                if not line:
                    break
                await self._receive(peer, line)
        except (ConnectionError, OSError, protocol.MessageTooLarge, ValueError, ValidationError):
            pass
        finally:
            self.disconnect(provider_id, generation, token=token)

    async def request(
        self,
        provider_id: str,
        generation: int,
        method: str,
        params: Mapping[str, object],
        *,
        timeout: float | None = None,
    ) -> Mapping[str, object]:
        spec = CALLBACK_CATALOG.get(method)
        if spec is None:
            raise ValueError(f"unknown provider callback {method!r}")
        if timeout is not None and (not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("provider callback timeout must be a finite positive number")
        peer = self._require_current(provider_id, generation)
        if not peer.attached or peer.writer is None:
            raise ProviderUnavailable(provider_id, "callback_not_attached")
        if peer.active_requests >= peer.pending_limit:
            raise ProviderBusy(peer.provider_id, "callback_queue_full")
        self._validate_target(method, generation, params)
        terminal_key = self._terminal_key(method, params)
        lock = (
            peer.terminal_locks.setdefault(terminal_key, asyncio.Lock()) if terminal_key else None
        )
        peer.active_requests += 1
        try:
            if lock is None:
                return await self._request_unlocked(peer, method, params, timeout=timeout)
            async with lock:
                self._require_same_peer(peer)
                return await self._request_unlocked(peer, method, params, timeout=timeout)
        finally:
            peer.active_requests -= 1

    async def _request_unlocked(
        self,
        peer: _Peer,
        method: str,
        params: Mapping[str, object],
        *,
        timeout: float | None,
    ) -> Mapping[str, object]:
        self._require_same_peer(peer)
        callback_id = self._next_callback_id()
        frame = {"type": "request", "id": callback_id, "method": method, "params": dict(params)}
        validate_callback_request(frame)
        encoded = protocol.encode(frame)
        if len(encoded) > peer.frame_limit:
            raise ValueError("provider callback request exceeds the public frame limit")
        future = asyncio.get_running_loop().create_future()
        pending = _Pending(method=method, params=dict(params), future=future)
        peer.pending[callback_id] = pending
        try:
            async with peer.write_lock:
                self._require_same_peer(peer)
                assert peer.writer is not None
                if CALLBACK_CATALOG[method].mutating:
                    # StreamWriter.write may hand bytes to the transport before
                    # drain reports a broken connection.  From this point a
                    # mutating callback can no longer be proved unexecuted.
                    pending.dispatched = True
                peer.writer.write(encoded)
                await peer.writer.drain()
                pending.dispatched = True
            try:
                return await asyncio.wait_for(
                    asyncio.shield(future),
                    peer.callback_timeout
                    if timeout is None
                    else min(timeout, peer.callback_timeout),
                )
            except TimeoutError as exc:
                if CALLBACK_CATALOG[method].mutating and pending.dispatched:
                    raise CallbackOutcomeUnknown(
                        peer.provider_id, callback_id, "callback_timeout"
                    ) from exc
                raise ProviderUnavailable(peer.provider_id, "callback_timeout") from exc
        except (ConnectionError, OSError) as exc:
            settled_before_disconnect = future.done()
            self.disconnect(peer.provider_id, peer.generation, token=peer.token)
            if settled_before_disconnect:
                return future.result()
            if future.done() and not future.cancelled():
                future.exception()
            if pending.dispatched and CALLBACK_CATALOG[method].mutating:
                raise CallbackOutcomeUnknown(
                    peer.provider_id, callback_id, "callback_disconnected"
                ) from exc
            raise ProviderUnavailable(peer.provider_id, "callback_disconnected") from exc
        finally:
            if pending.dispatched and not future.done() and not peer.closed:
                peer.abandoned[callback_id] = None
                while len(peer.abandoned) > peer.pending_limit * 4:
                    peer.abandoned.popitem(last=False)
            peer.pending.pop(callback_id, None)

    def disconnect(self, provider_id: str, generation: int, *, token: str | None = None) -> None:
        peer = self._peers.get(provider_id)
        if (
            peer is None
            or peer.generation != generation
            or (token is not None and peer.token != token)
        ):
            return
        self._deactivate(peer, "callback_disconnected")

    async def aclose(self) -> None:
        peers = tuple(self._peers.values())
        lease_tasks = tuple(peer.lease_task for peer in peers if peer.lease_task is not None)
        for peer in peers:
            self._deactivate(peer, "service_stopped")
        if lease_tasks:
            await asyncio.gather(*lease_tasks, return_exceptions=True)
        for peer in peers:
            writer = peer.writer
            if writer is not None:
                with contextlib.suppress(BaseException):
                    await writer.wait_closed()

    async def _watch_lease(self, peer: _Peer) -> None:
        try:
            while self._peers.get(peer.provider_id) is peer and not peer.closed:
                remaining = peer.lease_deadline - self._monotonic()
                if remaining <= 0:
                    self._deactivate(peer, "lease_expired")
                    return
                await asyncio.sleep(remaining)
        except asyncio.CancelledError:
            return

    async def _receive(self, peer: _Peer, line: bytes) -> None:
        if len(line) > peer.frame_limit:
            raise ValueError("provider callback response exceeds the negotiated frame limit")
        try:
            frame = json.loads(line)
        except (UnicodeError, ValueError) as exc:
            raise ValueError("invalid provider callback JSON") from exc
        if not isinstance(frame, Mapping) or frame.get("type") != "response":
            if isinstance(frame, Mapping) and isinstance(frame.get("method"), str):
                request_id = frame.get("id")
                correlated = request_id if type(request_id) is int and request_id > 0 else 0
                encoded = protocol.err(
                    correlated,
                    "wrong_connection_role",
                    "a provider callback connection does not accept ordinary RPC requests",
                )
                async with peer.write_lock:
                    self._require_same_peer(peer)
                    assert peer.writer is not None
                    peer.writer.write(encoded)
                    await peer.writer.drain()
                return
            raise ValueError("provider callback connections accept responses only")
        callback_id = frame.get("id")
        if not isinstance(callback_id, str):
            raise TypeError("provider callback response requires a string id")
        pending = peer.pending.get(callback_id)
        if pending is None:
            if callback_id in peer.abandoned:
                peer.abandoned.pop(callback_id, None)
                return
            raise ValueError("provider callback response has no pending request")
        validate_callback_response(pending.method, frame)
        if "error" in frame:
            error = frame["error"]
            assert isinstance(error, Mapping)
            pending.future.set_exception(ProviderCallbackRejected(error))
            return
        result = frame.get("result")
        if not isinstance(result, Mapping):
            raise TypeError("provider callback result must be an object")
        self._validate_result(peer, pending, result)
        pending.future.set_result(dict(result))

    def _current_peer(self, provider_id: str) -> _Peer | None:
        peer = self._peers.get(provider_id)
        if peer is None or peer.closed:
            return None
        if self._monotonic() >= peer.lease_deadline:
            self._deactivate(peer, "lease_expired")
            return None
        return peer

    def _require_current(self, provider_id: str, generation: int) -> _Peer:
        peer = self._current_peer(provider_id)
        if peer is None:
            record = self._store.providers.get(provider_id)
            if record is not None and record.generation != generation:
                raise StaleGeneration(provider_id, generation)
            raise ProviderUnavailable(provider_id, "offline")
        if peer.generation != generation:
            raise StaleGeneration(provider_id, generation)
        return peer

    def _require_same_peer(self, peer: _Peer) -> None:
        current = self._require_current(peer.provider_id, peer.generation)
        if current is not peer:
            raise StaleGeneration(peer.provider_id, peer.generation)

    def _deactivate(self, peer: _Peer, reason: str) -> None:
        if peer.closed:
            return
        peer.closed = True
        peer.state = "offline"
        if self._peers.get(peer.provider_id) is peer:
            self._peers.pop(peer.provider_id, None)
        for callback_id, pending in tuple(peer.pending.items()):
            if pending.future.done():
                continue
            error: Exception
            if pending.dispatched and CALLBACK_CATALOG[pending.method].mutating:
                error = CallbackOutcomeUnknown(peer.provider_id, callback_id, reason)
            else:
                error = ProviderUnavailable(peer.provider_id, reason)
            pending.future.set_exception(error)
        if peer.writer is not None:
            peer.writer.close()
        lease_task = peer.lease_task
        if lease_task is not None and lease_task is not asyncio.current_task():
            lease_task.cancel()
        try:
            timestamp = self._wall_clock()
            with self._store.write_unit() as unit:
                record = self._store.providers.get(peer.provider_id, connection=unit.connection)
                if record is not None and record.generation == peer.generation:
                    changed_bindings = self._store.terminal_bindings.mark_provider_health(
                        peer.provider_id,
                        health="offline",
                        updated_at=timestamp,
                        connection=unit.connection,
                    )
                    revision = self._store.journal.current_sequence(connection=unit.connection) + 1
                    events = [
                        provider_event(record, "offline", timestamp, revision=revision),
                        catalog_invalidated_event(
                            peer.provider_id,
                            revision=revision + 1,
                            recorded_at=timestamp,
                            reason="provider_offline",
                        ),
                    ]
                    for participant_id in changed_bindings:
                        binding = self._store.terminal_bindings.get(
                            participant_id, connection=unit.connection
                        )
                        assert binding is not None
                        events.append(
                            terminal_binding_event(
                                self._store,
                                binding,
                                unit.connection,
                                revision=revision + len(events),
                                recorded_at=timestamp,
                            )
                        )
                    self._store.journal.append_group(
                        unit,
                        events,
                    )
        except Exception:
            logger.exception("could not journal provider disconnect")

    def _next_callback_id(self) -> str:
        self._callback_sequence += 1
        return f"callback-{self._callback_sequence}-{self._id_factory()}"[:512]

    @staticmethod
    def _integer_limit(limits: Mapping[str, object], name: str, maximum: int) -> int:
        value = limits.get(name, maximum)
        return value if type(value) is int and 1 <= value <= maximum else maximum

    @staticmethod
    def _numeric_limit(limits: Mapping[str, object], name: str, maximum: float) -> float:
        value = limits.get(name, maximum)
        if (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(float(value))
            and 0 < value <= maximum
        ):
            return float(value)
        return maximum

    @staticmethod
    def _validate_target(method: str, generation: int, params: Mapping[str, object]) -> None:
        if params.get("provider_generation") != generation:
            actual = params.get("provider_generation")
            raise StaleGeneration("callback-target", actual if type(actual) is int else -1)
        if CALLBACK_CATALOG[method].mutating:
            if not isinstance(params.get("operation_id"), str):
                raise ValueError("mutating callbacks require an operation id")
            if method != "terminal.create" and not all(
                isinstance(params.get(key), str)
                for key in ("terminal_id", "terminal_incarnation", "expected_occupant")
            ):
                raise ValueError("terminal mutations require an exact terminal identity")

    @staticmethod
    def _terminal_key(method: str, params: Mapping[str, object]) -> tuple[str, str] | None:
        if not CALLBACK_CATALOG[method].mutating:
            return None
        if method == "terminal.create":
            return "launch", str(params["participant_id"])
        return "terminal", str(params["terminal_id"])

    @staticmethod
    def _validate_result(peer: _Peer, pending: _Pending, result: Mapping[str, object]) -> None:
        if result.get("provider_generation") != peer.generation:
            actual = result.get("provider_generation")
            raise StaleGeneration(peer.provider_id, actual if type(actual) is int else -1)
        params = pending.params
        if CALLBACK_CATALOG[pending.method].mutating and result.get("operation_id") != params.get(
            "operation_id"
        ):
            raise ValueError("provider callback result changed the operation id")
        if pending.method == "terminal.create" and result.get("outcome") == "accepted":
            terminal = result.get("terminal")
            if isinstance(terminal, Mapping) and (
                terminal.get("provider_id") != peer.provider_id
                or terminal.get("provider_generation") != peer.generation
            ):
                raise ValueError("provider create result changed the provider identity")
        if pending.method in {
            "terminal.deliver",
            "terminal.interrupt",
            "terminal.terminate",
        } and (
            result.get("terminal_id") != params.get("terminal_id")
            or result.get("terminal_incarnation") != params.get("terminal_incarnation")
        ):
            raise ValueError("provider callback result changed the terminal identity")
        if pending.method == "terminal.inventory":
            terminals = result.get("terminals")
            assert isinstance(terminals, list)
            if any(
                terminal.get("provider_id") != peer.provider_id
                or terminal.get("provider_generation") != peer.generation
                for terminal in terminals
                if isinstance(terminal, Mapping)
            ):
                raise ValueError("provider inventory result changed the provider identity")
        if pending.method == "terminal.inspect":
            terminal = result.get("terminal")
            assert isinstance(terminal, Mapping)
            if (
                terminal.get("provider_id") != peer.provider_id
                or terminal.get("provider_generation") != peer.generation
                or terminal.get("terminal_id") != params.get("terminal_id")
                or terminal.get("terminal_incarnation") != params.get("terminal_incarnation")
            ):
                raise ValueError("provider inspect result changed the terminal identity")


__all__ = [
    "CALLBACK_TIMEOUT_SECONDS",
    "PENDING_CALLBACK_LIMIT",
    "PROVIDER_LEASE_SECONDS",
    "CallbackOutcomeUnknown",
    "ProviderBusy",
    "ProviderCallbackRejected",
    "ProviderConnectionService",
    "ProviderUnavailable",
    "StaleGeneration",
]
