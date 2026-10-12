"""Deferred spawns: admit, wait durably for the source job, then launch through the normal path."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

from sqlalchemy import select

from theater import timing
from theater.constants.daemon import CONTROL_QUEUE_MAX_PENDING
from theater.constants.handoff import (
    ERROR_CANCELLED,
    ERROR_LAUNCH_REFUSED,
    ERROR_SOURCE_GONE,
    ERROR_SOURCE_NOT_REVIEWABLE,
    GATE_CANCELLED,
    GATE_FAILED,
    GATE_OPENED,
    GATE_OPENING,
    GATE_WAITING,
    KIND_SEND,
    KIND_SPAWN,
    MAX_CHAIN,
    MAX_WAITING_PER_CALLER,
    PATHS_MAX,
    RETRY_MAX_SECONDS,
    RETRY_MIN_SECONDS,
    TASK_KIND_REVIEW,
    TASK_KIND_WORK,
    TASK_KINDS,
    TRANSIENT_PROVIDER_REASONS,
)
from theater.daemon.handoff.card import SourceView, render_card
from theater.daemon.handoff.evidence import EvidencePinner
from theater.daemon.handoff.models import HandoffGate
from theater.daemon.handoff.resolution import participant_of, resolve_source
from theater.daemon.schema import launch_reservations
from theater.daemon.skill_cards import compose_prompt
from theater.daemon.terminals import ProviderBusy, ProviderUnavailable
from theater.models import BadRequest, Job, JobState, Status, TheaterError, now
from theater.observability.catalog import HANDOFF_GATE

logger = logging.getLogger("theater.handoff")


class HandoffRefused(TheaterError):
    """The gate cannot open as asked; the task never launches and its job ends crashed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class GateReadiness:
    """Whether a queued send may be dispatched now, must wait, or can never be."""

    waiting: bool = False
    error: TheaterError | None = None


def _provider_is_only_away(exc: TheaterError) -> bool:
    """A busy provider or one that is offline or reconnecting; not a misconfigured one."""
    if isinstance(exc, ProviderBusy):
        return True
    reason = (exc.details or {}).get("reason") if hasattr(exc, "details") else None
    return reason in TRANSIENT_PROVIDER_REASONS


_READY = GateReadiness()
_WAITING = GateReadiness(waiting=True)


