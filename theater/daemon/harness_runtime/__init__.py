"""Shared runtime engine: transport, detached backend ownership, runtime manager.

This package is the daemon-owned implementation behind the frozen Wave 1
public contracts (``RuntimeIO`` / ``RuntimeConnection`` seams and
``HarnessRuntime`` lifecycle ownership). Plugin code never imports it — it
receives the engine only through ``theater.harness.contracts.runtime``
injection. Composition roots (``theater/daemon/server.py``, spawning, RPCs)
are wired in later waves and are untouched here.

Layout:

* ``transport`` — RFC 6455 WebSocket-over-Unix client with HTTP Upgrade,
  masked client frames, fragmented/control frame handling, JSON request
  correlation without a ``jsonrpc`` field, bounded queues, deadlines, and
  server requests surfaced but never answered.
* ``backend`` — detached-backend process ownership: private participant
  endpoint artifacts, launch from a ``RuntimePlan``, verified pid/process
  identity, generation-safe graceful terminate/kill.
* ``manager`` — one ``HarnessRuntime`` instance per participant, concurrent
  get-or-create without duplicates, reconnect primitives, and explicit
  close-without-kill versus teardown.
"""

from __future__ import annotations

from theater.daemon.harness_runtime.backend import (
    BackendProcessIdentity,
    DetachedBackendProcess,
    adopt_detached_backend,
    backend_artifacts_dir,
    capture_process_identity,
    launch_detached_backend,
    pid_alive,
    process_started_at,
    verify_process_identity,
)
from theater.daemon.harness_runtime.errors import (
    BackendAlreadyLaunched,
    BackendIdentityMismatch,
    BackendLaunchError,
    BackendProcessError,
    RuntimeConnectionSaturated,
    RuntimeGenerationMismatch,
    RuntimeHandshakeError,
    RuntimeMalformedReply,
    RuntimeManagerError,
    RuntimeNotificationOverflow,
    RuntimePayloadTooLarge,
    RuntimeProtocolError,
)
from theater.daemon.harness_runtime.frontend import (
    FrontendProtocolError,
    FrontendRuntimeHost,
    UnixFrontendConnection,
)
from theater.daemon.harness_runtime.manager import (
    HarnessRuntimeManager,
    ManagedRuntime,
)
from theater.daemon.harness_runtime.transport import (
    JsonRpcRuntimeConnection,
    RuntimeTransportStatistics,
    WebSocketRuntimeIO,
    endpoint_to_path,
    wait_for_unix_endpoint,
)

__all__ = [
    "BackendAlreadyLaunched",
    "BackendIdentityMismatch",
    "BackendLaunchError",
    "BackendProcessError",
    "BackendProcessIdentity",
    "DetachedBackendProcess",
    "FrontendProtocolError",
    "FrontendRuntimeHost",
    "HarnessRuntimeManager",
    "JsonRpcRuntimeConnection",
    "ManagedRuntime",
    "RuntimeConnectionSaturated",
    "RuntimeGenerationMismatch",
    "RuntimeHandshakeError",
    "RuntimeMalformedReply",
    "RuntimeManagerError",
    "RuntimeNotificationOverflow",
    "RuntimePayloadTooLarge",
    "RuntimeProtocolError",
    "RuntimeTransportStatistics",
    "UnixFrontendConnection",
    "WebSocketRuntimeIO",
    "adopt_detached_backend",
    "backend_artifacts_dir",
    "capture_process_identity",
    "endpoint_to_path",
    "launch_detached_backend",
    "pid_alive",
    "process_started_at",
    "verify_process_identity",
    "wait_for_unix_endpoint",
]
