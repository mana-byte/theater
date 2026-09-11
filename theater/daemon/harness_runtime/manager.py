"""The daemon runtime manager: one runtime instance per participant.

The manager owns the participant-to-runtime and participant-to-backend
relationships so later composition waves can wire spawning, controls, and
observation without re-implementing ownership rules:

* **Exactly one ``HarnessRuntime`` instance per participant.** Concurrent
  ``get_or_create`` callers for one participant share a single instance;
  creation runs under the participant's own lock, never a daemon-wide one —
  no lock is held across native I/O for a different participant.
* **Generation checks.** Runtime creation binds to one backend generation; a
  stale generation cannot replace, disconnect, or signal the current one.
  ``teardown`` with a mismatched generation fails closed: no disconnect, no
  signal.
* **Close-without-kill is explicit.** ``close`` disconnects a runtime and
  leaves its backend running; only ``teardown`` terminates a backend, and it
  verifies the backend's process identity before any signal.
* **History reads create nothing.** ``get`` returns the existing instance or
  ``None`` — no code path from a short-lived history read can create a
  runtime, launch a backend, or open a control connection.
* **Backend ownership.** ``launch_backend`` records the detached process for
  the participant's exact generation and refuses to orphan a live backend of
  a different generation; ``adopt_backend`` re-establishes ownership of an
  already-running backend from the persisted identity after a daemon
  restart; ``teardown`` is the only path that terminates it.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

from theater import timing
from theater.daemon.harness_runtime.backend import (
    BackendProcessIdentity,
    DetachedBackendProcess,
    adopt_detached_backend,
    launch_detached_backend,
)
from theater.daemon.harness_runtime.errors import (
    BackendAlreadyLaunched,
    RuntimeGenerationMismatch,
)
from theater.harness.contracts.runtime import HarnessRuntime, RuntimePlan
from theater.observability.catalog import RUNTIME_RECONNECT


async def _close_quietly(runtime: HarnessRuntime) -> None:
    """Disconnect best effort; teardown must proceed even if a close misbehaves."""
    with contextlib.suppress(Exception):
        await runtime.aclose()


async def _close_strictly(runtime: HarnessRuntime) -> None:
    await runtime.aclose()


@dataclass
class ManagedRuntime:
    """What the manager tracks for one participant."""

    participant_id: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    runtime: HarnessRuntime | None = None
    runtime_generation: int | None = None
    backend: DetachedBackendProcess | None = None
    backend_generation: int | None = None


RuntimeFactory = Callable[[], Awaitable[HarnessRuntime]]


class HarnessRuntimeManager:
    """Per-participant runtime registry and detached-backend ownership."""

    def __init__(self) -> None:
        # Only dict reads/writes touch this lock; it is never held across
        # native I/O or across a create() callback.
        self._registry: dict[str, ManagedRuntime] = {}
        self._registry_lock = asyncio.Lock()

    # ---- lookups (create nothing) ------------------------------------------

    def get(self, participant_id: str) -> HarnessRuntime | None:
        """The existing runtime for one participant, or ``None``.

        Deliberately synchronous and creation-free: history reads and other
        short-lived callers can never launch a backend or open a control
        connection through this path.
        """
        entry = self._registry.get(participant_id)
        if entry is None:
            return None
        return entry.runtime

    def backend(self, participant_id: str) -> DetachedBackendProcess | None:
        """The detached backend owned for one participant, or ``None``."""
        entry = self._registry.get(participant_id)
        if entry is None:
            return None
        return entry.backend

    def participants(self) -> tuple[str, ...]:
        """Every participant id the manager currently owns state for."""
        return tuple(self._registry)

    # ---- runtime lifecycle --------------------------------------------------

    async def get_or_create(
        self,
        participant_id: str,
        *,
        backend_generation: int,
        create: RuntimeFactory,
    ) -> HarnessRuntime:
        """Return this participant's one runtime, creating it at most once.

        Concurrent callers for one participant cannot create duplicates: the
        first caller builds the instance under the participant's lock and every
        other caller — including ones that arrived while creation was still in
        flight — receives that instance. A caller naming a different
        generation replaces the old runtime (disconnecting it, never
        terminating its backend) — unless a live backend of another generation
        owns the participant, in which case binding a runtime to a conflicting
        generation fails closed instead of stranding that backend.
        """
        while True:
            entry = await self._entry(participant_id)
            async with entry.lock:
                if self._unregistered(entry, participant_id):
                    continue  # a concurrent teardown removed this entry; retry
                if (
                    entry.backend is not None
                    and entry.backend.alive()
                    and entry.backend_generation != backend_generation
                ):
                    raise RuntimeGenerationMismatch(
                        f"participant {participant_id} has a live backend of generation "
                        f"{entry.backend_generation} at pid {entry.backend.pid}; refuse to "
                        f"bind a runtime of generation {backend_generation} to it — "
                        "teardown the live generation explicitly instead of stranding "
                        "its backend behind a new runtime"
                    )
                if entry.runtime is not None and entry.runtime_generation == backend_generation:
                    return entry.runtime
                stale = entry.runtime
                entry.runtime = None
                entry.runtime_generation = None
                if stale is not None:
                    await _close_strictly(stale)
                runtime = await create()
                entry.runtime = runtime
                entry.runtime_generation = backend_generation
                return runtime

    async def reconnect(
        self,
        participant_id: str,
        *,
        backend_generation: int,
        create: RuntimeFactory,
    ) -> HarnessRuntime:
        """Close the current runtime's connection and create a fresh instance.

        Reconnect is explicit and generation-preserving: the generation must
        match both recorded generations *before anything is disconnected*
        (a mismatch fails closed with the old runtime untouched), and the
        replacement is bound to the same backend generation, so the
        participant keeps one runtime instance pointed at the same verified
        backend. The timing span is instrumentation only: it measures the
        attempt (a generation mismatch reads as an error with the exact
        mismatch class) and never changes the generation checks or the
        close-without-kill semantics.
        """
        with timing.span(RUNTIME_RECONNECT, id=participant_id, source="runtime_manager"):
            while True:
                entry = await self._entry(participant_id)
                async with entry.lock:
                    if self._unregistered(entry, participant_id):
                        continue  # a concurrent teardown removed this entry; retry
                    self._require_generation(
                        entry,
                        participant_id=participant_id,
                        backend_generation=backend_generation,
                    )
                    stale = entry.runtime
                    entry.runtime = None
                    entry.runtime_generation = None
                    if stale is not None:
                        await _close_strictly(stale)
                    runtime = await create()
                    entry.runtime = runtime
                    entry.runtime_generation = backend_generation
                    return runtime

    async def close(self, participant_id: str) -> None:
        """Disconnect one participant's runtime; the backend stays alive."""
        entry = self._registry.get(participant_id)
        if entry is None:
            return
        async with entry.lock:
            stale = entry.runtime
            entry.runtime = None
            entry.runtime_generation = None
            if stale is not None:
                await _close_strictly(stale)

    async def aclose(self) -> None:
        """Disconnect every runtime; never terminate any backend.

        For daemon shutdown: runtime clients disconnect, healthy backends and
        their native UIs survive and are reconnected by the next daemon.
        """
        async with self._registry_lock:
            entries = list(self._registry.values())
        for entry in entries:
            async with entry.lock:
                stale = entry.runtime
                entry.runtime = None
                entry.runtime_generation = None
                if stale is not None:
                    await _close_quietly(stale)

    # ---- detached backend ownership ----------------------------------------

    async def launch_backend(
        self,
        participant_id: str,
        *,
        backend_generation: int,
        plan: RuntimePlan,
        cwd: Path,
    ) -> BackendProcessIdentity:
        """Launch one detached backend for this participant and generation.

        A live backend of a different generation is never silently replaced —
        tearing it down is an explicit ``teardown`` decision, so this fails
        loudly instead of orphaning a healthy backend.
        """
        while True:
            entry = await self._entry(participant_id)
            async with entry.lock:
                if self._unregistered(entry, participant_id):
                    continue  # a concurrent teardown removed this entry; retry
                self._refuse_second_backend(entry, participant_id, backend_generation)
                backend = await launch_detached_backend(
                    plan, participant_id=participant_id, cwd=cwd
                )
                entry.backend = backend
                entry.backend_generation = backend_generation
                return backend.identity

    async def adopt_backend(
        self,
        participant_id: str,
        *,
        backend_generation: int,
        pid: int,
        started_at: float,
        endpoint: str,
    ) -> BackendProcessIdentity:
        """Register an already-running backend a previous daemon launched.

        Restart recovery: the persisted binding (pid, ``backend_started_at``,
        endpoint, generation) is re-verified against the live process before
        anything is registered — a dead pid or a changed start identity raises
        ``BackendIdentityMismatch`` and leaves no state behind. The adopted
        handle owns no child-process object; every later signal re-verifies
        identity, and a live backend of another generation still refuses to be
        replaced, exactly like a fresh launch.
        """
        while True:
            entry = await self._entry(participant_id)
            async with entry.lock:
                if self._unregistered(entry, participant_id):
                    continue  # a concurrent teardown removed this entry; retry
                self._refuse_second_backend(entry, participant_id, backend_generation)
                try:
                    backend = await asyncio.to_thread(
                        adopt_detached_backend,
                        pid,
                        started_at=started_at,
                        endpoint=endpoint,
                        participant_id=participant_id,
                    )
                except BaseException:
                    await self._drop_entry_if_empty(participant_id, entry)
                    raise
                entry.backend = backend
                entry.backend_generation = backend_generation
                return backend.identity

    async def teardown(
        self,
        participant_id: str,
        *,
        backend_generation: int,
    ) -> None:
        """Explicitly tear one participant's runtime down: disconnect and terminate.

        The only path that terminates a backend. The named generation must
        match both the runtime and the backend records; a mismatch fails
        closed — no disconnect, no signal — because acting on a stale
        generation is how one participant's teardown lands on another
        generation's backend. The registry entry is removed under the
        registry lock while the participant lock is still held and only if
        this exact entry is still the registered one, so a concurrent
        ``get_or_create``/``launch_backend`` can never install state into an
        entry that is no longer reachable.
        """
        entry = self._registry.get(participant_id)
        if entry is None:
            return
        async with entry.lock:
            self._require_generation(
                entry,
                participant_id=participant_id,
                backend_generation=backend_generation,
            )
            stale = entry.runtime
            entry.runtime = None
            entry.runtime_generation = None
            if stale is not None:
                await _close_quietly(stale)
            if entry.backend is not None:
                await entry.backend.terminate()
                entry.backend = None
                entry.backend_generation = None
            if entry.runtime is None and entry.backend is None:
                await self._drop_entry_if_empty(participant_id, entry)

    # ---- internals ----------------------------------------------------------

    async def _entry(self, participant_id: str) -> ManagedRuntime:
        async with self._registry_lock:
            entry = self._registry.get(participant_id)
            if entry is None:
                entry = ManagedRuntime(participant_id=participant_id)
                self._registry[participant_id] = entry
            return entry

    def _unregistered(self, entry: ManagedRuntime, participant_id: str) -> bool:
        """Whether a concurrent teardown already removed this entry."""
        return self._registry.get(participant_id) is not entry

    async def _drop_entry_if_empty(self, participant_id: str, entry: ManagedRuntime) -> None:
        """Remove the registry entry, under the registry lock, if it is still
        the registered one and holds no live state."""
        if entry.runtime is not None or entry.backend is not None:
            return
        async with self._registry_lock:
            if self._registry.get(participant_id) is entry:
                del self._registry[participant_id]

    def _refuse_second_backend(
        self,
        entry: ManagedRuntime,
        participant_id: str,
        backend_generation: int,
    ) -> None:
        backend = entry.backend
        if backend is None or not backend.alive():
            return
        if entry.backend_generation != backend_generation:
            raise BackendAlreadyLaunched(
                f"participant {participant_id} already has a live backend of "
                f"generation {entry.backend_generation} at pid {backend.pid}; teardown "
                "that generation explicitly before launching another — orphaning a "
                "live backend is never automatic"
            )
        raise BackendAlreadyLaunched(
            f"participant {participant_id} already has a live backend of this "
            f"generation at pid {backend.pid}; never launch a second backend for "
            "one participant"
        )

    def _require_generation(
        self,
        entry: ManagedRuntime,
        *,
        participant_id: str,
        backend_generation: int,
    ) -> None:
        if entry.runtime_generation is not None and entry.runtime_generation != backend_generation:
            raise RuntimeGenerationMismatch(
                f"participant {participant_id} has a runtime of generation "
                f"{entry.runtime_generation}, not {backend_generation} — teardown refuses "
                "to disconnect or signal a backend generation it was not asked to remove; "
                "reconcile the persisted binding before retrying"
            )
        if entry.backend_generation is not None and entry.backend_generation != backend_generation:
            raise RuntimeGenerationMismatch(
                f"participant {participant_id} has a backend of generation "
                f"{entry.backend_generation}, not {backend_generation} — teardown refuses "
                "to signal a backend of another generation; never terminate a process you "
                "cannot positively identify"
            )


__all__ = [
    "HarnessRuntimeManager",
    "ManagedRuntime",
]
