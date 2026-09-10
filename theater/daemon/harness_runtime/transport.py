"""RFC 6455 WebSocket-over-Unix transport for native harness backends.

This is the daemon-owned implementation of the frozen public ``RuntimeIO`` and
``RuntimeConnection`` seams from ``theater.harness.contracts.runtime``. A
runtime created inside plugin code reaches its native backend only through
those contracts; this module is the shared engine behind them.

Money rules:

* **JSON correlation without a ``jsonrpc`` field.** A message with ``method``
  is a notification (no ``id``) or a server request (``id`` present) — the
  latter is surfaced as a :class:`RuntimeNotification` carrying the id exactly
  as the backend captured it (integer or string, never stringified), and
  Theater never answers it. A message with ``id`` plus ``result``/``error``
  is a reply to one of our requests, matched by exact id type and value, in
  any order. ``initialize``/``initialized`` belong to the plugin dialect and
  are intentionally not spoken here.
* **Everything is bounded.** Frames, assembled messages, the notification
  buffer, and outstanding requests each have a hard cap. Saturating the
  notification buffer is *not* survivable: notifications carry terminal and
  identity evidence, so the connection fails closed with a typed
  ``RuntimeNotificationOverflow`` — pending requests fail, and the public
  notification iterator raises that typed failure (a frozen
  ``RuntimeConnectionError`` subclass) *after* draining the buffered
  evidence, so a plugin holding only the ``RuntimeConnection`` surface cannot
  mistake a lost-evidence stream for a clean end. Ordinary closes still end
  the iterator normally. Nothing is dropped to keep a stream alive that has
  already lost evidence.
* **Typed, frozen-contract failures.** Callers see ``RuntimeConnectionClosed``,
  ``RuntimeRequestTimeout``, ``RuntimeRequestError``, and the narrow
  subclasses defined in ``theater.daemon.harness_runtime.errors``.
* **aclose() closes the connection only.** Backend termination belongs to the
  manager's explicit teardown.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import itertools
import json
import os
import socket
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from theater.daemon.harness_runtime.constants import (
    RUNTIME_ENDPOINT_POLL_INTERVAL_SECONDS,
    RUNTIME_WS_CLOSE_HANDSHAKE_TIMEOUT_SECONDS,
    RUNTIME_WS_HANDSHAKE_HOST,
    RUNTIME_WS_MAX_FRAME_BYTES,
    RUNTIME_WS_MAX_MESSAGE_BYTES,
    RUNTIME_WS_MAX_OUTSTANDING_REQUESTS,
    RUNTIME_WS_RECEIVE_QUEUE_MAX,
)
from theater.daemon.harness_runtime.errors import (
    RuntimeConnectionSaturated,
    RuntimeHandshakeError,
    RuntimeMalformedReply,
    RuntimeNotificationOverflow,
    RuntimePayloadTooLarge,
    RuntimeProtocolError,
)
from theater.daemon.harness_runtime.frames import (
    CLOSE_NORMAL,
    OP_BINARY,
    OP_CLOSE,
    OP_CONTINUATION,
    OP_PING,
    OP_PONG,
    OP_TEXT,
    DecodedFrame,
    FrameDecoder,
    FrameProtocolError,
    encode_frame,
)
from theater.harness.contracts.runtime import (
    RuntimeConnection,
    RuntimeConnectionClosed,
    RuntimeConnectionError,
    RuntimeIO,
    RuntimeNotification,
    RuntimeRequestError,
    RuntimeRequestTimeout,
    validate_native_request_id,
)

#: The RFC 6455 magic GUID appended to Sec-WebSocket-Key for the accept hash.
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


#: The sentinel that ends the notification iterator, carrying the close
#: reason. An ordinary close ends iteration normally; an evidence-gap
#: overflow is raised to the consumer after the buffered evidence is drained,
#: so a caller holding only the frozen ``RuntimeConnection`` surface cannot
#: mistake a lost-evidence stream for a clean end.
class _CloseMarker:
    __slots__ = ("error",)

    def __init__(self, error: RuntimeConnectionError | None = None) -> None:
        self.error = error


@dataclass(slots=True)
class _ReassemblyState:
    """Fragment reassembly for one inbound direction; bounds stay per-message."""

    fragments: list[bytes] = field(default_factory=list)
    length: int = 0
    fragmented: bool = False

    def reset(self) -> None:
        self.fragments = []
        self.length = 0
        self.fragmented = False


def endpoint_to_path(endpoint: str) -> Path:
    """Resolve a runtime endpoint to one unix socket path.

    Accepts ``unix:///abs/path`` (the canonical private-backend form used by
    native plans) or a bare absolute path. Everything else — hostnames, ws://
    URLs, relative paths — is rejected: the engine is deliberately local-only,
    per the "no remote network service" boundary.
    """
    if endpoint.startswith("unix://"):
        raw = endpoint[len("unix://") :]
        if raw.startswith("/"):
            return Path(raw)
        raise RuntimeConnectionError(
            f"runtime endpoint {endpoint!r} must be a unix socket path like unix:///abs/path; "
            "the runtime engine is local-only by design"
        )
    if endpoint.startswith("/"):
        return Path(endpoint)
    raise RuntimeConnectionError(
        f"unsupported runtime endpoint {endpoint!r}: expected unix:///abs/path — the runtime "
        "engine never dials a remote network service"
    )


async def wait_for_unix_endpoint(endpoint: str, *, timeout: float) -> None:
    """Wait until the private endpoint accepts connections, or fail loudly.

    A reachability probe, not a control connection: it opens a bare unix socket,
    confirms the backend is listening, and closes immediately without speaking
    the protocol. Used only on launch and reconnect paths — short-lived history
    reads never reach it.
    """
    path = endpoint_to_path(endpoint)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            if probe.connect_ex(str(path)) == 0:
                return
        finally:
            probe.close()
        if loop.time() >= deadline:
            raise RuntimeConnectionError(
                f"runtime backend endpoint {endpoint} did not accept connections within "
                f"{timeout}s — the detached backend may have failed to start; inspect its "
                "launch log before relaunching anything, and never retry a native mutation"
            )
        await asyncio.sleep(RUNTIME_ENDPOINT_POLL_INTERVAL_SECONDS)


async def _upgrade_handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Perform the HTTP Upgrade handshake and verify the peer's identity proof."""
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        "GET / HTTP/1.1\r\n"
        f"Host: {RUNTIME_WS_HANDSHAKE_HOST}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    writer.write(request.encode("ascii"))
    await writer.drain()
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except asyncio.IncompleteReadError as exc:
        raise RuntimeConnectionClosed(
            "runtime backend closed the connection during the websocket upgrade — it may "
            "not be a WebSocket endpoint"
        ) from exc
    except asyncio.LimitOverrunError as exc:
        raise RuntimeHandshakeError(
            "runtime backend sent oversized handshake headers; refusing the connection"
        ) from exc
    lines = head.decode("latin-1").split("\r\n")
    status_line = lines[0]
    parts = status_line.split(" ", 2)
    if len(parts) < 2 or parts[1] != "101":
        raise RuntimeHandshakeError(
            f"runtime backend refused the websocket upgrade: {status_line!r} — verify the "
            "endpoint speaks WebSocket over this unix socket, not another protocol"
        )
    accept: str | None = None
    upgrade_header: str | None = None
    connection_header: str | None = None
    for line in lines[1:]:
        name, separator, value = line.partition(":")
        if not separator:
            continue
        lowered = name.strip().lower()
        if lowered == "sec-websocket-accept":
            accept = value.strip()
        elif lowered == "upgrade":
            upgrade_header = value.strip()
        elif lowered == "connection":
            connection_header = value.strip()
    if upgrade_header is None or upgrade_header.lower() != "websocket":
        raise RuntimeHandshakeError(
            f"runtime backend did not confirm the websocket upgrade "
            f"(Upgrade: {upgrade_header!r}) — refusing a connection that is not a "
            "WebSocket endpoint"
        )
    connection_tokens = [token.strip().lower() for token in (connection_header or "").split(",")]
    if "upgrade" not in connection_tokens:
        raise RuntimeHandshakeError(
            f"runtime backend's Connection header {(connection_header or '')!r} does not "
            "include the Upgrade token — refusing a connection that is not a switched "
            "WebSocket endpoint"
        )
    expected = base64.b64encode(hashlib.sha1(f"{key}{_WS_GUID}".encode()).digest()).decode("ascii")
    if accept != expected:
        raise RuntimeHandshakeError(
            "runtime backend returned the wrong Sec-WebSocket-Accept — refusing a "
            "connection that is not the endpoint we probed"
        )


