"""Daemon-owned lifecycle for bounded inbound native OTel channels."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from theater.constants.harness import (
    HARNESS_CHANNEL_HEALTH_DIAGNOSTIC_MAX_CHARS,
    HARNESS_CHANNEL_HEALTH_MAX_DIAGNOSTICS,
    HARNESS_CHANNEL_IDENTIFIER_MAX_CHARS,
    HARNESS_OTEL_PARSE_MAX_IN_FLIGHT,
)
from theater.harness.channels.otel.bounds import (
    OtelIngressError,
    decode_otlp_json,
    decode_otlp_protobuf,
    validate_records,
)
from theater.harness.channels.otel.callbacks import (
    OtelCallbackBusy,
    OtelCallbackRunner,
    OtelCallbackTimeout,
)
from theater.harness.channels.otel.inbox import OtelDelivery, OtelInbox
from theater.harness.channels.otel.receiver import (
    NativeOtelReceiver,
    OtelHttpError,
    OtelHttpResponse,
)
from theater.harness.channels.otel.source import OtelSource
from theater.harness.contracts.callbacks import OtelCorrelationContext
from theater.harness.contracts.channels import (
    ChannelHealth,
    ChannelKind,
    OtelBinding,
    OtelProtocol,
    OtelRecord,
)
from theater.harness.contracts.launch import ChannelCredential
from theater.harness.contracts.manifest import EnrichmentManifest, OtelChannelManifest

if TYPE_CHECKING:
    from theater.harness.channels.composite import EnrichmentBinding

logger = logging.getLogger("theater.harness.otel")


type OtelCredentialLookup = Callable[[str, str], object | None]
type OtelReceiverPortLookup = Callable[[], str | None]
type OtelReceiverPortStore = Callable[[int], None]


@dataclass(frozen=True, slots=True)
class _ActiveChannel:
    participant_id: str
    harness: str
    channel: OtelChannelManifest
    credential: ChannelCredential


class NativeOtelRuntime:
    """Own the independent receiver, queues, callbacks, and bounded state."""

    def __init__(
        self,
        credential_lookup: OtelCredentialLookup,
        *,
        bind_host: str = "127.0.0.1",
        callback_runner: OtelCallbackRunner | None = None,
        receiver_port_lookup: OtelReceiverPortLookup | None = None,
        receiver_port_store: OtelReceiverPortStore | None = None,
    ) -> None:
        self._inbox = OtelInbox()
        self._callbacks = callback_runner if callback_runner is not None else OtelCallbackRunner()
        self._credential_lookup = credential_lookup
        self._receiver_port_lookup = (
            receiver_port_lookup if receiver_port_lookup is not None else lambda: None
        )
        self._receiver_port_store = (
            receiver_port_store if receiver_port_store is not None else lambda _port: None
        )
        self._bind_host = bind_host
        self._receiver = NativeOtelReceiver(self.ingest_http, host=bind_host)
        self._active: dict[tuple[str, str], _ActiveChannel] = {}
        self._parse_tasks: set[asyncio.Task[object]] = set()
        self._parse_in_flight = 0
        self._diagnostics: tuple[str, ...] = ()
        self._available = False
        self._closed = False

    async def start(self, harnesses: Mapping[str, object]) -> bool:
        """Start only when at least one declared channel can be installed safely."""
        if self._closed:
            raise RuntimeError("native OTel runtime is closed")
        if self._available:
            return True
        try:
            if not any(_has_available_channel(harness) for harness in harnesses.values()):
                return False
            persisted_port = self._receiver_port_lookup()
            preferred_port = _receiver_port(persisted_port)
            await self._receiver.start(preferred_port=preferred_port)
            if persisted_port is None:
                self._receiver_port_store(self._receiver.port)
        except Exception as exc:
            self._available = False
            diagnostic = self._diagnose(
                f"native OTel receiver is unavailable: {type(exc).__name__}: {exc}"
            )
            logger.warning("%s", diagnostic)
            with contextlib.suppress(Exception):
                await self._receiver.aclose()
            self._receiver = NativeOtelReceiver(self.ingest_http, host=self._bind_host)
            return False
        self._available = True
        return True

    @property
    def available(self) -> bool:
        """Whether the receiver can accept native OTel exports."""
        return self._available and not self._closed

    @property
    def diagnostics(self) -> tuple[str, ...]:
        """Return bounded receiver lifecycle diagnostics."""
        return self._diagnostics

    @property
    def endpoint(self) -> str:
        """Return the launch-local endpoint for an active receiver."""
        if not self.available:
            raise RuntimeError("native OTel receiver is unavailable")
        return self._receiver.endpoint

    def activate(
        self,
        *,
        participant_id: str,
        harness: str,
        channel: OtelChannelManifest,
        credential: ChannelCredential,
    ) -> bool:
        """Register one persisted participant channel after launch planning."""
        if not self.available:
            return False
        self._inbox.register(participant_id, channel.declaration)
        self._active[(participant_id, channel.declaration.id)] = _ActiveChannel(
            participant_id=participant_id,
            harness=harness,
            channel=channel,
            credential=credential,
        )
        return True

    def restore(self, participants: Sequence[object], harnesses: Mapping[str, object]) -> None:
        """Restore active channels from persisted credentials after daemon restart."""
        if not self.available:
            return
        for participant in participants:
            participant_id = getattr(participant, "id", None)
            harness_name = getattr(participant, "harness", None)
            if not isinstance(participant_id, str) or not isinstance(harness_name, str):
                continue
            harness = harnesses.get(harness_name)
            observer = getattr(harness, "observer", None)
            enrichments = getattr(observer, "enrichment_manifests", lambda: ())()
            for channel in enrichments:
                if not isinstance(channel, OtelChannelManifest) or not _channel_available(channel):
                    continue
                record = self._credential_lookup(participant_id, channel.declaration.id)
                token = getattr(record, "token", None)
                stored_harness = getattr(record, "harness", None)
                stored_channel = getattr(record, "channel_id", None)
                token_path = getattr(record, "token_path", None)
                if (
                    not isinstance(token, str)
                    or stored_harness != harness_name
                    or stored_channel != channel.declaration.id
                    or not isinstance(token_path, str)
                ):
                    continue
                from pathlib import Path

                self.activate(
                    participant_id=participant_id,
                    harness=harness_name,
                    channel=channel,
                    credential=ChannelCredential(
                        kind=ChannelKind.OTEL,
                        channel_id=channel.declaration.id,
                        token=token,
                        token_path=Path(token_path),
                    ),
                )

    def active_channels(
        self,
        participant_id: str,
        enrichments: Sequence[EnrichmentManifest],
    ) -> tuple[OtelChannelManifest, ...]:
        if not self.available:
            return ()
        return tuple(
            channel
            for channel in enrichments
            if isinstance(channel, OtelChannelManifest)
            and _channel_available(channel)
            and (participant_id, channel.declaration.id) in self._active
        )

    def has_active(self, participant_id: str, enrichments: Sequence[EnrichmentManifest]) -> bool:
        return bool(self.active_channels(participant_id, enrichments))

    def open_source(
        self,
        *,
        participant_id: str,
        harness: str,
        channel: OtelChannelManifest,
    ) -> OtelSource:
        if not self.available:
            raise RuntimeError("native OTel receiver is unavailable")
        self._inbox.register(participant_id, channel.declaration)
        return OtelSource(
            inbox=self._inbox,
            callbacks=self._callbacks,
            participant_id=participant_id,
            harness=harness,
            channel=channel,
        )

    def enrichment_bindings(
        self,
        participant_id: str,
        harness: str,
        enrichments: Sequence[EnrichmentManifest],
    ) -> tuple[EnrichmentBinding, ...]:
        from theater.harness.channels.composite import EnrichmentBinding

        return tuple(
            EnrichmentBinding(
                source=self.open_source(
                    participant_id=participant_id,
                    harness=harness,
                    channel=channel,
                ),
                declaration=channel.declaration,
            )
            for channel in self.active_channels(participant_id, enrichments)
        )

    def drop_participant(self, participant_id: str) -> None:
        self._inbox.drop_participant(participant_id)
        for key in tuple(self._active):
            if key[0] == participant_id:
                self._active.pop(key, None)

    def health_snapshot(self, participant_id: str) -> tuple[ChannelHealth, ...]:
        return self._inbox.health_snapshot(participant_id)

    async def ingest_http(  # noqa: PLR0912
        self,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
    ) -> OtelHttpResponse:
        """Decode, authenticate, correlate, and enqueue one bounded OTLP export."""
        if not self.available:
            raise OtelHttpError("native OTel receiver is unavailable", status=503)
        if path != "/v1/logs":
            raise OtelHttpError("native OTel path is not enabled", status=404)
        active = self._authenticate_request(headers)
        content_type = headers.get("content-type")
        protocol = _protocol(content_type)
        if active.channel.protocol is not protocol:
            self._mark_degraded(active, "native OTel protocol is not enabled")
            raise OtelHttpError("native OTel protocol is not enabled")
        if len(body) > active.channel.declaration.bounds.max_payload_bytes:
            self._mark_degraded(active, "native OTel body exceeds the channel limit")
            raise OtelHttpError("native OTel body exceeds the channel limit", status=413)
        try:
            records = await self._decode(protocol, body)
        except OtelIngressError as exc:
            self._mark_degraded(active, str(exc))
            raise OtelHttpError(str(exc)) from exc
        except OtelHttpError as exc:
            self._mark_degraded(active, str(exc))
            raise
        if not records:
            return OtelHttpResponse()
        try:
            validate_records(records, active.channel.bounds)
        except OtelIngressError as exc:
            self._mark_degraded(active, str(exc))
            raise OtelHttpError(str(exc)) from exc
        if any(not self._matches_resource(active, record) for record in records):
            self._mark_degraded(active, "native OTel resource identity does not match credential")
            raise OtelHttpError("native OTel resource identity is invalid", status=403)
        pending: list[OtelDelivery] = []
        try:
            for record_index, record in enumerate(records):
                binding = self._binding(active.channel, record)
                export_id = self._export_id(active.channel, record)
                delivery_id = _record_delivery_id(export_id, record_index)
                if self._inbox.delivery_seen(
                    active.participant_id,
                    active.channel.declaration.id,
                    delivery_id,
                ):
                    continue
                native_id = await self._correlate(active, binding, record, delivery_id)
                pending.append(
                    OtelDelivery(
                        signal=record.signal,
                        binding_name=binding.name,
                        record=record,
                        native_id=native_id,
                        delivery_id=delivery_id,
                    )
                )
        except OtelHttpError as exc:
            self._mark_degraded(active, str(exc))
            raise
        dropped = 0
        for delivery in pending:
            result = self._inbox.enqueue(
                active.participant_id,
                active.channel.declaration,
                delivery,
            )
            dropped += int(result.dropped)
        if dropped:
            return OtelHttpResponse(
                rejected_records=dropped,
                error_message="native OTel inbox overflow",
            )
        return OtelHttpResponse()

    def _authenticate_request(self, headers: Mapping[str, str]) -> _ActiveChannel:
        matches: list[_ActiveChannel] = []
        for active in self._active.values():
            correlation = active.channel.correlation
            if correlation is None:
                continue
            token = headers.get(correlation.auth_header.lower())
            if isinstance(token, str) and hmac.compare_digest(token, active.credential.token):
                matches.append(active)
        if not matches:
            raise OtelHttpError("native OTel credential is invalid", status=401)
        if len(matches) != 1:
            raise OtelHttpError("native OTel credential is invalid", status=401)
        return matches[0]

    @staticmethod
    def _matches_resource(active: _ActiveChannel, record: OtelRecord) -> bool:
        correlation = active.channel.correlation
        assert correlation is not None
        return (
            record.resource.get(correlation.participant_attribute) == active.participant_id
            and record.resource.get(correlation.harness_attribute) == active.harness
            and record.resource.get(correlation.channel_attribute) == active.channel.declaration.id
        )

    async def _decode(self, protocol: OtelProtocol, body: bytes) -> tuple[OtelRecord, ...]:
        if self._parse_in_flight >= HARNESS_OTEL_PARSE_MAX_IN_FLIGHT:
            raise OtelHttpError("native OTel decoder is overloaded", status=429)
        decoder = (
            decode_otlp_json if protocol is OtelProtocol.OTLP_HTTP_JSON else decode_otlp_protobuf
        )
        self._parse_in_flight += 1
        task = asyncio.create_task(asyncio.to_thread(decoder, body))
        self._parse_tasks.add(task)
        task.add_done_callback(self._finish_parse)
        return await asyncio.shield(task)

    def _finish_parse(self, task: asyncio.Task[object]) -> None:
        self._parse_tasks.discard(task)
        self._parse_in_flight -= 1
        if not task.cancelled():
            with contextlib.suppress(Exception):
                task.exception()

    def _binding(self, channel: OtelChannelManifest, record: OtelRecord) -> OtelBinding:
        correlation = channel.correlation
        assert correlation is not None
        name = record.attributes.get(correlation.binding_attribute)
        if not isinstance(name, str):
            raise OtelHttpError("native OTel signal is not declared")
        for binding in channel.bindings:
            if binding.signal is record.signal and binding.name == name:
                return binding
        raise OtelHttpError("native OTel signal is not declared")

    def _export_id(self, channel: OtelChannelManifest, record: OtelRecord) -> str:
        correlation = channel.correlation
        assert correlation is not None
        value = record.attributes.get(correlation.delivery_id_attribute)
        if not isinstance(value, str):
            raise OtelHttpError("native OTel export identity is invalid")
        return _identifier(value, "export identity")

    async def _correlate(
        self,
        active: _ActiveChannel,
        binding: OtelBinding,
        record: OtelRecord,
        delivery_id: str,
    ) -> str:
        try:
            native_id = await self._callbacks.correlate(
                binding.correlation,
                OtelCorrelationContext(
                    participant_id=active.participant_id,
                    harness=active.harness,
                    channel_id=active.channel.declaration.id,
                    record=record,
                    delivery_id=delivery_id,
                ),
            )
            return _identifier(native_id, "correlation")
        except asyncio.CancelledError:
            raise
        except (OtelCallbackBusy, OtelCallbackTimeout):
            raise OtelHttpError("native OTel correlation is unavailable") from None
        except Exception:
            raise OtelHttpError("native OTel correlation is invalid") from None

    def _mark_degraded(self, active: _ActiveChannel, diagnostic: str) -> None:
        tracker = self._inbox.health(active.participant_id, active.channel.declaration.id)
        if tracker is not None:
            tracker.mark_degraded(diagnostic)

    def _diagnose(self, diagnostic: str) -> str:
        bounded = diagnostic[:HARNESS_CHANNEL_HEALTH_DIAGNOSTIC_MAX_CHARS]
        self._diagnostics = (
            *self._diagnostics,
            bounded,
        )[-HARNESS_CHANNEL_HEALTH_MAX_DIAGNOSTICS:]
        return bounded

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._available = False
        await self._receiver.aclose()
        for task in tuple(self._parse_tasks):
            task.cancel()
        if self._parse_tasks:
            await asyncio.gather(*self._parse_tasks, return_exceptions=True)
        self._parse_tasks.clear()
        await self._callbacks.aclose()
        await self._inbox.aclose()
        self._active.clear()


def _has_available_channel(harness: object) -> bool:
    observer = getattr(harness, "observer", None)
    enrichments = getattr(observer, "enrichment_manifests", lambda: ())()
    return any(
        isinstance(channel, OtelChannelManifest) and _channel_available(channel)
        for channel in enrichments
    )


def _channel_available(channel: OtelChannelManifest) -> bool:
    return (
        channel.unavailable_reason is None
        and bool(channel.bindings)
        and channel.installer is not None
    )


def _identifier(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or not value.strip()
        or len(value) > HARNESS_CHANNEL_IDENTIFIER_MAX_CHARS
        or not value.isprintable()
    ):
        raise OtelHttpError(f"native OTel {label} is invalid")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise OtelHttpError(f"native OTel {label} is invalid") from exc
    return value


def _record_delivery_id(export_id: str, record_index: int) -> str:
    digest = hashlib.blake2s(f"{export_id}\0{record_index}".encode(), digest_size=16).hexdigest()
    return f"otel-{digest}"


def _protocol(content_type: object) -> OtelProtocol:
    if content_type == "application/json":
        return OtelProtocol.OTLP_HTTP_JSON
    if content_type == "application/x-protobuf":
        return OtelProtocol.OTLP_HTTP_PROTOBUF
    raise OtelHttpError("native OTel content type is not enabled", status=415)


def _receiver_port(value: str | None) -> int:
    if value is None:
        return 0
    if not value.isdecimal():
        raise ValueError("persisted native OTel receiver port is invalid")
    port = int(value)
    if not 1 <= port <= 65_535:
        raise ValueError("persisted native OTel receiver port is invalid")
    return port


__all__ = ["NativeOtelRuntime"]
