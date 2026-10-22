"""An observer plus control service over one store with scripted sources, and its bus readers."""

from __future__ import annotations

from sqlalchemy import update

from tests.support.control_harness import make_runtime
from tests.support.fake_runtime import FakeRuntimeState
from tests.support.waiting import until
from theater.constants.daemon import SEND_CLAIM_TTL_SECONDS
from theater.daemon.controls import ControlGates, ControlService
from theater.daemon.jobs import JobManager
from theater.daemon.observation.live import LiveRegistration
from theater.daemon.observation.service import _WatchContext, _WatchIteration
from theater.daemon.observer import (
    Observer,
    QuietClock,
)
from theater.daemon.persistence.store import Store
from theater.daemon.schema import jobs as jobs_table
from theater.harness.contracts.channels import (
    ChannelDeclaration,
    ChannelKind,
)
from theater.harness.contracts.events import Event, EventKind
from theater.harness.contracts.observation import (
    HarnessObserver,
)
from theater.harness.contracts.runtime import (
    LiveChannelDeclaration,
    SessionOpenMode,
)
from theater.harness.source import Batch, Source
from theater.models import (
    ProviderRecord,
    TerminalBindingRecord,
    now,
)

LIVE_CHANNEL = ChannelDeclaration(id="fake-live", kind=ChannelKind.LIVE)


DURABLE_CHANNEL = ChannelDeclaration(id="transcript", kind=ChannelKind.TRANSCRIPT)


UNSET = object()


class ScriptedDurable(Source):
    """The durable reader handed over by the (monkeypatched) open hook."""

    def __init__(self) -> None:
        self.batches: list[Batch] = []
        self.closed = False

    async def read(self) -> Batch:
        return self.batches.pop(0) if self.batches else Batch()

    async def aclose(self) -> None:
        self.closed = True


def apply_batch(
    observer: Observer,
    pid: str,
    batch: Batch,
    clock: QuietClock,
    turns,
    *,
    source: Source | None = None,
) -> bool:
    """Exercise the watch's source-aware apply path with a stateless test source by default."""
    return observer._apply_source_batch(
        pid, source if source is not None else ScriptedDurable(), batch, clock, turns
    )


def make_watch_context(
    observer: Observer,
    pid: str,
    screen: HarnessObserver,
    source: Source,
    clock: QuietClock,
    turns,
) -> _WatchContext:
    registration = observer.live.registration_for(pid)
    return _WatchContext(
        pid=pid,
        observer=screen,
        source=source,
        registration=registration,
        opened_durable=True,
        completion=observer._bind_completion(registration),
        wake=None,
        clock=clock,
        turns=turns,
    )


async def run_quiet_step(
    observer: Observer,
    pid: str,
    screen: HarnessObserver,
    source: Source,
    clock: QuietClock,
    turns,
) -> _WatchIteration:
    """Exercise the production quiet step with the supplied source and watch state."""
    context = make_watch_context(observer, pid, screen, source, clock, turns)
    iteration = _WatchIteration(next_poll=observer.poll)
    await observer._quiet_step(context, iteration, Batch())
    return iteration


class FakeHarnessObserver(HarnessObserver):
    """A transcript observer whose durable source is injected by the rig."""

    def __init__(self, *, has_transcript: bool = True):
        self.has_transcript = has_transcript

    def primary_channel_declaration(self):
        return DURABLE_CHANNEL if self.has_transcript else None

    def is_idle_screen(self, capture: str) -> bool:
        return True


class FakeHarness:
    """Just the ``.observer`` half the observation service needs."""

    def __init__(self, *, has_transcript: bool = True):
        self.observer = FakeHarnessObserver(has_transcript=has_transcript)


class RecordingJobs(JobManager):
    """JobManager recording finishes, path attribution, and the evidence gate."""

    def __init__(self, store: Store):
        super().__init__(store)
        self.store = store
        self.finishes: list[str] = []
        self.path_touches: list[tuple[str, tuple[str, ...]]] = []
        #: job handle -> evidence key that must be committed when finish runs.
        self.evidence_gate: dict[str, tuple[str, int, str, str]] = {}

    def finish(self, handle: str, **kwargs):
        if handle in self.evidence_gate:
            participant_id, generation, session, turn = self.evidence_gate[handle]
            assert (
                self.store.get_native_terminal_evidence(
                    participant_id=participant_id,
                    backend_generation=generation,
                    native_session_id=session,
                    native_turn_id=turn,
                )
                is not None
            ), "job finish ran before the evidence commit"
        self.finishes.append(handle)
        return super().finish(handle, **kwargs)

    def observe_paths(self, handle: str, paths) -> None:
        self.path_touches.append((handle, tuple(p.path for p in paths)))
        super().observe_paths(handle, paths)


def minimal_gates() -> ControlGates:
    async def noop(*args, **kwargs) -> None:
        return None

    return ControlGates(
        authorize=lambda *args: None,
        require_absent=noop,
        check_absent=lambda participant_id: None,
        send_preflight=noop,
        send_claim_now=now,
        send_claim_ttl=lambda: SEND_CLAIM_TTL_SECONDS,
        check_prompt=lambda prompt: None,
        check_settings=lambda model, effort: None,
        cwd_for=lambda participant_id: "/tmp",
    )


