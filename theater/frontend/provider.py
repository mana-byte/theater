"""Connect-only terminal-provider callback channel for the public frontend API."""

from __future__ import annotations

import asyncio
import contextlib
import math
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from jsonschema.exceptions import ValidationError

from theater.frontend.capabilities import (
    CALLBACK_CATALOG,
    MAX_EXACT_JSON_INTEGER,
    MAX_FRAME_BYTES,
    PUBLIC_API_MAJOR,
    PUBLIC_API_MINOR,
    PUBLIC_LIMITS,
    TERMINAL_PROVIDER_CAPABILITY,
)
from theater.frontend.dto import HandshakeResult, Response
from theater.frontend.schemas import (
    validate_callback_request,
    validate_callback_response,
    validate_public_request,
    validate_public_response,
    validator_for,
)
from theater.frontend.transport import _encode_frame, _FrameEOF, _FrameError, _FrameReader

_CALLBACK_RESPONSE_SCHEMA_ID = next(iter(CALLBACK_CATALOG.values())).response_schema_id
_DEFAULT_HANDSHAKE_TIMEOUT = float(PUBLIC_LIMITS["handshake_timeout_seconds"])
_DEFAULT_CALLBACK_TIMEOUT = float(PUBLIC_LIMITS["provider_callback_timeout_seconds"])
_DEFAULT_LEASE_SECONDS = float(PUBLIC_LIMITS["provider_lease_seconds"])
_DEFAULT_PENDING_CALLBACKS = PUBLIC_LIMITS["provider_pending_callbacks"]
_COMPLETED_RESPONSE_FACTOR = 4


class ProviderClientError(RuntimeError):
    """Base error for a terminal-provider callback connection."""


class ProviderConnectionError(ProviderClientError):
    """The explicit callback socket could not be connected or retained."""


class ProviderHandshakeError(ProviderClientError):
    """The peer did not accept a valid provider callback handshake."""


class ProviderHandshakeRefused(ProviderHandshakeError):
    """The daemon refused the provider callback handshake."""

    def __init__(self, response: Response) -> None:
        assert response.error is not None
        super().__init__(f"{response.error.code}: {response.error.message}")
        self.response = response


class ProviderProtocolError(ProviderClientError):
    """The callback peer sent an uncorrelatable or malformed frame."""


class ProviderGenerationError(ProviderClientError):
    """A local lease update referred to an inactive provider generation."""


@dataclass(frozen=True, slots=True)
class ProviderClientConfig:
    """Stable identity and deadline facts for one explicit callback connection."""

    socket_path: str
    client_id: str
    provider_id: str
    provider_credential: str = field(repr=False)
    required_capabilities: tuple[str, ...] = (TERMINAL_PROVIDER_CAPABILITY,)
    handshake_timeout: float = _DEFAULT_HANDSHAKE_TIMEOUT
    callback_timeout: float = _DEFAULT_CALLBACK_TIMEOUT
    lease_seconds: float = _DEFAULT_LEASE_SECONDS
    pending_callbacks: int = _DEFAULT_PENDING_CALLBACKS


@dataclass(frozen=True, slots=True)
class CallbackRequest:
    """One validated daemon-to-provider reverse request."""

    callback_id: str
    method: str
    params: Mapping[str, object]
    provider_generation: int


@dataclass(frozen=True, slots=True)
class CallbackResponse:
    """A handler result or structured refusal, never an arbitrary envelope."""

    result: Mapping[str, object] | None = None
    error: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if (self.result is None) == (self.error is None):
            raise ValueError("callback response must contain exactly one result or error")


type CallbackHandler = Callable[
    [CallbackRequest], Awaitable[Mapping[str, object] | CallbackResponse]
]


@dataclass(slots=True)
class _PendingCallback:
    request: CallbackRequest
    epoch: int
    handler_started: bool = False
    responded: bool = False
    response: dict[str, object] | None = None


