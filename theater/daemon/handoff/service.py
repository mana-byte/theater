"""Deferred spawns: admit, wait durably for the source job, then launch through the normal path."""

from __future__ import annotations

import asyncio
import json
import logging
import time

from sqlalchemy import select

from theater import timing
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
    MAX_CHAIN,
    MAX_WAITING_PER_CALLER,
    PATHS_MAX,
    TASK_KIND_REVIEW,
    TASK_KIND_WORK,
    TASK_KINDS,
)
from theater.daemon.handoff.card import SourceView, render_card
from theater.daemon.handoff.evidence import EvidencePinner
from theater.daemon.handoff.models import HandoffGate
from theater.daemon.handoff.resolution import participant_of, resolve_source
from theater.daemon.response_contracts.prompts import prompt_with_guidance
from theater.daemon.schema import launch_reservations
from theater.models import BadRequest, JobState, TheaterError, now
from theater.observability.catalog import HANDOFF_GATE

logger = logging.getLogger("theater.handoff")


class HandoffRefused(TheaterError):
    """The gate cannot open as asked; the task never launches and its job ends crashed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class HandoffService:
    """Owns the durable gate between a deferred spawn and the source job it waits for."""

    def __init__(self, daemon) -> None:
        self.daemon = daemon
        self.store = daemon.store
        self._tasks: set[asyncio.Task] = set()
        daemon.jobs.evidence = EvidencePinner(daemon.store)
        daemon.jobs.add_terminal_listener(self._on_terminal)

    # ---- admission ----------------------------------------------------------

    def admit_spawn(self, params: dict, *, task: str, response_format: str | None) -> dict:
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

        stored = {
            "params": {k: params.get(k) for k in _REQUEST_KEYS},
            "task": task,
            "response_format": response_format,
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
        """Open every task waiting on this job; cancel a gate whose own job ended first."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        for gate in self.store.handoff_gates((GATE_WAITING,), source_handle=handle):
            self._schedule(gate.job_handle)
        own = self.store.get_handoff_gate(handle)
        if own is not None and own.state == GATE_WAITING:
            self._close(own, GATE_CANCELLED, ERROR_CANCELLED, "the task ended before it launched")

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
        except (TheaterError, ValueError) as exc:
            self._fail(gate, ERROR_LAUNCH_REFUSED, f"{type(exc).__name__}: {exc}")
        else:
            self.store.move_handoff_gate(
                handle, expected=(GATE_OPENING,), state=GATE_OPENED, updated_at=now()
            )
            self._signal(gate, "success")

    async def _launch(self, gate: HandoffGate, source) -> None:
        from theater.daemon.spawning.provider_launch import ParticipantLaunchService

        stored = json.loads(gate.request)
        params, task, response_format = stored["params"], stored["task"], stored["response_format"]
        participant = participant_of(self.daemon, source.target_id)
        view = SourceView(
            job=source,
            name=participant.name if participant is not None else gate.source_name,
            participant_id=source.target_id,
            evidence=self.store.get_job_evidence(source.handle),
            paths=tuple(self.store.job_modified_paths(source.handle, limit=PATHS_MAX)),
        )
        base = params.get("base_branch")
        if gate.task_kind == TASK_KIND_REVIEW:
            base = await self._review_base(view, str(params["cwd"]))
        prompt = f"{task}\n\n{render_card(gate.task_kind, view)}"
        if response_format is not None:
            prompt = prompt_with_guidance(prompt, response_format)
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

    def _close(self, gate: HandoffGate, state: str, code: str, message: str) -> None:
        if not self.store.move_handoff_gate(
            gate.job_handle,
            expected=(GATE_WAITING, GATE_OPENING),
            state=state,
            updated_at=now(),
            error_code=code,
            error=message[:1000],
        ):
            return
        job = self.store.get_job(gate.job_handle)
        if job is not None and job.state == JobState.RUNNING:
            self.daemon.jobs.finish(
                gate.job_handle,
                state=JobState.KILLED if state == GATE_CANCELLED else JobState.CRASHED,
                error_code=code,
            )
        self.daemon.registry.mark_dead(gate.job_handle)
        self._signal(gate, "cancelled" if state == GATE_CANCELLED else "error")
        logger.info("handoff gate %s %s: %s", gate.job_handle, state, message)

    def _signal(self, gate: HandoffGate, result: str) -> None:
        waited = max(0.0, now() - gate.created_at) * 1000.0
        timing.emit(
            HANDOFF_GATE, waited, result=result, task_kind=gate.task_kind, id=gate.job_handle
        )

    # ---- restart --------------------------------------------------------------------

    async def recover(self) -> None:
        """Re-arm every durable gate after a restart; nothing here replays a launch."""
        for gate in self.store.handoff_gates((GATE_OPENING,)):
            launched = self.store.conn.execute(
                select(launch_reservations.c.operation_id).where(
                    launch_reservations.c.participant_id == gate.job_handle
                )
            ).first()
            self.store.move_handoff_gate(
                gate.job_handle,
                expected=(GATE_OPENING,),
                state=GATE_OPENED if launched is not None else GATE_WAITING,
                updated_at=now(),
            )
        for gate in self.store.handoff_gates((GATE_WAITING,)):
            job = self.store.get_job(gate.job_handle)
            if job is None or job.state != JobState.RUNNING:
                self._close(gate, GATE_CANCELLED, ERROR_CANCELLED, "ended before the restart")
                continue
            self.daemon.jobs.track(gate.job_handle)
            self._schedule(gate.job_handle)

    # ---- facts other layers read ------------------------------------------------------

    def cancel_unlaunched(self, handle: str) -> bool:
        """End a task still waiting for its source; True when no launch had begun to end."""
        gate = self.store.get_handoff_gate(handle)
        if gate is None or gate.state != GATE_WAITING:
            return False
        self._close(gate, GATE_CANCELLED, ERROR_CANCELLED, "cancelled before its source ended")
        return self.store.get_handoff_gate(handle).state == GATE_CANCELLED

    def never_dispatched(self, handle: str) -> bool:
        """Durable proof that this job's work was never launched, for the await exception."""
        gate = self.store.get_handoff_gate(handle)
        return gate is not None and gate.state in (GATE_FAILED, GATE_CANCELLED)

    def dependency_targets(self, handles: list[str]) -> list[str]:
        """Participants whose running jobs must finish before an awaited gated task can start."""
        targets: list[str] = []
        for handle in handles:
            current = handle
            for _ in range(MAX_CHAIN):
                gate = self.store.get_handoff_gate(current)
                if gate is None or gate.state not in (GATE_WAITING, GATE_OPENING):
                    break
                source = self.store.get_job(gate.source_handle)
                if source is None or source.state != JobState.RUNNING or source.target_id is None:
                    break
                if source.target_id not in targets:
                    targets.append(source.target_id)
                current = source.handle
        return targets

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