class Rig:
    """One observer plus one control service over one shared store."""

    def __init__(
        self,
        store: Store,
        registry,
        monkeypatch,
        *,
        poll: float = 0.02,
        awaiting: float = 1.5,
    ):
        from theater.daemon.observer import Observer

        self.store = store
        self.registry = registry
        self.durable = ScriptedDurable()
        self.jobs = RecordingJobs(store)
        self.runtime = make_runtime("p1")
        self.observer = Observer(
            registry,
            {"fake": FakeHarness()},
            source_factory=lambda observer, **kwargs: self.durable,
            poll=poll,
            search=poll,
            sync=poll,
            awaiting=awaiting,
            jobs=self.jobs,
        )
        self.service = ControlService(
            store=store,
            jobs=self.jobs,
            runtime_for=lambda pid: self.runtime if pid == "p1" else None,
            gates=minimal_gates(),
        )
        self.observer.start()

    async def open(self) -> None:
        await self.runtime.open_session(mode=SessionOpenMode.NEW)

    @property
    def state(self) -> FakeRuntimeState:
        return self.runtime.state

    @property
    def session(self) -> str:
        assert self.state.native_session_id is not None
        return self.state.native_session_id

    def register_live(self, *, evidence_sink=UNSET, native_session_id=None) -> None:
        self.observer.live.register(
            LiveRegistration(
                participant_id="p1",
                live_source=self.runtime.live_source(),
                channel=LiveChannelDeclaration(channel=LIVE_CHANNEL),
                backend_generation=self.state.backend_generation,
                native_session_id=native_session_id or self.session,
                evidence_sink=(
                    self.service.record_terminal_evidence
                    if evidence_sink is UNSET
                    else evidence_sink
                ),
                active_job_for_turn=self.service.active_job_for_native_turn,
            )
        )

    async def aclose(self) -> None:
        await self.observer.aclose()
        await self.runtime.aclose()

    def job(self, handle: str):
        return self.store.get_job(handle)

    async def send(self, prompt: str = "do it"):
        return await self.service.send("p1", caller_id="caller", prompt=prompt)

    async def warm_up(self) -> None:
        """Prove the durable watch is alive with one durable event."""
        self.durable.batches.append(Batch(events=(Event(kind=EventKind.USER, text="warmup"),)))
        assert await until(lambda: "agent.user" in bus_kinds(self.store))


def bus_kinds(store, prefix="agent.") -> list[str]:
    return [row["kind"] for row in store.bus_tail(limit=500) if row["kind"].startswith(prefix)]


class ScreenObserver(HarnessObserver):
    """Just enough observer for `_rescue_jobs`, which reads `screen_reading`.

    Inherits the default `screen_reading` shim, which derives a reading from
    `is_idle_screen`. An observer and not a harness: rescue is observation,
    so the reducer is handed only that half and never sees the object that
    launches anything.
    """

    has_transcript = True

    def __init__(self, idle: bool):
        self.idle = idle

    def is_idle_screen(self, capture: str) -> bool:
        return self.idle


def poised(
    registry,
    *,
    provider_bound=True,
    idle=True,
    capture="$ ",
    prompt: str | None = None,
    response_format: str | None = None,
    **observer_options,
):
    """A participant with one running job, and an observer poised to rescue it.

    `capture=None` stands for a pane that could not be read at all.
    """
    from theater.daemon.jobs import JobManager

    jobs = JobManager(registry.store)
    p = registry.register(harness="vibe", pane=None, cwd="/tmp")
    if provider_bound:
        timestamp = now()
        with registry.store.write_unit() as unit:
            if (
                registry.store.providers.get("observer-provider", connection=unit.connection)
                is None
            ):
                registry.store.providers.register(
                    ProviderRecord(
                        provider_id="observer-provider",
                        selector="observer-provider",
                        kind="test",
                        credential_verifier="f" * 64,
                        configuration_version=1,
                        capabilities=("terminal-provider.v1",),
                        limits={},
                        generation=1,
                        last_report_revision=1,
                        created_at=timestamp,
                        updated_at=timestamp,
                    ),
                    connection=unit.connection,
                )
            registry.store.terminal_bindings.bind(
                TerminalBindingRecord(
                    participant_id=p.id,
                    provider_id="observer-provider",
                    provider_generation=1,
                    terminal_id=f"terminal-{p.id}",
                    terminal_incarnation="incarnation-1",
                    occupant_evidence={"occupant_id": p.id, "harness": "vibe"},
                    process_facts=None,
                    health="healthy",
                    report_revision=1,
                    created_at=timestamp,
                    updated_at=timestamp,
                ),
                connection=unit.connection,
            )
    jobs.create(
        handle="h1",
        caller_id="caller",
        target_id=p.id,
        kind="send",
        prompt=prompt,
        response_format=response_format,
    )
    observer = Observer(registry, harnesses={}, jobs=jobs, **observer_options)

    async def capture_pane(_pane):
        return capture

    observer._capture = capture_pane
    cursor = QuietClock()
    cursor.last_text = "the last thing it said"
    return observer, ScreenObserver(idle), cursor, p, jobs


def age_job(registry, handle: str, age: float) -> None:
    registry.store.conn.execute(
        update(jobs_table).where(jobs_table.c.handle == handle).values(created_at=now() - age)
    )
