"""A daemon-level rig with scripted presence, and the control-operation rows it leaves."""

from __future__ import annotations

import asyncio

from sqlalchemy import select

from tests.support.fake_runtime import FakeRuntime, FakeRuntimeIO, FakeRuntimeState
from tests.support.presence import (
    _BasePresence,
)
from theater.constants.daemon import SEND_CLAIM_TTL_SECONDS
from theater.daemon.controls import ControlGates, ControlService
from theater.daemon.jobs import JobManager
from theater.daemon.schema import control_operations as control_operations_table
from theater.harness.contracts.runtime import (
    RuntimeContext,
)
from theater.models import HumanPresent, now

_OWNED_SERVICES: list[ControlService] = []


class PresenceGates:
    """A ControlGates presence seam with per-target scripted refusals."""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {}
        #: Targets whose presence gate always refuses.
        self.refusals: set[str] = set()
        #: Targets refused only after N passing calls (focus arrives mid-flight).
        self.passes_before_refusal: dict[str, int] = {}
        #: Real provider double; when set, it decides every check.
        self.provider: _BasePresence | None = None
        #: Optional exception send_preflight raises after signaling preflight_parked.
        self.preflight_exception: Exception | None = None
        self.preflight_parked = asyncio.Event()

    async def require_absent(self, participant_id: str) -> None:
        if self.provider is not None:
            await self.provider.require_absent(participant_id)
            return
        self.check_absent(participant_id)

    def check_absent(self, participant_id: str) -> None:
        if self.provider is not None:
            if self.provider.snapshot(participant_id).protected:
                raise HumanPresent(f"human focus protects {participant_id!r}")
            return
        calls = self.calls.get(participant_id, 0) + 1
        self.calls[participant_id] = calls
        passes = self.passes_before_refusal.get(participant_id)
        if participant_id in self.refusals or (passes is not None and calls > passes):
            raise HumanPresent(f"human focus protects {participant_id!r}")

    def gates(self) -> ControlGates:
        return ControlGates(
            authorize=lambda *args: None,
            require_absent=self.require_absent,
            check_absent=self.check_absent,
            send_preflight=self._send_preflight,
            send_claim_now=now,
            send_claim_ttl=lambda: SEND_CLAIM_TTL_SECONDS,
            check_prompt=lambda prompt: None,
            check_settings=lambda model, effort: None,
            cwd_for=lambda participant_id: None,
        )

    async def _send_preflight(self, participant_id: str) -> None:
        if self.preflight_exception is not None:
            self.preflight_parked.set()
            await asyncio.sleep(0)
            raise self.preflight_exception


class Rig:
    """One control service over one store with per-participant fake runtimes."""

    def __init__(self, store, *pids: str):
        self.store = store
        self.jobs = JobManager(store)
        self.presence = PresenceGates()
        self.runtimes = {}
        for pid in pids:
            state = FakeRuntimeState(participant_id=pid, backend_generation=1)
            state.native_session_id = f"thread-{pid}"
            state.backend_alive = True
            context = RuntimeContext(
                participant_id=pid,
                cwd=None,
                io=FakeRuntimeIO(state),
                backend_generation=1,
            )
            self.runtimes[pid] = FakeRuntime(context)
        self.service = ControlService(
            store=store,
            jobs=self.jobs,
            runtime_for=self.runtimes.get,
            gates=self.presence.gates(),
        )
        _OWNED_SERVICES.append(self.service)

    def state(self, pid: str) -> FakeRuntimeState:
        return self.runtimes[pid].state


def operation_rows(store, pid: str) -> list[dict]:
    return [
        dict(row._mapping)
        for row in store.conn.execute(
            select(control_operations_table).where(control_operations_table.c.participant_id == pid)
        ).fetchall()
    ]
