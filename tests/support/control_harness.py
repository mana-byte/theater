"""One control service over one store with fake native runtimes, plus the recording gates."""

from __future__ import annotations

from pathlib import Path

from tests.support.fake_runtime import FakeRuntime, FakeRuntimeIO, FakeRuntimeState
from theater.constants.daemon import SEND_CLAIM_TTL_SECONDS
from theater.daemon.controls import ControlGates, ControlService
from theater.daemon.jobs import JobManager
from theater.daemon.persistence.repositories.control_operations import ControlOperation
from theater.daemon.persistence.repositories.runtime_bindings import ParticipantRuntimeBinding
from theater.daemon.persistence.store import Store
from theater.harness.contracts.runtime import (
    ControlDeliveryPhase,
    ControlKind,
    ControlTransport,
    RuntimeContext,
    RuntimeLifecyclePhase,
    RuntimeWiring,
    SessionOpenMode,
)
from theater.models import (
    HumanPresent,
    Job,
    NotYourChild,
    StaleTarget,
    now,
)


def make_runtime(participant_id: str, *, generation: int = 1) -> FakeRuntime:
    """A fake runtime sharing its state with the injected fake I/O."""
    state = FakeRuntimeState(participant_id=participant_id, backend_generation=generation)
    context = RuntimeContext(
        participant_id=participant_id,
        cwd=None,
        io=FakeRuntimeIO(state),
        backend_generation=generation,
        endpoint=f"unix:///tmp/{participant_id}.sock",
        config_path=Path(f"/tmp/{participant_id}-config.json"),
    )
    runtime = FakeRuntime(context)
    runtime.state.backend_alive = True
    return runtime


def wrap_runtime(base: FakeRuntime, wrapper_cls: type[FakeRuntime]) -> FakeRuntime:
    """The same shared state behind a runtime subclass with canned behaviour."""
    wrapped = wrapper_cls(base.context)
    wrapped.state = base.state
    return wrapped


class RecordingGates:
    """All the injected gates, recording what the service asked of them."""

    def __init__(self):
        self.authorized: list[tuple[str, str, str]] = []
        self.absence_checks: list[str] = []
        self.preflights: list[str] = []
        self.prompt_checks: list[str] = []
        self.settings_checks: list[tuple[str | None, str | None]] = []
        self.refuse_dispatch_callers: set[str] = set()
        self.refuse_preflight_for: set[str] = set()
        #: Participants whose presence gate raises a temporary refusal.
        self.presence_refusals: set[str] = set()
        #: Callers the authorize gate refuses for every action.
        self.refuse_authorize_for: set[str] = set()

    def gates(self) -> ControlGates:
        async def require_absent(participant_id: str) -> None:
            check_absent(participant_id)

        def check_absent(participant_id: str) -> None:
            self.absence_checks.append(participant_id)
            if participant_id in self.presence_refusals:
                raise HumanPresent(f"human focus protects {participant_id!r}")

        async def send_preflight(participant_id: str) -> None:
            self.preflights.append(participant_id)
            if participant_id in self.refuse_preflight_for:
                raise StaleTarget(f"pane of {participant_id!r} no longer exists")

        def authorize(participant_id: str, caller_id: str, action: str) -> None:
            self.authorized.append((participant_id, caller_id, action))
            if caller_id in self.refuse_authorize_for:
                raise NotYourChild(
                    f"refusing to control {participant_id!r} for {caller_id!r}: "
                    "only the direct parent or an operator may"
                )
            if action == "queue_dispatch" and caller_id in self.refuse_dispatch_callers:
                raise NotYourChild(
                    f"refusing to dispatch a queued followup of {participant_id!r} "
                    f"for {caller_id!r}: that caller no longer owns the participant"
                )

        def check_prompt(prompt: str) -> None:
            self.prompt_checks.append(prompt)

        def check_settings(model: str | None, reasoning_effort: str | None) -> None:
            self.settings_checks.append((model, reasoning_effort))

        def cwd_for(participant_id: str) -> str | None:
            return "/tmp/wk"

        return ControlGates(
            authorize=authorize,
            require_absent=require_absent,
            check_absent=check_absent,
            send_preflight=send_preflight,
            send_claim_now=now,
            send_claim_ttl=lambda: SEND_CLAIM_TTL_SECONDS,
            check_prompt=check_prompt,
            check_settings=check_settings,
            cwd_for=cwd_for,
            check_settings_for=lambda participant_id, model, effort: None,
        )


class SpyJobs(JobManager):
    """Records finish calls; optionally asserts evidence-before-finish."""

    def __init__(self, store: Store):
        super().__init__(store)
        self.store = store
        self.finishes: list[tuple[str, str]] = []
        #: job handle -> evidence key that must be committed when finish runs.
        self.evidence_gate: dict[str, tuple[str, int, str, str]] = {}

    def finish(self, handle: str, **kwargs) -> Job | None:
        if handle in self.evidence_gate:
            participant_id, generation, session, turn = self.evidence_gate[handle]
            evidence = self.store.get_native_terminal_evidence(
                participant_id=participant_id,
                backend_generation=generation,
                native_session_id=session,
                native_turn_id=turn,
            )
            assert evidence is not None, "job finish ran before the evidence commit"
        self.finishes.append((handle, str(kwargs.get("state"))))
        return super().finish(handle, **kwargs)


class Harness:
    """One control service over one store, with per-participant fake runtimes."""

    def __init__(self, store: Store, participants: dict[str, FakeRuntime]):
        self.store = store
        self.jobs = SpyJobs(store)
        self.runtimes = dict(participants)
        self.gates_recorder = RecordingGates()
        self.service = ControlService(
            store=store,
            jobs=self.jobs,
            runtime_for=self.runtimes.get,
            gates=self.gates_recorder.gates(),
        )


async def open_harness(store: Store, *participant_ids: str) -> Harness:
    harness = Harness(store, {pid: make_runtime(pid) for pid in participant_ids})
    for runtime in harness.runtimes.values():
        await runtime.open_session(mode=SessionOpenMode.NEW)
    return harness


def state_of(harness: Harness, participant_id: str) -> FakeRuntimeState:
    return harness.runtimes[participant_id].state


def make_operation(
    operation_id: str,
    *,
    participant_id: str = "p1",
    job_handle: str | None,
    kind: ControlKind,
    transport: ControlTransport,
    phase: ControlDeliveryPhase = ControlDeliveryPhase.RESERVED,
    backend_generation: int | None = 1,
    native_session_id: str | None = None,
    native_turn_id: str | None = None,
) -> ControlOperation:
    return ControlOperation(
        operation_id=operation_id,
        participant_id=participant_id,
        kind=kind,
        transport=transport,
        delivery_phase=phase,
        job_handle=job_handle,
        backend_generation=backend_generation,
        native_session_id=native_session_id,
        native_turn_id=native_turn_id,
        created_at=now(),
        updated_at=now(),
    )


def disconnected_native_harness(store: Store, *participant_ids: str) -> Harness:
    """A persisted native binding with no live runtime: detached/recovering."""
    harness = Harness(store, {})  # runtime_for is None for every participant
    for pid in participant_ids:
        store.upsert_runtime_binding(
            ParticipantRuntimeBinding(
                participant_id=pid,
                harness="codex",
                wiring=RuntimeWiring.NATIVE,
                backend_generation=1,
                lifecycle=RuntimeLifecyclePhase.BOUND,
                created_at=now(),
                updated_at=now(),
            )
        )
    return harness
