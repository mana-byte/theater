"""Typed failures for the shared harness runtime engine.

The frozen Wave 1 contracts fix the connection-failure vocabulary
(``RuntimeConnectionError`` and its three concrete subclasses). This module
only adds narrow subclasses and process-ownership failures on top of those
frozen types — nothing here edits or replaces the public contracts.
"""

from __future__ import annotations

from theater.harness.contracts.runtime import (
    RuntimeConnectionError,
)


class RuntimeHandshakeError(RuntimeConnectionError):
    """The HTTP Upgrade handshake to the native backend failed."""


class RuntimeProtocolError(RuntimeConnectionError):
    """The backend broke the WebSocket framing rules; the connection is dead."""


class RuntimePayloadTooLarge(RuntimeProtocolError):
    """A frame or message exceeded the engine's bounded payload limits."""


class RuntimeConnectionSaturated(RuntimeConnectionError):
    """The outstanding-request bound is exhausted; fail fast instead of queueing."""


class RuntimeNotificationOverflow(RuntimeConnectionError):
    """The bounded notification buffer saturated; the connection is closed.

    Closing is the fail-closed move: notifications carry terminal and identity
    evidence, so discarding one to keep the stream alive would let a caller act
    on an outcome whose proof was dropped. Callers see this typed failure (a
    frozen ``RuntimeConnectionError`` subclass) on pending requests, the
    notification iterator ends, and durable reconciliation from persisted state
    is required before trusting any inferred outcome.
    """


class RuntimeMalformedReply(RuntimeConnectionError):
    """The backend replied with a message that cannot represent a result."""


class BackendProcessError(RuntimeError):
    """Base class for detached backend process-ownership failures."""


class BackendLaunchError(BackendProcessError):
    """The detached backend could not be launched from its plan."""


class BackendIdentityMismatch(BackendProcessError):
    """The recorded pid no longer identifies our backend; fail closed.

    No signal is sent and no attachment is made: a pid whose process identity
    changed is a different process, and pid reuse must never turn participant
    teardown into killing an unrelated process.
    """


class RuntimeManagerError(RuntimeError):
    """Base class for runtime-manager state failures."""


class RuntimeGenerationMismatch(RuntimeManagerError):
    """The requested operation names a backend generation that is not current.

    The manager fails closed: neither the runtime connection nor the backend
    process is touched, because acting on a stale generation is how one
    participant's controls land on another generation's backend.
    """


class BackendAlreadyLaunched(RuntimeManagerError):
    """A live backend of a different generation already owns this participant."""


__all__ = [
    "BackendAlreadyLaunched",
    "BackendIdentityMismatch",
    "BackendLaunchError",
    "BackendProcessError",
    "RuntimeConnectionSaturated",
    "RuntimeGenerationMismatch",
    "RuntimeHandshakeError",
    "RuntimeMalformedReply",
    "RuntimeManagerError",
    "RuntimeNotificationOverflow",
    "RuntimePayloadTooLarge",
    "RuntimeProtocolError",
]
