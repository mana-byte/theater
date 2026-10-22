"""Adopt and stop only the persisted detached backend process."""

from __future__ import annotations

from typing import cast

from theater.daemon.harness_runtime.backend import BackendProcessIdentity
from theater.daemon.harness_runtime.errors import BackendIdentityMismatch
from theater.daemon.harness_runtime.manager import HarnessRuntimeManager


class BackendAdoptionFailed(RuntimeError):
    """The persisted backend could not be adopted for stopping."""


class BackendTeardownFailed(RuntimeError):
    """The adopted backend could not be stopped."""


def persisted_backend_identity(
    pid: int | None, started_at: float | None
) -> BackendProcessIdentity | None:
    if pid is None or started_at is None:
        return None
    return BackendProcessIdentity(pid=pid, started_at=started_at)


async def stop_exact_backend(
    manager: HarnessRuntimeManager,
    participant_id: str,
    *,
    backend_generation: int,
    identity: BackendProcessIdentity,
    endpoint: str | None,
) -> None:
    """Keep adoption and teardown failures distinct for caller-owned cleanup."""
    if manager.backend(participant_id) is None:
        try:
            await manager.adopt_backend(
                participant_id,
                backend_generation=backend_generation,
                pid=identity.pid,
                started_at=cast(float, identity.started_at),
                endpoint=cast(str, endpoint),
            )
        except BackendIdentityMismatch:
            raise
        except Exception as exc:
            raise BackendAdoptionFailed from exc
    try:
        await manager.teardown(participant_id, backend_generation=backend_generation)
    except Exception as exc:
        raise BackendTeardownFailed from exc