@dataclass(frozen=True, slots=True)
class RuntimeTransportStatistics:
    """Surfaced transport counters; degradation is reported, never hidden."""

    endpoint: str
    closed: bool
    close_error: str | None
    outstanding_requests: int
    buffered_notifications: int
    #: Notifications that could not be delivered: the one that saturated the
    #: buffer (which closed the connection) or arrivals racing a closed
    #: connection. The close error names the reason and the obligation.
    dropped_notifications: int
    malformed_messages: int
    unsolicited_replies: int
    skipped_server_requests: int


class JsonRpcRuntimeConnection(RuntimeConnection):
    """One bounded, JSON-correlated connection to a native backend.

    Correlation is exact: our request ids are integers, and a reply matches only
    an integer id of the same value — a string ``"1"`` never satisfies request
    ``1``, because a response must correlate against the captured type. Replies
    may arrive in any order. Server requests and notifications flow through one
    bounded queue; Theater records them and never answers them.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        endpoint: str,
        max_frame_bytes: int = RUNTIME_WS_MAX_FRAME_BYTES,
        max_message_bytes: int = RUNTIME_WS_MAX_MESSAGE_BYTES,
        receive_queue_max: int = RUNTIME_WS_RECEIVE_QUEUE_MAX,
        outstanding_requests_max: int = RUNTIME_WS_MAX_OUTSTANDING_REQUESTS,
        close_handshake_timeout: float = RUNTIME_WS_CLOSE_HANDSHAKE_TIMEOUT_SECONDS,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._endpoint = endpoint
        self._max_frame_bytes = max_frame_bytes
        self._max_message_bytes = max_message_bytes
        self._receive_queue_max = receive_queue_max
        self._outstanding_requests_max = outstanding_requests_max
        self._close_handshake_timeout = close_handshake_timeout
        self._pending: dict[int, asyncio.Future[Mapping[str, object]]] = {}
        # One slot past the buffer bound is reserved for the close marker, so
        # aborting a full queue never has to discard a notification to end
        # the iterator.
        self._notifications: asyncio.Queue[RuntimeNotification | _CloseMarker] = asyncio.Queue(
            maxsize=receive_queue_max + 1
        )
        self._request_ids = itertools.count(1)
        self._send_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._close_sent = False
        self._close_error: RuntimeConnectionError | None = None
        self.dropped_notifications = 0
        self.malformed_messages = 0
        self.unsolicited_replies = 0
        self.skipped_server_requests = 0
        self._reader_task = asyncio.create_task(
            self._receive_loop(), name=f"runtime-ws-recv:{endpoint}"
        )

    # ---- public surface -----------------------------------------------------

    async def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        timeout: float,
    ) -> Mapping[str, object]:
        if self._closed:
            raise self._close_error or RuntimeConnectionClosed(
                f"runtime connection to {self._endpoint} is closed"
            )
        if len(self._pending) >= self._outstanding_requests_max:
            raise RuntimeConnectionSaturated(
                f"runtime connection to {self._endpoint} already has "
                f"{self._outstanding_requests_max} outstanding requests — serialize "
                "controls per participant before dispatching more, instead of piling "
                "unbounded in-flight state onto the backend"
            )
        request_id = next(self._request_ids)
        payload = json.dumps(
            {"id": request_id, "method": method, "params": dict(params)},
            separators=(",", ":"),
        ).encode("utf-8")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Mapping[str, object]] = loop.create_future()
        self._pending[request_id] = future
        try:
            await self._send_message(payload)
            return await asyncio.wait_for(future, timeout)
        except TimeoutError:
            raise RuntimeRequestTimeout(
                f"runtime request {method!r} missed its {timeout}s deadline — delivery is "
                "uncertain: no retry, no tmux fallback; leave the operation to reconciliation"
            ) from None
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: Mapping[str, object]) -> None:
        if self._closed:
            raise self._close_error or RuntimeConnectionClosed(
                f"runtime connection to {self._endpoint} is closed"
            )
        payload = json.dumps(
            {"method": method, "params": dict(params)},
            separators=(",", ":"),
        ).encode("utf-8")
        await self._send_message(payload)

    def notifications(self) -> AsyncIterator[RuntimeNotification]:
        return self._iterate_notifications()

    async def aclose(self) -> None:
        """Close the connection deterministically; the backend stays alive."""
        async with self._close_lock:
            if self._closed:
                return
            await self._send_close_frame()
            try:
                await asyncio.wait_for(
                    asyncio.shield(self._reader_task), self._close_handshake_timeout
                )
            except (TimeoutError, asyncio.CancelledError):
                pass
            except RuntimeConnectionError:
                pass
            self._abort(
                RuntimeConnectionClosed(f"runtime connection to {self._endpoint} was closed")
            )
            if not self._reader_task.done():
                self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)

    def statistics(self) -> RuntimeTransportStatistics:
        """Bounded counters exposing degradation instead of hiding it."""
        close_error = None if self._close_error is None else str(self._close_error)
        return RuntimeTransportStatistics(
            endpoint=self._endpoint,
            closed=self._closed,
            close_error=close_error,
            outstanding_requests=len(self._pending),
            buffered_notifications=self._notifications.qsize(),
            dropped_notifications=self.dropped_notifications,
            malformed_messages=self.malformed_messages,
            unsolicited_replies=self.unsolicited_replies,
            skipped_server_requests=self.skipped_server_requests,
        )

    # ---- notification iteration --------------------------------------------

    async def _iterate_notifications(self) -> AsyncIterator[RuntimeNotification]:
        while True:
            item = await self._notifications.get()
            if isinstance(item, _CloseMarker):
                if isinstance(item.error, RuntimeNotificationOverflow):
                    # The buffered evidence was delivered first; the consumer
                    # now learns, through the frozen surface, that evidence
                    # beyond it was lost and reconciliation is required.
                    raise item.error
                return
            yield item

    # ---- sending -----------------------------------------------------------

    async def _send_message(self, payload: bytes) -> None:
        if len(payload) > self._max_message_bytes:
            raise RuntimePayloadTooLarge(
                f"runtime message of {len(payload)} bytes exceeds the engine bound "
                f"{self._max_message_bytes} — the control service bounds prompts before "
                "dispatch, so a payload this large is a bug, not a prompt"
            )
        async with self._send_lock:
            if self._closed:
                raise self._close_error or RuntimeConnectionClosed(
                    f"runtime connection to {self._endpoint} is closed"
                )
            if len(payload) <= self._max_frame_bytes:
                self._writer.write(encode_frame(OP_TEXT, payload, mask=True))
            else:
                # Large messages are fragmented so each frame stays within the
                # frame bound; fragments of one message never interleave with
                # another frame because the send lock is held for the message.
                for offset in range(0, len(payload), self._max_frame_bytes):
                    chunk = payload[offset : offset + self._max_frame_bytes]
                    final = offset + self._max_frame_bytes >= len(payload)
                    opcode = OP_TEXT if offset == 0 else OP_CONTINUATION
                    self._writer.write(encode_frame(opcode, chunk, mask=True, fin=final))
                    await self._writer.drain()
            await self._writer.drain()

    async def _send_control(self, opcode: int, payload: bytes) -> None:
        async with self._send_lock:
            if self._closed:
                raise self._close_error or RuntimeConnectionClosed(
                    f"runtime connection to {self._endpoint} is closed"
                )
            self._writer.write(encode_frame(opcode, payload, mask=True))
            await self._writer.drain()

    async def _send_close_frame(self) -> None:
        if self._close_sent:
            return
        self._close_sent = True
        # A peer that already reset the socket must not make aclose raise:
        # closing is deterministic even against a rude backend.
        with contextlib.suppress(RuntimeConnectionError, OSError):
            await self._send_control(OP_CLOSE, CLOSE_NORMAL.to_bytes(2, "big"))

    # ---- receiving ---------------------------------------------------------

    async def _receive_loop(self) -> None:
        decoder = FrameDecoder(expect_masked=False, max_frame_bytes=self._max_frame_bytes)
        reassembly = _ReassemblyState()
        try:
            while True:
                data = await self._reader.read(65536)
                if not data:
                    self._abort(
                        RuntimeConnectionClosed(
                            f"runtime backend at {self._endpoint} closed the connection"
                        )
                    )
                    return
                for frame in decoder.feed(data):
                    if await self._apply_frame(frame, decoder, reassembly):
                        return
        except FrameProtocolError as exc:
            self._abort(
                RuntimeProtocolError(
                    f"runtime backend at {self._endpoint} broke the websocket framing: {exc}"
                )
            )
        except RuntimeProtocolError as exc:
            self._abort(exc)
        except Exception as exc:
            self._abort(
                RuntimeConnectionClosed(
                    f"runtime connection to {self._endpoint} failed while receiving: {exc}"
                )
            )

    async def _apply_frame(
        self,
        frame: DecodedFrame,
        decoder: FrameDecoder,
        reassembly: _ReassemblyState,
    ) -> bool:
        """Apply one decoded frame; True when the receive loop must stop.

        Protocol violations raise from here, outside any ``try`` in the loop
        itself, so the loop's handlers stay a single typed failure seam.
        """
        del decoder
        if frame.opcode == OP_PING:
            await self._send_control(OP_PONG, frame.payload)
            return False
        if frame.opcode == OP_PONG:
            return False
        if frame.opcode == OP_CLOSE:
            if not self._close_sent:
                with contextlib.suppress(RuntimeConnectionError):
                    await self._send_control(OP_CLOSE, frame.payload[:2])
            self._abort(
                RuntimeConnectionClosed(f"runtime backend at {self._endpoint} sent a close frame")
            )
            return True
        return self._apply_data_frame(frame, reassembly)

    def _apply_data_frame(self, frame: DecodedFrame, reassembly: _ReassemblyState) -> bool:
        if frame.opcode == OP_BINARY:
            raise RuntimeProtocolError(
                "runtime backend sent a binary frame — the engine speaks JSON "
                "text frames only, so a binary frame is a protocol violation, "
                "not a payload to guess at"
            )
        if frame.opcode == OP_CONTINUATION:
            if not reassembly.fragmented:
                raise RuntimeProtocolError(
                    "runtime backend sent a continuation frame without an open fragmented message"
                )
            reassembly.fragments.append(frame.payload)
            reassembly.length += len(frame.payload)
            if reassembly.length > self._max_message_bytes:
                raise RuntimePayloadTooLarge(
                    f"fragmented runtime message exceeded the bound {self._max_message_bytes} bytes"
                )
            if not frame.fin:
                return False
            message = b"".join(reassembly.fragments)
            reassembly.reset()
            self._handle_message(message)
            return False
        # A new text data frame.
        if reassembly.fragmented:
            raise RuntimeProtocolError(
                "runtime backend started a new data frame while a fragmented message was still open"
            )
        if frame.fin:
            if len(frame.payload) > self._max_message_bytes:
                raise RuntimePayloadTooLarge(
                    f"runtime message of {len(frame.payload)} bytes exceeds the engine "
                    f"bound {self._max_message_bytes} — the frame bound alone does not "
                    "license an oversized message"
                )
            self._handle_message(frame.payload)
            return False
        if len(frame.payload) > self._max_message_bytes:
            raise RuntimePayloadTooLarge(
                f"runtime message fragment of {len(frame.payload)} bytes already exceeds "
                f"the engine bound {self._max_message_bytes}"
            )
        reassembly.fragments = [frame.payload]
        reassembly.length = len(frame.payload)
        reassembly.fragmented = True
        return False

    def _handle_message(self, payload: bytes) -> None:
        try:
            message = json.loads(payload)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.malformed_messages += 1
            return
        if not isinstance(message, dict):
            self.malformed_messages += 1
            return
        method = message.get("method")
        if isinstance(method, str) and method.strip():
            self._handle_inbound_method(message, method)
            return
        if "method" in message:
            self.malformed_messages += 1
            return
        if "id" in message and ("result" in message or "error" in message):
            self._handle_reply(message)
            return
        self.malformed_messages += 1

    def _handle_inbound_method(self, message: dict[str, object], method: str) -> None:
        """Deliver one notification or server request; never answer it."""
        params: Mapping[str, object] | None = None
        if "params" in message:
            raw_params = message["params"]
            if not isinstance(raw_params, dict):
                self.malformed_messages += 1
                return
            params = raw_params
        request_id = message.get("id")
        if request_id is not None and not isinstance(request_id, (int, str)):
            self.skipped_server_requests += 1
            return
        if request_id is not None:
            try:
                validate_native_request_id(request_id, "notification request_id")
            except (TypeError, ValueError):
                # The id cannot be represented in the frozen contract without
                # lying about its type (bools, oversized ints, blank strings).
                # Record it and keep the connection; Theater must never answer
                # a request it cannot even name, and the native UI still can.
                self.skipped_server_requests += 1
                return
        try:
            notification = RuntimeNotification(
                method=method,
                params={} if params is None else params,
                request_id=request_id,
            )
        except (TypeError, ValueError):
            self.malformed_messages += 1
            return
        self._enqueue(notification)

    def _handle_reply(self, message: dict[str, object]) -> None:
        request_id = message["id"]
        if isinstance(request_id, bool) or not isinstance(request_id, int):
            self.unsolicited_replies += 1
            return
        future = self._pending.get(request_id)
        if future is None or future.done():
            self.unsolicited_replies += 1
            return
        if "error" in message:
            error = message["error"]
            if isinstance(error, dict):
                code = error.get("code")
                text = error.get("message")
            else:
                code = None
                text = None
            message_text = text if isinstance(text, str) else json.dumps(error, default=str)
            future.set_exception(RuntimeRequestError(code, message_text))
            return
        result = message.get("result")
        if not isinstance(result, Mapping):
            future.set_exception(
                RuntimeMalformedReply(
                    f"runtime backend at {self._endpoint} replied to request "
                    f"{request_id} with a non-object result — the reply cannot be "
                    "represented as a JSON object mapping, so the request failed rather "
                    "than guessing at a shape"
                )
            )
            return
        future.set_result(result)

    def _enqueue(self, notification: RuntimeNotification) -> None:
        if self._closed:
            # Only reachable in the same decoder batch that closed the
            # connection; the close reason already carries the evidence gap.
            self.dropped_notifications += 1
            return
        if self._notifications.qsize() >= self._receive_queue_max:
            # Fail closed: the buffered stream is evidence (terminal and
            # identity facts), so the 129th notification is not something we
            # may drop or deliver-and-continue. Close with a typed, visible
            # reason; the observer must reconcile from durable state.
            self.dropped_notifications += 1
            self._abort(
                RuntimeNotificationOverflow(
                    f"runtime notification buffer for {self._endpoint} saturated at "
                    f"{self._receive_queue_max} buffered notifications — closing the "
                    "connection instead of discarding evidence: terminal or identity "
                    "facts may be missing, so durable reconciliation from persisted "
                    "state is required before acting on any inferred outcome"
                )
            )
            return
        self._notifications.put_nowait(notification)

    # ---- close plumbing ----------------------------------------------------

    def _abort(self, error: RuntimeConnectionError) -> None:
        """Idempotently mark closed, fail pending requests, end the iterator."""
        if self._closed:
            self._close_error = self._close_error or error
            return
        self._closed = True
        self._close_error = error
        for future in list(self._pending.values()):
            if not future.done():
                future.set_exception(error)
        self._pending.clear()
        # The sentinel slot is reserved, so no buffered notification is
        # sacrificed to terminate the iterator.
        self._notifications.put_nowait(_CloseMarker(error))
        with contextlib.suppress(Exception):
            self._writer.close()


class WebSocketRuntimeIO(RuntimeIO):
    """The shared ``RuntimeIO`` implementation: WebSocket-over-Unix connections.

    Stateless and reusable: the daemon injects one instance through a
    ``RuntimeContext`` and each ``connect`` yields an independent bounded
    connection to one private endpoint.
    """

    def __init__(
        self,
        *,
        max_frame_bytes: int = RUNTIME_WS_MAX_FRAME_BYTES,
        max_message_bytes: int = RUNTIME_WS_MAX_MESSAGE_BYTES,
        receive_queue_max: int = RUNTIME_WS_RECEIVE_QUEUE_MAX,
        outstanding_requests_max: int = RUNTIME_WS_MAX_OUTSTANDING_REQUESTS,
        close_handshake_timeout: float = RUNTIME_WS_CLOSE_HANDSHAKE_TIMEOUT_SECONDS,
    ) -> None:
        self._max_frame_bytes = max_frame_bytes
        self._max_message_bytes = max_message_bytes
        self._receive_queue_max = receive_queue_max
        self._outstanding_requests_max = outstanding_requests_max
        self._close_handshake_timeout = close_handshake_timeout

    async def connect(self, endpoint: str, *, timeout: float) -> JsonRpcRuntimeConnection:
        path = endpoint_to_path(endpoint)
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(path), timeout)
        except (OSError, TimeoutError) as exc:
            raise RuntimeConnectionError(
                f"cannot reach the runtime backend endpoint {endpoint!r}: {exc} — the backend "
                "may not be running; use wait_for_unix_endpoint on launch paths, and never "
                "retry a native mutation on an uncertain delivery"
            ) from exc
        try:
            await asyncio.wait_for(_upgrade_handshake(reader, writer), timeout)
        except BaseException:
            # Best-effort closure: a transport that errors while closing must
            # never mask the typed handshake failure the caller needs to see.
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
            raise
        return JsonRpcRuntimeConnection(
            reader,
            writer,
            endpoint=endpoint,
            max_frame_bytes=self._max_frame_bytes,
            max_message_bytes=self._max_message_bytes,
            receive_queue_max=self._receive_queue_max,
            outstanding_requests_max=self._outstanding_requests_max,
            close_handshake_timeout=self._close_handshake_timeout,
        )


__all__ = [
    "JsonRpcRuntimeConnection",
    "RuntimeTransportStatistics",
    "WebSocketRuntimeIO",
    "endpoint_to_path",
    "wait_for_unix_endpoint",
]
