"""Generic hook ingress bounds and runtime broker."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING

from theater.constants.harness import (
    HARNESS_HOOK_IDENTIFIER_MAX_CHARS,
    HARNESS_HOOK_MAX_JSON_ATTRIBUTES,
    HARNESS_HOOK_MAX_JSON_DEPTH,
)
from theater.harness.channels.hooks.callbacks import HookCallbackRunner
from theater.harness.channels.hooks.inbox import HookDelivery, HookEnqueueResult, HookInbox
from theater.harness.channels.hooks.source import HookSource
from theater.harness.contracts.callbacks import HookCorrelationContext
from theater.harness.contracts.channels import HookBinding
from theater.harness.contracts.manifest import EnrichmentManifest, HookChannelManifest

if TYPE_CHECKING:
    from theater.harness.channels.composite import EnrichmentBinding


class HookIngressError(ValueError):
    """An untrusted hook envelope exceeded its contract."""


type HookCredentialProbe = Callable[[str, str], bool]


def validate_hook_identifier(value: object, label: str) -> str:
    """Return one bounded non-blank hook identifier."""
    if (
        not isinstance(value, str)
        or not value
        or not value.strip()
        or len(value) > HARNESS_HOOK_IDENTIFIER_MAX_CHARS
        or not value.isprintable()
    ):
        raise HookIngressError(f"hook {label} must be a bounded non-blank string")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise HookIngressError(f"hook {label} must be valid UTF-8") from exc
    return value


def validate_hook_payload(payload: object, *, max_bytes: int) -> dict[str, object]:
    """Copy one bounded JSON object without accepting opaque values."""
    if type(payload) is not dict:
        raise HookIngressError("hook payload must be a JSON object")
    attributes = 0

    def visit(value: object, depth: int) -> object:
        nonlocal attributes
        if depth > HARNESS_HOOK_MAX_JSON_DEPTH:
            raise HookIngressError("hook payload exceeds the maximum nesting depth")
        if value is None or type(value) in (bool, int, float):
            return value
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as exc:
                raise HookIngressError("hook payload contains invalid UTF-8") from exc
            return value
        if type(value) is list:
            if len(value) > HARNESS_HOOK_MAX_JSON_ATTRIBUTES:
                raise HookIngressError("hook payload has too many values")
            return [visit(item, depth + 1) for item in value]
        if type(value) is dict:
            attributes += len(value)
            if attributes > HARNESS_HOOK_MAX_JSON_ATTRIBUTES:
                raise HookIngressError("hook payload has too many attributes")
            copied: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise HookIngressError("hook payload keys must be strings")
                try:
                    key.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise HookIngressError("hook payload contains invalid UTF-8") from exc
                copied[key] = visit(item, depth + 1)
            return copied
        raise HookIngressError("hook payload must contain JSON values")

    copied = visit(payload, 0)
    if not isinstance(copied, dict):
        raise HookIngressError("hook payload must be a JSON object")
    try:
        encoded = json.dumps(
            copied, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise HookIngressError("hook payload must be JSON serializable") from exc
    if len(encoded) > max_bytes:
        raise HookIngressError("hook payload exceeds the channel size limit")
    return copied


class HookRuntime:
    """Daemon-owned broker for bounded generic hook channels."""

    def __init__(
        self,
        credential_active: HookCredentialProbe,
        *,
        callback_runner: HookCallbackRunner | None = None,
    ) -> None:
        self._inbox = HookInbox()
        self._callbacks = callback_runner if callback_runner is not None else HookCallbackRunner()
        self._credential_active = credential_active
        self._closed = False

    def active_channels(
        self,
        participant_id: str,
        enrichments: Sequence[EnrichmentManifest],
    ) -> tuple[HookChannelManifest, ...]:
        if self._closed:
            return ()
        return tuple(
            channel
            for channel in enrichments
            if isinstance(channel, HookChannelManifest)
            and channel.bindings
            and channel.installer is not None
            and self._credential_active(participant_id, channel.declaration.id)
        )

    def has_active(self, participant_id: str, enrichments: Sequence[EnrichmentManifest]) -> bool:
        return bool(self.active_channels(participant_id, enrichments))

    async def correlate(self, binding: HookBinding, context: HookCorrelationContext) -> str:
        return await self._callbacks.correlate(binding.correlation, context)

    def enqueue(
        self,
        *,
        participant_id: str,
        channel: HookChannelManifest,
        event: str,
        payload: Mapping[str, object],
        delivery_id: str | None,
        native_id: str,
    ) -> HookEnqueueResult:
        if self._closed:
            raise RuntimeError("hook runtime is closed")
        return self._inbox.enqueue(
            participant_id,
            channel.declaration,
            HookDelivery(
                event=event,
                payload=payload,
                delivery_id=delivery_id,
                native_id=native_id,
            ),
        )

    def delivery_seen(
        self,
        *,
        participant_id: str,
        channel_id: str,
        delivery_id: str,
    ) -> bool:
        return self._inbox.delivery_seen(participant_id, channel_id, delivery_id)

    def open_source(self, *, participant_id: str, channel: HookChannelManifest) -> HookSource:
        if self._closed:
            raise RuntimeError("hook runtime is closed")
        self._inbox.register(participant_id, channel.declaration)
        return HookSource(
            inbox=self._inbox,
            callbacks=self._callbacks,
            participant_id=participant_id,
            channel=channel,
        )

    def enrichment_bindings(
        self,
        participant_id: str,
        enrichments: Sequence[EnrichmentManifest],
    ) -> tuple[EnrichmentBinding, ...]:
        from theater.harness.channels.composite import EnrichmentBinding

        return tuple(
            EnrichmentBinding(
                source=self.open_source(participant_id=participant_id, channel=channel),
                declaration=channel.declaration,
            )
            for channel in self.active_channels(participant_id, enrichments)
        )

    def drop_participant(self, participant_id: str) -> None:
        self._inbox.drop_participant(participant_id)

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._callbacks.aclose()
        await self._inbox.aclose()


__all__ = [
    "HookCredentialProbe",
    "HookIngressError",
    "HookRuntime",
    "validate_hook_identifier",
    "validate_hook_payload",
]