class HandoffService:
    """Owns the durable gate between a deferred spawn and the source job it waits for."""

    def __init__(self, daemon) -> None:
        self.daemon = daemon
        self.store = daemon.store
        self._tasks: set[asyncio.Task] = set()
        #: Gates whose launch was refused only because the provider is away; retried with backoff.
        self._retrying: set[str] = set()
        self._retry_task: asyncio.Task | None = None
        daemon.jobs.evidence = EvidencePinner(daemon.store)
        daemon.jobs.add_terminal_listener(self._on_terminal)

    # ---- admission ----------------------------------------------------------

    def admit_spawn(
        self,
        params: dict,
        *,
        task: str,
        response_format: str | None,
        skills: str | None = None,
    ) -> dict:
        """Reserve the child and its one task handle now; launch only when the source ends."""
        kind = params.get("task_kind") or TASK_KIND_WORK
        self._validate(params, task, kind)
        source, source_participant = resolve_source(self.daemon, params.get("after"))
        caller = params.get("parent_id") or "cli"
        if self.store.count_open_handoff_gates(caller) >= MAX_WAITING_PER_CALLER:
            raise BadRequest(
                f"{caller} already has {MAX_WAITING_PER_CALLER} tasks waiting on sources; await "
                "or cancel one before deferring another"
            )
        from theater.daemon.spawning.provider_launch import ParticipantLaunchService

        ParticipantLaunchService(self.daemon).require_known_provider(params.get("provider"))
        stored = {
            "kind": KIND_SPAWN,
            "params": {k: params.get(k) for k in _REQUEST_KEYS},
            "task": task,
            "response_format": response_format,
            **({"skills": skills} if skills else {}),
        }
        gate = HandoffGate(
            job_handle="",
            caller_id=caller,
            source_handle=source.handle,
            source_participant_id=source.target_id,
            source_name=source_participant.name if source_participant is not None else None,
            task_kind=kind,
            request=json.dumps(stored, sort_keys=True),
            state=GATE_WAITING,
            error_code=None,
            error=None,
            created_at=0.0,
            updated_at=0.0,
        )
        participant = ParticipantLaunchService(self.daemon).reserve_deferred_spawn(
            harness=str(params["harness"]),
            cwd=str(params["cwd"]),
            parent_id=params.get("parent_id"),
            name=params.get("name"),
            description=params.get("description"),
            task=task,
            response_format=response_format,
            gate=gate,
        )
        self._schedule(participant.id)
        result = participant.to_dict()
        result.update(
            {
                "addressable": False,
                "handle": participant.id,
                "operation_state": "accepted",
                "handoff": self.describe(participant.id),
            }
        )
        return result

    # ---- send ---------------------------------------------------------------

    def resolve_send_source(self, params: dict, task: str) -> tuple[Job, str | None]:
        """Validate ``after`` on a send and resolve it once; returns the source job and its name."""
        kind = params.get("task_kind") or TASK_KIND_WORK
        if kind == TASK_KIND_REVIEW:
            raise BadRequest(
                "independent_review needs its own isolated worktree on the source's exact "
                "commit: use spawn_session with after, not send"
            )
        if kind != TASK_KIND_WORK:
            raise BadRequest(f"task_kind must be one of {', '.join(TASK_KINDS)}; got {kind!r}")
        if not params.get("after"):
            raise BadRequest("task_kind needs after: name the source it is about")
        if (params.get("if_busy") or "auto") != "auto":
            raise BadRequest(
                "after works with the default send: Theater queues the message until the source "
                "ends, so if_busy must be omitted"
            )
        if params.get("job_handle") is not None:
            raise BadRequest(
                "after cannot be combined with job_handle: the handoff creates its own "
                "awaitable handle after resolving the source"
            )
        if not task.strip():
            raise BadRequest("after needs a prompt: state the task, Theater supplies the context")
        source, participant = resolve_source(self.daemon, params.get("after"))
        return source, participant.name if participant is not None else None

    def send_gate(
        self,
        source: Job,
        source_name: str | None,
        *,
        task: str,
        response_format: str | None,
        skills: str | None = None,
    ) -> HandoffGate:
        """The gate row for a send waiting on ``source``; the queue fills in its job handle."""
        request = {"kind": KIND_SEND, "task": task, "response_format": response_format}
        if skills:
            request["skills"] = skills
        return HandoffGate(
            job_handle="",
            caller_id=None,
            source_handle=source.handle,
            source_participant_id=source.target_id,
            source_name=source_name,
            task_kind=TASK_KIND_WORK,
            request=json.dumps(request, sort_keys=True),
            state=GATE_WAITING,
            error_code=None,
            error=None,
            created_at=0.0,
            updated_at=0.0,
        )

    def send_prompt_now(
        self, source: Job, source_name: str | None, task: str, rf, skills: str | None = None
    ) -> str:
        """The prompt for a send whose source already ended: no gate, the card is rendered now."""
        gate = self.send_gate(source, source_name, task=task, response_format=rf, skills=skills)
        return self._render_send(gate, source)

    def _render_send(self, gate: HandoffGate, source: Job) -> str:
        stored = json.loads(gate.request)
        return compose_prompt(
            stored["task"],
            card=render_card(gate.task_kind, self._source_view(gate, source)),
            skills=stored.get("skills"),
            response_format=stored["response_format"],
        )

    def ready_for_dispatch(self, job: Job) -> GateReadiness:
        """Called by the dispatcher before every delivery: open the gate or say why not."""
        if job.kind != KIND_SEND:
            return _READY
        gate = self.store.get_handoff_gate(job.handle)
        if gate is None or gate.state != GATE_WAITING:
            return _READY
        source = self.store.get_job(gate.source_handle)
        if source is None:
            message = "the source job no longer exists"
            self._close(gate, GATE_FAILED, ERROR_SOURCE_GONE, message)
            return GateReadiness(error=HandoffRefused(ERROR_SOURCE_GONE, message))
        if source.state == JobState.RUNNING:
            return _WAITING
        prompt = self._render_send(gate, source)
        with self.store.write_unit() as unit:
            opened = self.store.move_handoff_gate(
                gate.job_handle,
                expected=(GATE_WAITING,),
                state=GATE_OPENED,
                updated_at=now(),
                connection=unit.connection,
            )
            if opened:
                self.store.set_job_prompt(gate.job_handle, prompt, connection=unit.connection)
        if opened:
            self._signal(gate, "success")
        return _READY

    @staticmethod
    def _validate(params: dict, task: str, kind: object) -> None:
        if kind not in TASK_KINDS:
            raise BadRequest(f"task_kind must be one of {', '.join(TASK_KINDS)}; got {kind!r}")
        if not params.get("after"):
            raise BadRequest("task_kind needs after: name the source it is about")
        if not task.strip():
            raise BadRequest("after needs a prompt: state the task, Theater supplies the context")
        if params.get("resume"):
            raise BadRequest("after cannot be combined with resume: a resumed session has its own")
        if kind == TASK_KIND_REVIEW:
            if params.get("worktree") is not True:
                raise BadRequest(
                    "independent_review needs worktree=true: it reviews an isolated checkout of "
                    "the source's exact commit, never a shared or borrowed directory"
                )
            if params.get("base_branch"):
                raise BadRequest(
                    "independent_review fixes its own base: the source's pinned commit, so "
                    "base_branch must be omitted"
                )

    # ---- waiting and opening --------------------------------------------------

    def _on_terminal(self, handle: str) -> None:
        """Release every task waiting on this job; cancel a gate whose own job ended first."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        for gate in self.store.handoff_gates((GATE_WAITING,), source_handle=handle):
            self._release(gate, self.store.get_job(gate.job_handle))
        own = self.store.get_handoff_gate(handle)
        if own is not None and own.state == GATE_WAITING:
            self._close(
                own,
                GATE_CANCELLED,
                ERROR_CANCELLED,
                "the task ended before it launched",
                expected=(GATE_WAITING,),
            )

    def _release(self, gate: HandoffGate, job: Job | None) -> None:
        """The source ended: a send is handed to its dispatcher, a spawn to its launch."""
        if job is not None and job.kind == KIND_SEND and job.target_id is not None:
            # The dispatcher owns a send's gate: it checks readiness itself before delivery.
            controls = getattr(self.daemon, "controls", None)
            if controls is not None:
                controls.schedule_dispatch(job.target_id)
        else:
            self._schedule(gate.job_handle)

    def wake(self) -> None:
        """Re-examine every waiting gate: for jobs ended behind the listener's back (the GC).

        A job the retention sweep marks crashed is written directly, so no terminal listener
        ran; without this its dependents would wait for a restart.
        """
        for gate in self.store.handoff_gates((GATE_WAITING,)):
            job = self.store.get_job(gate.job_handle)
            if job is None or job.state != JobState.RUNNING:
                self._close(
                    gate,
                    GATE_CANCELLED,
                    ERROR_CANCELLED,
                    "the task ended before it launched",
                    expected=(GATE_WAITING,),
                )
                continue
            source = self.store.get_job(gate.source_handle)
            if source is None or source.state != JobState.RUNNING:
                self._release(gate, job)

    def _schedule(self, handle: str) -> None:
        try:
            task = asyncio.get_running_loop().create_task(self._open_guarded(handle))
        except RuntimeError:
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def settle(self) -> None:
        """Wait for scheduled gate openings (tests and shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def _open_guarded(self, handle: str) -> None:
        try:
            await self._open(handle)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("handoff gate %s failed to open", handle)

    async def _open(self, handle: str) -> None:
        gate = self.store.get_handoff_gate(handle)
        if gate is None or gate.state != GATE_WAITING:
            return
        source = self.store.get_job(gate.source_handle)
        if source is None:
            self._fail(gate, ERROR_SOURCE_GONE, "the source job no longer exists")
            return
        if source.state == JobState.RUNNING:
            return
        job = self.store.get_job(handle)
        if job is None or job.state != JobState.RUNNING:
            self._close(gate, GATE_CANCELLED, ERROR_CANCELLED, "the task ended before it launched")
            return
        if not self.store.move_handoff_gate(
            handle, expected=(GATE_WAITING,), state=GATE_OPENING, updated_at=now()
        ):
            return
        try:
            await self._launch(gate, source)
        except HandoffRefused as exc:
            self._fail(gate, exc.code, str(exc))
        except (ProviderUnavailable, ProviderBusy) as exc:
            if _provider_is_only_away(exc):
                self._wait_for_provider(gate, exc)
            else:
                self._fail(gate, ERROR_LAUNCH_REFUSED, f"{type(exc).__name__}: {exc}")
        except (TheaterError, ValueError) as exc:
            self._fail(gate, ERROR_LAUNCH_REFUSED, f"{type(exc).__name__}: {exc}")
        else:
            self.store.move_handoff_gate(
                handle, expected=(GATE_OPENING,), state=GATE_OPENED, updated_at=now()
            )
            self._signal(gate, "success")

    def _launch_reserved(self, handle: str) -> bool:
        return (
            self.store.conn.execute(
                select(launch_reservations.c.operation_id).where(
                    launch_reservations.c.participant_id == handle
                )
            ).first()
            is not None
        )

    def _wait_for_provider(self, gate: HandoffGate, exc: TheaterError) -> None:
        """The provider is away: keep the gate waiting and retry. Nothing was launched.

        A refusal here happens before the launch is accepted, so no reservation exists and a
        retry cannot replay one; if one does exist the launch is already the operation's.
        """
        if self._launch_reserved(gate.job_handle):
            self.store.move_handoff_gate(
                gate.job_handle, expected=(GATE_OPENING,), state=GATE_OPENED, updated_at=now()
            )
            return
        self.store.move_handoff_gate(
            gate.job_handle,
            expected=(GATE_OPENING,),
            state=GATE_WAITING,
            updated_at=now(),
            error_code=exc.code,
            error=str(exc)[:1000],
        )
        self._retrying.add(gate.job_handle)
        self._arm_retry()
        logger.warning("handoff gate %s waits for its provider: %s", gate.job_handle, exc)

    def _arm_retry(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._retry_task is None or self._retry_task.done():
            self._retry_task = loop.create_task(self._retry_loop())

    async def _retry_loop(self) -> None:
        delay = RETRY_MIN_SECONDS
        while self._retrying:
            await asyncio.sleep(delay)
            for handle in list(self._retrying):
                self._retrying.discard(handle)  # _open puts it back if the provider is still away
                await self._open_guarded(handle)
            delay = min(delay * 2, RETRY_MAX_SECONDS) if self._retrying else RETRY_MIN_SECONDS

    async def aclose(self) -> None:
        tasks = [*self._tasks, *([self._retry_task] if self._retry_task else [])]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _launch(self, gate: HandoffGate, source) -> None:
        from theater.daemon.spawning.provider_launch import ParticipantLaunchService

        stored = json.loads(gate.request)
        params, task, response_format = stored["params"], stored["task"], stored["response_format"]
        view = self._source_view(gate, source)
        base = params.get("base_branch")
        if gate.task_kind == TASK_KIND_REVIEW:
            base = await self._review_base(view, str(params["cwd"]))
        prompt = compose_prompt(
            task,
            card=render_card(gate.task_kind, view),
            skills=stored.get("skills"),
            response_format=response_format,
        )
        request = {
            "harness": params["harness"],
            "approval": params["approval"],
            "workspace": {
                "cwd": params["cwd"],
                "worktree": params.get("worktree") or False,
                "base_ref": base,
            },
            "initiating_participant_id": params.get("parent_id"),
            "model": params.get("model"),
            "reasoning_effort": params.get("reasoning_effort"),
            "resume": None,
            "name": None,
            "description": params.get("description"),
            "prompt": prompt,
        }
        if params.get("provider"):
            request["provider"] = params["provider"]
        from theater.daemon.rpc.spawning import wiring_from_value

        await ParticipantLaunchService(self.daemon).spawn(
            client_id="private-rpc",
            idempotency_key=f"handoff-{gate.job_handle}",
            params=request,
            launch_prompt=prompt,
            launch_wiring=wiring_from_value(params.get("wiring")),
            launch_response_format=response_format,
            reserved_participant_id=gate.job_handle,
        )

    def _source_view(self, gate: HandoffGate, source: Job) -> SourceView:
        participant = participant_of(self.daemon, source.target_id)
        return SourceView(
            job=source,
            name=participant.name if participant is not None else gate.source_name,
            participant_id=source.target_id,
            evidence=self.store.get_job_evidence(source.handle),
            paths=tuple(self.store.job_modified_paths(source.handle, limit=PATHS_MAX)),
        )

    @staticmethod
    async def _review_base(view: SourceView, cwd: str) -> str:
        """The source's pinned clean commit, or a refusal naming exactly why it is not one."""
        evidence = view.evidence
        if evidence is None:
            raise HandoffRefused(
                ERROR_SOURCE_NOT_REVIEWABLE,
                "the source left no completion evidence, so no exact commit is pinned; "
                "name the source before it finishes, or review a pinned job",
            )
        git = evidence.git
        if not git.reviewable:
            why = (
                f"its working tree was dirty ({git.dirty_entries} uncommitted entries)"
                if git.pin_state == "dirty"
                else f"its Git state could not be pinned ({git.reason})"
            )
            raise HandoffRefused(
                ERROR_SOURCE_NOT_REVIEWABLE,
                f"the source is not reviewable: {why}. Ask it to commit, then review that job",
            )
        from theater.daemon.worktrees.repository import _git

        probe = await asyncio.to_thread(
            _git,
            ["git", "cat-file", "-e", f"{git.commit_sha}^{{commit}}"],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if probe.returncode != 0:
            raise HandoffRefused(
                ERROR_SOURCE_NOT_REVIEWABLE,
                f"commit {git.commit_sha} is not in the repository at {cwd}; "
                "review from the source's repository",
            )
        assert git.commit_sha is not None
        return git.commit_sha

    # ---- ending a gate ----------------------------------------------------------

    def _fail(self, gate: HandoffGate, code: str, message: str) -> None:
        """The task will never launch: end its job crashed and retire the placeholder."""
        self._close(gate, GATE_FAILED, code, message)

    def _close(
        self,
        gate: HandoffGate,
        state: str,
        code: str,
        message: str,
        *,
        expected: tuple[str, ...] = (GATE_WAITING, GATE_OPENING),
    ) -> None:
        with self.store.write_unit() as unit:
            if not self.store.move_handoff_gate(
                gate.job_handle,
                expected=expected,
                state=state,
                updated_at=now(),
                error_code=code,
                error=message[:1000],
                connection=unit.connection,
            ):
                return
            self._end_in_unit(unit, gate.job_handle, state, code)
        self._signal(gate, "cancelled" if state == GATE_CANCELLED else "error")
        logger.info("handoff gate %s %s: %s", gate.job_handle, state, message)

    def _end_in_unit(self, unit, handle: str, state: str, code: str) -> None:
        """Finish the gated job with its gate, so a terminal gate never leaves a RUNNING job."""
        job = self.store.get_job(handle, connection=unit.connection)
        if job is not None and job.state == JobState.RUNNING:
            self.daemon.jobs.finish(
                handle,
                state=JobState.KILLED if state == GATE_CANCELLED else JobState.CRASHED,
                error_code=code,
                connection=unit.connection,
            )
        if job is None or job.kind == KIND_SPAWN:
            # The placeholder child (a send has none); registry state is not unit-aware.
            unit.after_commit(lambda: self.daemon.registry.mark_dead(handle))

    def _signal(self, gate: HandoffGate, result: str) -> None:
        waited = max(0.0, now() - gate.created_at) * 1000.0
        kind = json.loads(gate.request).get("kind", KIND_SPAWN)
        timing.emit(
            HANDOFF_GATE,
            waited,
            result=result,
            task_kind=gate.task_kind,
            kind=kind,
            id=gate.job_handle,
        )

    # ---- restart --------------------------------------------------------------------

    async def recover(self) -> None:
        """Re-arm every durable gate after a restart; nothing here replays a launch."""
        for gate in self.store.handoff_gates((GATE_OPENING,)):
            self.store.move_handoff_gate(
                gate.job_handle,
                expected=(GATE_OPENING,),
                state=GATE_OPENED if self._launch_reserved(gate.job_handle) else GATE_WAITING,
                updated_at=now(),
            )
        self._reconcile_ended()
        for gate in self.store.handoff_gates((GATE_WAITING,)):
            job = self.store.get_job(gate.job_handle)
            if job is None or job.state != JobState.RUNNING:
                self._close(gate, GATE_CANCELLED, ERROR_CANCELLED, "ended before the restart")
                continue
            self.daemon.jobs.track(gate.job_handle)
            if job.kind == KIND_SPAWN:
                self._schedule(gate.job_handle)  # a send's queue row is recovered by the controls

    def _reconcile_ended(self) -> None:
        """Finish a RUNNING job or retire a live placeholder behind an already-ended gate."""
        for gate in self.store.handoff_gates((GATE_FAILED, GATE_CANCELLED)):
            job = self.store.get_job(gate.job_handle)
            if job is None:
                continue
            placeholder = self.store.get_participant(gate.job_handle)
            live = job.kind == KIND_SPAWN and placeholder is not None
            if job.state == JobState.RUNNING or (live and placeholder.status is not Status.DEAD):
                with self.store.write_unit() as unit:
                    self._end_in_unit(
                        unit, gate.job_handle, gate.state, gate.error_code or ERROR_CANCELLED
                    )

    # ---- facts other layers read ------------------------------------------------------

    def cancel_unlaunched(self, handle: str) -> bool:
        """End a task still waiting for its source; True when no launch had begun to end."""
        gate = self.store.get_handoff_gate(handle)
        if gate is None or gate.state != GATE_WAITING:
            return False
        self._close(
            gate,
            GATE_CANCELLED,
            ERROR_CANCELLED,
            "cancelled before its source ended",
            expected=(GATE_WAITING,),
        )
        current = self.store.get_handoff_gate(handle)
        return current is not None and current.state == GATE_CANCELLED

    def never_dispatched(self, handle: str) -> bool:
        """Durable proof that this job's work was never launched, for the await exception."""
        gate = self.store.get_handoff_gate(handle)
        return gate is not None and gate.state in (GATE_FAILED, GATE_CANCELLED)

    def dependency_targets(self, handles: list[str]) -> list[str]:
        """Participants whose running jobs must finish before an awaited message or task can go.

        Follows a gate to its source job, and a send to the earlier gated sends of its own lane
        (one caller to one target stays in order); an oversized graph is refused.
        """
        targets: list[str] = []
        for handle in handles:
            pending: list[str] = [handle]
            seen: set[str] = set()
            while pending and len(seen) < MAX_CHAIN:
                current = pending.pop()
                if current in seen:
                    continue
                seen.add(current)
                gate = self.store.get_handoff_gate(current)
                if gate is not None and gate.state in (GATE_WAITING, GATE_OPENING):
                    source = self.store.get_job(gate.source_handle)
                    if source is not None and source.state == JobState.RUNNING:
                        if source.target_id is not None and source.target_id not in targets:
                            targets.append(source.target_id)
                        pending.append(source.handle)
                job = self.store.get_job(current)
                if job is not None:
                    pending.extend(self._lane_predecessors(job))
            if pending:
                raise BadRequest(
                    f"after dependencies for {handle!r} exceed {MAX_CHAIN} jobs; await a nearer "
                    "source or shorten the chain"
                )
        return targets

    def _lane_predecessors(self, job: Job) -> list[str]:
        """Earlier queued sends from the same caller to the same target that still hold the lane."""
        if job.kind != KIND_SEND or job.target_id is None or job.state != JobState.RUNNING:
            return []
        callers = self.store.queued_control_callers(job.target_id)
        before: list[str] = []
        for operation in self.store.queued_control_operations(job.target_id)[
            :CONTROL_QUEUE_MAX_PENDING
        ]:
            if operation.job_handle == job.handle:
                return before
            if callers.get(operation.operation_id) == job.caller_id and operation.job_handle:
                before.append(operation.job_handle)
        return []

    def describe(self, handle: str) -> dict | None:
        gate = self.store.get_handoff_gate(handle)
        if gate is None:
            return None
        info = {
            "state": gate.state,
            "task_kind": gate.task_kind,
            "source": gate.source_handle,
            "waited_s": int(time.time() - gate.created_at),
        }
        if gate.source_name:
            info["source_name"] = gate.source_name
        if gate.error_code:
            info["error_code"] = gate.error_code
            info["error"] = gate.error
        return info


#: The spawn parameters a deferred launch needs; everything else is fixed by the card.
_REQUEST_KEYS = (
    "harness",
    "approval",
    "cwd",
    "worktree",
    "base_branch",
    "model",
    "reasoning_effort",
    "description",
    "provider",
    "wiring",
    "parent_id",
)