@dataclass(slots=True)
class _TerminalGate:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class ProviderClient:
    """Provider-side callback dispatcher; it connects only to its supplied socket."""

    def __init__(
        self,
        socket_path: str | Path,
        *,
        client_id: str,
        provider_id: str,
        provider_credential: str,
        handlers: Mapping[str, CallbackHandler] | None = None,
        required_capabilities: Sequence[str] = (TERMINAL_PROVIDER_CAPABILITY,),
        handshake_timeout: float = _DEFAULT_HANDSHAKE_TIMEOUT,
        callback_timeout: float = _DEFAULT_CALLBACK_TIMEOUT,
        lease_seconds: float = _DEFAULT_LEASE_SECONDS,
        pending_callbacks: int = _DEFAULT_PENDING_CALLBACKS,
    ) -> None:
        _require_identifier(client_id, "client_id")
        _require_identifier(provider_id, "provider_id")
        _require_identifier(provider_credential, "provider_credential")
        capabilities = tuple(required_capabilities)
        if TERMINAL_PROVIDER_CAPABILITY not in capabilities:
            capabilities = (*capabilities, TERMINAL_PROVIDER_CAPABILITY)
        if any(not isinstance(capability, str) or not capability for capability in capabilities):
            raise ValueError("required_capabilities must contain non-empty strings")
        _require_positive_seconds(handshake_timeout, "handshake_timeout")
        _require_positive_seconds(callback_timeout, "callback_timeout")
        _require_positive_seconds(lease_seconds, "lease_seconds")
        if (
            type(pending_callbacks) is not int
            or not 1 <= pending_callbacks <= _DEFAULT_PENDING_CALLBACKS
        ):
            raise ValueError(
                f"pending_callbacks must be an integer from 1 through {_DEFAULT_PENDING_CALLBACKS}"
            )
        self._config = ProviderClientConfig(
            socket_path=str(socket_path),
            client_id=client_id,
            provider_id=provider_id,
            provider_credential=provider_credential,
            required_capabilities=capabilities,
            handshake_timeout=float(handshake_timeout),
            callback_timeout=float(callback_timeout),
            lease_seconds=float(lease_seconds),
            pending_callbacks=pending_callbacks,
        )
        self._handlers: dict[str, CallbackHandler] = {}
        if handlers is not None:
            for method, handler in handlers.items():
                self.set_handler(method, handler)

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._frame_reader: _FrameReader | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._connect_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()
        self._closed_event = asyncio.Event()
        self._closed_event.set()
        self._epoch = 0
        self._active = False
        self._generation_active = False
        self._provider_generation: int | None = None
        self._lease_deadline = 0.0
        self._frame_limit = MAX_FRAME_BYTES
        self._callback_timeout = self._config.callback_timeout
        self._lease_seconds = self._config.lease_seconds
        self._pending_limit = self._config.pending_callbacks
        self._handshake_result: HandshakeResult | None = None
        self._handshake_response: Response | None = None
        self._last_error: ProviderClientError | None = None
        self._pending: dict[tuple[int, str], _PendingCallback] = {}
        self._completed: OrderedDict[str, dict[str, object]] = OrderedDict()
        self._seen_callback_ids: set[str] = set()
        self._terminal_locks: dict[tuple[str, ...], _TerminalGate] = {}
        self._tasks: set[asyncio.Task[None]] = set()

    @property
    def config(self) -> ProviderClientConfig:
        return self._config

    @property
    def connected(self) -> bool:
        return self._active and self._reader is not None and self._writer is not None

    @property
    def generation_active(self) -> bool:
        return (
            self.connected and self._generation_active and time.monotonic() < self._lease_deadline
        )

    @property
    def provider_generation(self) -> int | None:
        return self._provider_generation

    @property
    def handshake_result(self) -> HandshakeResult | None:
        return self._handshake_result

    @property
    def handshake_response(self) -> Response | None:
        return self._handshake_response

    @property
    def last_error(self) -> ProviderClientError | None:
        return self._last_error

    @property
    def pending_callbacks(self) -> int:
        return sum(1 for epoch, _ in self._pending if epoch == self._epoch)

    def set_handler(self, method: str, handler: CallbackHandler) -> None:
        """Install one asynchronous handler for a frozen terminal callback method."""
        if method not in CALLBACK_CATALOG:
            raise KeyError(f"method is not in the frozen callback catalog: {method!r}")
        if not callable(handler):
            raise TypeError("callback handler must be callable")
        self._handlers[method] = handler

    def renew_lease(self, *, generation: int | None = None) -> None:
        """Record a separately authenticated heartbeat for this active generation."""
        expected = self._provider_generation if generation is None else generation
        if (
            type(expected) is not int
            or not self.connected
            or not self._generation_active
            or time.monotonic() >= self._lease_deadline
            or expected != self._provider_generation
        ):
            raise ProviderGenerationError("cannot renew an inactive provider callback generation")
        self._lease_deadline = time.monotonic() + self._lease_seconds

    def invalidate_generation(self, *, generation: int | None = None) -> None:
        """Fence queued callbacks when the provider learns that its generation is lost."""
        if generation is None or generation == self._provider_generation:
            self._generation_active = False
            self._lease_deadline = 0.0

    async def connect(self) -> HandshakeResult:
        """Open, authenticate, and begin reading one callback connection without autostart."""
        async with self._connect_lock:
            if self.connected:
                assert self._handshake_result is not None
                return self._handshake_result
            self._epoch += 1
            epoch = self._epoch
            self._completed.clear()
            self._seen_callback_ids.clear()
            self._last_error = None
            self._closed_event.clear()
            try:
                (
                    reader,
                    writer,
                    frame_reader,
                    response,
                    result,
                    settings,
                ) = await self._open_and_negotiate()
            except BaseException:
                self._closed_event.set()
                raise

            self._reader = reader
            self._writer = writer
            self._frame_reader = frame_reader
            self._frame_limit = settings.frame_limit
            self._callback_timeout = settings.callback_timeout
            self._lease_seconds = settings.lease_seconds
            self._pending_limit = settings.pending_callbacks
            frame_reader.set_limit(settings.frame_limit)
            self._handshake_response = response
            self._handshake_result = result
            self._provider_generation = result.provider_generation
            self._generation_active = True
            self._lease_deadline = time.monotonic() + self._lease_seconds
            self._active = True
            self._reader_task = asyncio.create_task(
                self._reader_loop(epoch, reader, frame_reader),
                name="theater-provider-callback-reader",
            )
            return result

    async def _open_and_negotiate(
        self,
    ) -> tuple[
        asyncio.StreamReader,
        asyncio.StreamWriter,
        _FrameReader,
        Response,
        HandshakeResult,
        _NegotiatedSettings,
    ]:
        try:
            reader, writer = await asyncio.open_unix_connection(
                self._config.socket_path, limit=MAX_FRAME_BYTES
            )
        except OSError as exc:
            raise ProviderConnectionError(
                "could not connect to the explicit provider socket "
                f"{self._config.socket_path}: {exc}"
            ) from exc
        frame_reader = _FrameReader(limit=MAX_FRAME_BYTES)
        try:
            await self._write_handshake(writer)
            try:
                async with asyncio.timeout(self._config.handshake_timeout):
                    raw_response = await frame_reader.read(reader)
            except TimeoutError as exc:
                raise ProviderHandshakeError("provider callback handshake timed out") from exc
            except _FrameError as exc:
                raise ProviderHandshakeError(
                    f"invalid provider callback handshake frame: {exc}"
                ) from exc
            except (OSError, ConnectionError) as exc:
                raise ProviderHandshakeError(
                    f"provider callback handshake connection failed: {exc}"
                ) from exc
            response, result = self._decode_handshake(raw_response)
            return (
                reader,
                writer,
                frame_reader,
                response,
                result,
                _negotiated_settings(result, self._config),
            )
        except BaseException:
            writer.close()
            with contextlib.suppress(OSError, ConnectionError):
                await writer.wait_closed()
            raise

    async def close(self) -> None:
        """Close the callback connection without cancelling a started physical side effect."""
        async with self._connect_lock:
            epoch = self._epoch
            reader_task = self._reader_task
            writer = self._deactivate(epoch, ProviderConnectionError("provider client closed"))
            if reader_task is not None and reader_task is not asyncio.current_task():
                reader_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, OSError, ConnectionError):
                    await reader_task
            if writer is not None:
                with contextlib.suppress(OSError, ConnectionError):
                    await writer.wait_closed()

    async def wait_closed(self) -> None:
        """Wait until the current callback connection is no longer usable."""
        await self._closed_event.wait()

    async def __aenter__(self) -> ProviderClient:
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def _write_handshake(self, writer: asyncio.StreamWriter) -> None:
        request = {
            "id": 1,
            "method": "frontend.handshake",
            "params": {
                "api": {"major": PUBLIC_API_MAJOR, "minor": PUBLIC_API_MINOR},
                "client_id": self._config.client_id,
                "role": "provider",
                "channel": "callback",
                "required_capabilities": list(self._config.required_capabilities),
                "provider_id": self._config.provider_id,
                "provider_credential": self._config.provider_credential,
            },
        }
        try:
            validate_public_request(request)
            writer.write(_encode_frame(request, limit=MAX_FRAME_BYTES))
            await writer.drain()
        except (
            ValidationError,
            ValueError,
            TypeError,
            OSError,
            ConnectionError,
            _FrameError,
        ) as exc:
            raise ProviderHandshakeError(
                f"could not write provider callback handshake: {exc}"
            ) from exc

    def _decode_handshake(
        self, raw_response: Mapping[str, object]
    ) -> tuple[Response, HandshakeResult]:
        try:
            validate_public_response("frontend.handshake", raw_response)
        except (ValidationError, ValueError, TypeError) as exc:
            raise ProviderHandshakeError(
                f"invalid provider callback handshake response: {exc}"
            ) from exc
        response_id = raw_response.get("id")
        if type(response_id) is not int or not 1 <= response_id <= MAX_EXACT_JSON_INTEGER:
            raise ProviderHandshakeError("provider callback handshake response id is not exact")
        if response_id != 1:
            raise ProviderHandshakeError(
                f"provider callback handshake response id {response_id} does not match request id 1"
            )
        try:
            response = Response.from_wire(raw_response)
        except (TypeError, ValueError) as exc:
            raise ProviderHandshakeError(
                f"invalid provider callback handshake envelope: {exc}"
            ) from exc
        if not response.ok:
            raise ProviderHandshakeRefused(response)
        try:
            result = HandshakeResult.from_wire(response.result)
        except (TypeError, ValueError) as exc:
            raise ProviderHandshakeError(
                f"invalid provider callback handshake result: {exc}"
            ) from exc
        if (result.api.major, result.api.minor) != (PUBLIC_API_MAJOR, PUBLIC_API_MINOR):
            raise ProviderHandshakeError(
                f"server negotiated unsupported public API {result.api.major}.{result.api.minor}"
            )
        missing = set(self._config.required_capabilities).difference(result.capabilities)
        if missing:
            raise ProviderHandshakeError(
                f"server omitted required provider capability: {', '.join(sorted(missing))}"
            )
        if type(result.provider_generation) is not int or result.provider_generation < 0:
            raise ProviderHandshakeError("provider callback handshake did not acquire a generation")
        return response, result

    async def _reader_loop(
        self, epoch: int, reader: asyncio.StreamReader, frame_reader: _FrameReader
    ) -> None:
        failure: ProviderClientError | None = None
        try:
            while self._is_epoch_connected(epoch):
                frame = await frame_reader.read(reader)
                await self._handle_frame(epoch, frame)
        except asyncio.CancelledError:
            return
        except _FrameEOF as exc:
            failure = ProviderConnectionError(f"provider callback connection closed: {exc}")
        except _FrameError as exc:
            failure = ProviderProtocolError(f"invalid provider callback frame: {exc}")
        except (OSError, ConnectionError) as exc:
            failure = ProviderConnectionError(f"provider callback connection failed: {exc}")
        except ProviderClientError as exc:
            failure = exc
        finally:
            if self._is_epoch_connected(epoch):
                self._deactivate(
                    epoch,
                    failure or ProviderConnectionError("provider callback reader stopped"),
                )

    async def _handle_frame(self, epoch: int, frame: Mapping[str, object]) -> None:
        frame_type = frame.get("type")
        if frame_type == "request":
            await self._handle_request(epoch, frame)
            return
        if frame_type == "response":
            try:
                validator_for(_CALLBACK_RESPONSE_SCHEMA_ID).validate(frame)
            except ValidationError as exc:
                raise ProviderProtocolError(f"invalid callback response envelope: {exc}") from exc
            response_id = frame.get("id")
            if _valid_callback_id(response_id):
                raise ProviderProtocolError(
                    f"unexpected or late callback response {response_id!r} on provider connection"
                )
            raise ProviderProtocolError(
                "provider callback response does not contain a valid string id"
            )
        raise ProviderProtocolError("provider callback frame type must be request or response")

    async def _handle_request(self, epoch: int, frame: Mapping[str, object]) -> None:
        callback_id = frame.get("id")
        method = frame.get("method")
        if not _valid_callback_id(callback_id):
            raise ProviderProtocolError(
                "provider callback request does not contain a valid string id"
            )
        assert isinstance(callback_id, str)
        if not isinstance(method, str) or method not in CALLBACK_CATALOG:
            await self._send_error(
                epoch,
                callback_id,
                "unknown_method",
                "The terminal provider does not implement this callback method.",
            )
            return
        try:
            validate_callback_request(frame)
        except (ValidationError, ValueError, TypeError, KeyError):
            await self._send_error(
                epoch,
                callback_id,
                "bad_request",
                "The terminal callback request is invalid.",
            )
            return
        params = frame.get("params")
        assert isinstance(params, Mapping)
        generation = params.get("provider_generation")
        if type(generation) is not int:
            raise ProviderProtocolError("validated callback generation is not an exact integer")
        request = CallbackRequest(
            callback_id=callback_id,
            method=method,
            params=MappingProxyType(dict(params)),
            provider_generation=generation,
        )
        key = (epoch, callback_id)
        existing = self._pending.get(key)
        if existing is not None:
            return
        completed = self._completed.get(callback_id)
        if completed is not None:
            await self._send_frame(epoch, method, completed)
            return
        if callback_id in self._seen_callback_ids:
            await self._send_error(
                epoch,
                callback_id,
                "duplicate_callback",
                "The callback id was already completed and its cached response has expired.",
            )
            return
        if request.provider_generation != self._provider_generation or not self._generation_usable(
            epoch
        ):
            await self._send_error(
                epoch,
                callback_id,
                "stale_generation",
                "The callback generation is no longer active on this provider connection.",
            )
            return
        if self.pending_callbacks >= self._pending_limit:
            await self._send_error(
                epoch,
                callback_id,
                "provider_busy",
                "The terminal provider callback queue is full before dispatch.",
            )
            return
        record = _PendingCallback(request=request, epoch=epoch)
        self._seen_callback_ids.add(callback_id)
        self._pending[key] = record
        task = asyncio.create_task(
            self._run_callback(record), name=f"theater-provider-callback-{callback_id}"
        )
        self._tasks.add(task)
        task.add_done_callback(self._finished_task)

    async def _run_callback(self, record: _PendingCallback) -> None:
        key = (record.epoch, record.request.callback_id)
        try:
            terminal_key = _mutation_key(self._config.provider_id, record.request)
            if terminal_key is None:
                await self._dispatch_callback(record)
            else:
                gate = self._terminal_locks.setdefault(terminal_key, _TerminalGate())
                gate.users += 1
                try:
                    async with gate.lock:
                        await self._dispatch_callback(record)
                finally:
                    gate.users -= 1
                    if gate.users == 0 and self._terminal_locks.get(terminal_key) is gate:
                        self._terminal_locks.pop(terminal_key)
        finally:
            self._pending.pop(key, None)

    async def _dispatch_callback(self, record: _PendingCallback) -> None:
        request = record.request
        if not self._can_start(record):
            if self._is_epoch_connected(record.epoch):
                await self._complete(
                    record,
                    _error_frame(
                        request.callback_id,
                        "stale_generation",
                        "The callback was fenced before its terminal side effect began.",
                    ),
                )
            return
        handler = self._handlers.get(request.method)
        if handler is None:
            await self._complete(
                record,
                _error_frame(
                    request.callback_id,
                    "provider_unavailable",
                    "The terminal provider has no handler for this callback method.",
                ),
            )
            return
        record.handler_started = True
        try:
            handler_task: asyncio.Future[Mapping[str, object] | CallbackResponse] = (
                asyncio.ensure_future(handler(request))
            )
        except Exception:
            await self._complete(
                record,
                _uncertain_or_error_frame(
                    request,
                    "internal",
                    "The terminal provider could not start its callback handler.",
                ),
            )
            return
        done, _ = await asyncio.wait({handler_task}, timeout=self._callback_timeout)
        if not done:
            await self._complete(record, _timeout_frame(request))
            await self._consume_started_handler(handler_task)
            return
        try:
            payload = handler_task.result()
            frame = _response_from_payload(request, payload)
        except asyncio.CancelledError:
            frame = _uncertain_or_error_frame(
                request, "callback_cancelled", "The callback handler stopped."
            )
        except (TypeError, ValueError, ValidationError):
            frame = _uncertain_or_error_frame(
                request,
                "internal",
                "The callback handler returned an invalid response.",
            )
        except Exception:
            frame = _uncertain_or_error_frame(
                request,
                "internal",
                "The terminal provider callback handler failed after dispatch.",
            )
        await self._complete(record, frame)

    async def _consume_started_handler(
        self, handler_task: asyncio.Future[Mapping[str, object] | CallbackResponse]
    ) -> None:
        """Keep a timed-out side effect serialized until its handler actually exits."""
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await asyncio.shield(handler_task)

    async def _complete(self, record: _PendingCallback, frame: dict[str, object]) -> None:
        if record.responded or record.epoch != self._epoch:
            return
        if (
            record.handler_started
            and CALLBACK_CATALOG[record.request.method].mutating
            and not self._can_start(record)
        ):
            response = _unknown_frame(
                record.request,
                "stale_generation",
                "The callback generation changed after terminal work may have started.",
            )
        else:
            response = _safe_handler_response(record.request, frame)
        response = _validated_response(record.request.method, record.request.callback_id, response)
        fitted_response = await self._fit_response(
            record.epoch,
            record.request.method,
            record.request.callback_id,
            response,
            request=record.request,
        )
        if fitted_response is None:
            return
        record.response = fitted_response
        record.responded = True
        self._remember_response(record.request.callback_id, fitted_response)
        await self._send_frame(record.epoch, record.request.method, fitted_response)

    async def _fit_response(
        self,
        epoch: int,
        method: str,
        callback_id: str,
        frame: dict[str, object],
        *,
        request: CallbackRequest | None = None,
    ) -> dict[str, object] | None:
        try:
            _encode_frame(frame, limit=self._frame_limit)
        except _FrameError:
            if request is not None and CALLBACK_CATALOG[request.method].mutating:
                fallback = _unknown_frame(
                    request,
                    "too_large",
                    "The callback result exceeds the negotiated frame limit after work may have "
                    "started.",
                )
            else:
                fallback = _error_frame(
                    callback_id,
                    "too_large",
                    "The terminal provider callback response exceeds the negotiated frame limit.",
                )
            fallback = _validated_response(method, callback_id, fallback)
            try:
                _encode_frame(fallback, limit=self._frame_limit)
            except _FrameError as exc:
                self._deactivate(
                    epoch,
                    ProviderProtocolError(f"cannot encode bounded callback refusal: {exc}"),
                )
                return None
            return fallback
        else:
            return frame

    async def _send_error(self, epoch: int, callback_id: str, code: str, message: str) -> None:
        frame = _validated_response(
            None,
            callback_id,
            _error_frame(callback_id, code, message),
        )
        fitted_frame = await self._fit_response(epoch, "terminal.inventory", callback_id, frame)
        if fitted_frame is not None:
            await self._send_frame(epoch, None, fitted_frame)

    async def _send_frame(
        self, epoch: int, method: str | None, frame: Mapping[str, object]
    ) -> bool:
        if not self._is_epoch_connected(epoch):
            return False
        try:
            encoded = _encode_frame(frame, limit=self._frame_limit)
        except _FrameError as exc:
            self._deactivate(
                epoch, ProviderProtocolError(f"invalid outbound callback frame: {exc}")
            )
            return False
        async with self._write_lock:
            if not self._is_epoch_connected(epoch):
                return False
            writer = self._writer
            if writer is None:
                return False
            try:
                writer.write(encoded)
                await writer.drain()
            except (OSError, ConnectionError) as exc:
                self._deactivate(
                    epoch,
                    ProviderConnectionError(f"provider callback response write failed: {exc}"),
                )
                return False
        return True

    def _remember_response(self, callback_id: str, frame: dict[str, object]) -> None:
        self._completed[callback_id] = frame
        self._completed.move_to_end(callback_id)
        while len(self._completed) > self._pending_limit * _COMPLETED_RESPONSE_FACTOR:
            self._completed.popitem(last=False)

    def _can_start(self, record: _PendingCallback) -> bool:
        return (
            self._generation_usable(record.epoch)
            and record.request.provider_generation == self._provider_generation
        )

    def _generation_usable(self, epoch: int) -> bool:
        return (
            self._is_epoch_connected(epoch)
            and self._generation_active
            and time.monotonic() < self._lease_deadline
        )

    def _is_epoch_connected(self, epoch: int) -> bool:
        return self._active and epoch == self._epoch and self._writer is not None

    def _deactivate(self, epoch: int, error: ProviderClientError) -> asyncio.StreamWriter | None:
        if epoch != self._epoch:
            return None
        self._last_error = error
        self._active = False
        self._generation_active = False
        self._lease_deadline = 0.0
        self._reader = None
        self._frame_reader = None
        self._reader_task = None
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.close()
        self._closed_event.set()
        return writer

    def _finished_task(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        with contextlib.suppress(asyncio.CancelledError):
            failure = task.exception()
            if isinstance(failure, ProviderClientError):
                self._last_error = failure


@dataclass(frozen=True, slots=True)
class _NegotiatedSettings:
    frame_limit: int
    pending_callbacks: int
    callback_timeout: float
    lease_seconds: float


def _negotiated_settings(
    result: HandshakeResult, config: ProviderClientConfig
) -> _NegotiatedSettings:
    limits = result.limits
    frame_limit = _negotiated_integer(
        limits,
        "max_frame_bytes",
        MAX_FRAME_BYTES,
        minimum=1,
        maximum=MAX_FRAME_BYTES,
    )
    pending_callbacks = _negotiated_integer(
        limits,
        "provider_pending_callbacks",
        config.pending_callbacks,
        minimum=1,
        maximum=_DEFAULT_PENDING_CALLBACKS,
    )
    pending_callbacks = min(pending_callbacks, config.pending_callbacks)
    mutations_per_terminal = _negotiated_integer(
        limits,
        "provider_mutations_per_terminal",
        1,
        minimum=1,
        maximum=1,
    )
    assert mutations_per_terminal == 1
    callback_timeout = _negotiated_seconds(
        limits, "provider_callback_timeout_seconds", config.callback_timeout
    )
    lease_seconds = _negotiated_seconds(limits, "provider_lease_seconds", config.lease_seconds)
    return _NegotiatedSettings(
        frame_limit=frame_limit,
        pending_callbacks=pending_callbacks,
        callback_timeout=min(callback_timeout, config.callback_timeout),
        lease_seconds=min(lease_seconds, config.lease_seconds),
    )


def _negotiated_integer(
    limits: Mapping[str, object], name: str, default: int, *, minimum: int, maximum: int
) -> int:
    value = limits.get(name, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ProviderHandshakeError(
            f"handshake limit {name} must be an integer from {minimum} through {maximum}"
        )
    return value


def _negotiated_seconds(limits: Mapping[str, object], name: str, default: float) -> float:
    value = limits.get(name, default)
    _require_positive_seconds(value, f"handshake limit {name}")
    assert isinstance(value, (int, float)) and not isinstance(value, bool)
    return float(value)


def _require_identifier(value: object, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")


def _require_positive_seconds(value: object, name: str) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or value <= 0
    ):
        raise ValueError(f"{name} must be a finite positive number")


def _valid_callback_id(value: object) -> bool:
    return isinstance(value, str) and bool(value) and len(value) <= 512


def _mutation_key(provider_id: str, request: CallbackRequest) -> tuple[str, ...] | None:
    if not CALLBACK_CATALOG[request.method].mutating:
        return None
    params = request.params
    if request.method == "terminal.create":
        participant_id = params.get("participant_id")
        launch_id = params.get("launch_id")
        assert isinstance(participant_id, str)
        assert isinstance(launch_id, str)
        return ("launch", provider_id, participant_id, launch_id)
    terminal_id = params.get("terminal_id")
    terminal_incarnation = params.get("terminal_incarnation")
    assert isinstance(terminal_id, str)
    assert isinstance(terminal_incarnation, str)
    return ("terminal", provider_id, terminal_id, terminal_incarnation)


def _response_from_payload(
    request: CallbackRequest, payload: Mapping[str, object] | CallbackResponse
) -> dict[str, object]:
    if isinstance(payload, CallbackResponse):
        if payload.result is not None:
            return {"type": "response", "id": request.callback_id, "result": dict(payload.result)}
        assert payload.error is not None
        return {"type": "response", "id": request.callback_id, "error": dict(payload.error)}
    if not isinstance(payload, Mapping):
        raise TypeError("callback handler result must be a mapping or CallbackResponse")
    return {"type": "response", "id": request.callback_id, "result": dict(payload)}


def _validated_response(
    method: str | None, callback_id: str, frame: dict[str, object]
) -> dict[str, object]:
    try:
        if method is None:
            validator_for(_CALLBACK_RESPONSE_SCHEMA_ID).validate(frame)
        else:
            validate_callback_response(method, frame)
    except (ValidationError, TypeError, ValueError, KeyError):
        fallback = _error_frame(
            callback_id,
            "internal",
            "The terminal provider produced an invalid callback response.",
        )
        validator_for(_CALLBACK_RESPONSE_SCHEMA_ID).validate(fallback)
        return fallback
    return frame


def _safe_handler_response(request: CallbackRequest, frame: dict[str, object]) -> dict[str, object]:
    try:
        validate_callback_response(request.method, frame)
    except (ValidationError, TypeError, ValueError, KeyError):
        return _uncertain_or_error_frame(
            request,
            "internal",
            "The terminal provider handler returned an invalid callback response.",
        )
    return frame


def _error_frame(callback_id: str, code: str, message: str) -> dict[str, object]:
    return {
        "type": "response",
        "id": callback_id,
        "error": {"code": code, "message": message},
    }


def _timeout_frame(request: CallbackRequest) -> dict[str, object]:
    if CALLBACK_CATALOG[request.method].mutating:
        return _unknown_frame(
            request,
            "callback_timeout",
            "The callback deadline elapsed after terminal work may have started.",
        )
    return _error_frame(
        request.callback_id,
        "callback_timeout",
        "The read-only terminal callback did not finish before its deadline.",
    )


def _uncertain_or_error_frame(
    request: CallbackRequest, code: str, message: str
) -> dict[str, object]:
    if CALLBACK_CATALOG[request.method].mutating:
        return _unknown_frame(request, code, message)
    return _error_frame(request.callback_id, code, message)


def _unknown_frame(request: CallbackRequest, code: str, message: str) -> dict[str, object]:
    params = request.params
    error = {"code": code, "message": message}
    if request.method == "terminal.create":
        result: dict[str, object] = {
            "operation_id": params["operation_id"],
            "provider_generation": params["provider_generation"],
            "outcome": "unknown",
            "error": error,
        }
    else:
        result = {
            "operation_id": params["operation_id"],
            "provider_generation": params["provider_generation"],
            "terminal_id": params["terminal_id"],
            "terminal_incarnation": params["terminal_incarnation"],
            "delivery": "unknown",
            "error": error,
        }
        if request.method == "terminal.terminate":
            result["exit_confirmed"] = False
    return {"type": "response", "id": request.callback_id, "result": result}


__all__ = [
    "CallbackHandler",
    "CallbackRequest",
    "CallbackResponse",
    "ProviderClient",
    "ProviderClientConfig",
    "ProviderClientError",
    "ProviderConnectionError",
    "ProviderGenerationError",
    "ProviderHandshakeError",
    "ProviderHandshakeRefused",
    "ProviderProtocolError",
]
