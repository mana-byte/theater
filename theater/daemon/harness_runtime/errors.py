"""Typed failures for the shared harness runtime engine.

Only narrow subclasses on top of the frozen ``RuntimeConnectionError`` vocabulary.
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

    Fail closed: notifications carry terminal and identity evidence, so dropping one would
    let callers act on unproven outcomes. Reconcile from persisted state before trusting any.
    """


class RuntimeMalformedReply(RuntimeConnectionError):
    """The backend replied with a message that cannot represent a result."""


class BackendProcessError(RuntimeError):
    """Base class for detached backend process-ownership failures."""


class BackendLaunchError(BackendProcessError):
    """The detached backend could not be launched from its plan."""


class BackendIdentityMismatch(BackendProcessError):
    """The recorded pid no longer identifies our backend; fail closed.

    No signal, no attachment: pid reuse must never turn teardown into killing another process.
    """


class RuntimeManagerError(RuntimeError):
    """Base class for runtime-manager state failures."""


class RuntimeGenerationMismatch(RuntimeManagerError):
    """The requested operation names a backend generation that is not current.

    Nothing is touched: a stale generation is how controls land on another backend.
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
