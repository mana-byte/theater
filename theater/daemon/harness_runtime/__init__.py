"""Shared runtime engine: transport, detached backend ownership, runtime manager.

Daemon-owned implementation behind the frozen runtime contracts; plugin code never
imports it and receives the engine only through ``theater.harness.contracts.runtime``.
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
