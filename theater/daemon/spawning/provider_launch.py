"""Durable provider-backed participant launch and adoption lifecycle."""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, replace

from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from theater.daemon.events.publication import (
    job_event,
    participant_event,
    terminal_binding_event,
    workspace_usage_event,
)
from theater.daemon.operations import (
    DispatchIntent,
    OperationAcceptance,
    OperationOutcome,
    OperationService,
    PreparedOperation,
    request_digest,
)
from theater.daemon.operations.projection import operation_event_payload
from theater.daemon.persistence.repositories._json import decode_json, encode_json
from theater.daemon.persistence.transactions import WriteUnit
from theater.daemon.schema import jobs as jobs_table
from theater.daemon.schema import launch_reservations, participants
from theater.daemon.spawning.models import (
    ProviderLaunchOutcome,
    ProviderLaunchSelection,
    Reservation,
    SpawnRequest,
)
from theater.daemon.spawning.planning import resolve_launch_command
from theater.daemon.terminals import ProviderUnavailable, TerminalIdentityMismatch
from theater.daemon.worktrees.service import (
    WorkspacePreparation,
    WorkspaceRequest,
    WorkspaceReservation,
)
from theater.frontend.capabilities import TERMINAL_PROVIDER_CAPABILITY, MethodClass
from theater.harness import get as get_harness
from theater.harness.base import ResumeLaunchOverlay
from theater.harness.contracts.runtime import RuntimeWiring
from theater.models import (
    BadRequest,
    Job,
    JobState,
    JournalEventRecord,
    LaunchReservationRecord,
    Participant,
    ParticipantOrigin,
    PublicOperationRecord,
    Status,
    TerminalBindingRecord,
    Tier,
    WorkspaceState,
    WorkspaceUsageHolderKind,
    WorkspaceUsageRecord,
    new_id,
    now,
)
from theater.provenance import is_trusted_provenance


@dataclass(frozen=True, slots=True)
class _SpawnAdmission:
    provider_id: str
    provider_generation: int
    provider_selector: str
    parent_id: str | None
    workspace_request: WorkspaceRequest
    request: SpawnRequest
    resume_predecessor: Participant | None
    resume_overlay: ResumeLaunchOverlay | None
    cwd: str


class ParticipantLaunchService:
    """RC10 public spawn/adoption orchestration over existing daemon services."""

    DEFAULT_PROVIDER_SELECTOR = "tmux"

    def __init__(self, daemon) -> None:
        self.daemon = daemon
        self.store = daemon.store
        self.registry = daemon.registry
        self.spawner = daemon.spawner
        self.operations: OperationService = daemon.operation_service
        self.terminals = daemon.terminal_service
        self.workspaces = daemon.workspace_service
        terminal_config = getattr(getattr(daemon, "config", None), "terminals", None)
        configured_default = getattr(terminal_config, "default_provider", None)
        self.default_provider_selector = (
            configured_default
            if isinstance(configured_default, str) and configured_default
            else self.DEFAULT_PROVIDER_SELECTOR
        )

    async def spawn(
        self,
        *,
        client_id: str,
        idempotency_key: str,
        params: Mapping[str, object],
        launch_prompt: str | None = None,
        launch_wiring: RuntimeWiring | None = None,
        launch_response_format: str | None = None,
    ) -> Mapping[str, object]:
        """Accept a spawn and detach all filesystem/provider work from the request."""
        replay = self._replay_spawn_if_known(client_id, idempotency_key, params)
        if replay is not None:
            return replay.response
        workspace_preparation = await self._prepare_workspace_for_spawn(params)
        captured: dict[str, object] = {}
        launch_params = dict(params)
        if launch_prompt is not None:
            launch_params["prompt"] = launch_prompt
        if launch_wiring is not None:
            launch_params["wiring"] = launch_wiring.value
        if launch_response_format is not None:
            launch_params["response_format"] = launch_response_format

        def prepare(operation_id: str, unit: WriteUnit) -> PreparedOperation:
            return self._prepare_spawn_operation(
                operation_id,
                unit,
                client_id=client_id,
                params=launch_params,
                captured=captured,
                workspace_preparation=workspace_preparation,
            )

        acceptance = self.operations.accept_operation(
            client_id=client_id,
            idempotency_key=idempotency_key,
            method="frontend.participants.spawn",
            params=params,
            prepare=prepare,
        )
        if acceptance.replayed:
            return acceptance.response
        participant = captured["participant"]
        assert isinstance(participant, Participant)
        self._start_spawn(acceptance, captured, launch_params, participant)
        return acceptance.response

    def _replay_spawn_if_known(
        self, client_id: str, idempotency_key: str, params: Mapping[str, object]
    ) -> OperationAcceptance | None:
        """Return a valid replay before resolving mutable Git defaults."""
        self.operations._validate_idempotent_request(
            "frontend.participants.spawn", params, idempotency_key, MethodClass.OPERATION
        )
        digest = request_digest("frontend.participants.spawn", params)
        record = self.operations._active_idempotency(
            client_id, idempotency_key, self.operations._clock()
        )
        if record is None:
            return None
        return self.operations._operation_replay(record, "frontend.participants.spawn", digest)

    async def _prepare_workspace_for_spawn(
        self, params: Mapping[str, object]
    ) -> WorkspacePreparation:
        """Resolve immutable workspace facts before operation admission owns SQLite."""
        workspace_request = self._workspace_request(params)
        self._validate_workspace_request(workspace_request)
        cwd = self._workspace_cwd_for_preparation(workspace_request)
        parent_id = self._optional_text(params.get("initiating_participant_id"))
        request = self._spawn_request(
            params,
            parent_id=parent_id,
            cwd=cwd,
            worktree=workspace_request.worktree,
            base_ref=workspace_request.base_ref,
        )
        harness = get_harness(request.harness)
        request = self.spawner._resolve_resume_reference(request)
        _predecessor, overlay = self.spawner._validate_before_create(request, harness)
        if overlay is not None and overlay.cwd is not None:
            if workspace_request.workspace_id is not None and overlay.cwd != cwd:
                raise BadRequest("resume workspace does not match the predecessor's trusted cwd")
            if workspace_request.workspace_id is None:
                workspace_request = replace(workspace_request, cwd=overlay.cwd)
        return await self.workspaces.prepare_for_spawn(workspace_request)

    def _prepare_spawn_operation(
        self,
        operation_id: str,
        unit: WriteUnit,
        *,
        client_id: str,
        params: Mapping[str, object],
        captured: dict[str, object],
        workspace_preparation: WorkspacePreparation,
    ) -> PreparedOperation:
        admission = self._validate_spawn_admission(params, unit)
        if admission.workspace_request != workspace_preparation.request:
            raise BadRequest("workspace facts changed before spawn acceptance; retry the request")
        workspace = self.workspaces.reserve_for_spawn(
            workspace_preparation,
            reservation_id=operation_id,
            owner_id="local_operator",
            connection=unit.connection,
        )
        cwd = workspace.workspace.path
        request = replace(admission.request, cwd=cwd, worktree=False)
        description = self._optional_text(params.get("description"))
        if description is None and admission.resume_predecessor is not None:
            description = admission.resume_predecessor.description
        participant_id = new_id()
        try:
            participant = self.registry.create_spawned(
                pid=participant_id,
                harness=request.harness,
                cwd=cwd,
                parent_id=admission.parent_id,
                has_prompt=bool(request.prompt),
                resumed_from_id=(
                    admission.resume_predecessor.id
                    if admission.resume_predecessor is not None
                    else None
                ),
                name=self._optional_text(params.get("name")),
                description=description,
                workspace_id=workspace.workspace.workspace_id,
                connection=unit.connection,
            )
            participant.branch = workspace.workspace.branch
            self.registry.persist_in_connection(participant, unit.connection)
        except IntegrityError:
            if admission.resume_predecessor is None:
                raise
            raise BadRequest(
                f"cannot resume participant {admission.resume_predecessor.id!r}: a live successor "
                "already claims this recovery"
            ) from None
        timestamp = now()
        job = self._reserve_spawn_job(
            unit,
            participant_id=participant_id,
            parent_id=admission.parent_id,
            client_id=client_id,
            prompt=request.prompt,
            response_format=request.response_format,
            timestamp=timestamp,
        )
        launch = LaunchReservationRecord(
            operation_id=operation_id,
            participant_id=participant_id,
            provider_id=admission.provider_id,
            workspace_usage_id=workspace.usage.usage_id,
            adapter=request.harness,
            phase="reserved",
            launch_facts={
                "provider_generation": admission.provider_generation,
                "provider_selector": admission.provider_selector,
                "workspace_request": self._workspace_request_facts(admission.workspace_request),
                "cwd": cwd,
                "approval": request.approval,
                "resume": request.resume,
            },
            artifact_refs=(),
            created_at=timestamp,
            updated_at=timestamp,
        )
        self.store.operations.reserve_launch(launch, connection=unit.connection)
        captured.update(
            participant=participant,
            provider_id=admission.provider_id,
            provider_generation=admission.provider_generation,
            workspace_reservation=workspace,
            request=request,
            resume_predecessor=admission.resume_predecessor,
            resume_overlay=admission.resume_overlay,
        )
        self._stage_spawn_caches(unit, participant, job, cwd, has_prompt=bool(request.prompt))
        events = self._spawn_acceptance_events(unit, participant, job, workspace, timestamp)
        return PreparedOperation(
            record=PublicOperationRecord(
                operation_id=operation_id,
                kind="spawn",
                actor_client_id=client_id,
                actor_participant_id=admission.parent_id,
                target_ids=(participant_id,),
                state="accepted",
                phase="launch_reserved",
                job_handle=participant_id,
                created_at=timestamp,
                updated_at=timestamp,
            ),
            response={
                "operation_id": operation_id,
                "state": "accepted",
                "participant_id": participant_id,
                "job_handle": participant_id,
            },
            events=events,
        )

    def _validate_spawn_admission(
        self, params: Mapping[str, object], unit: WriteUnit
    ) -> _SpawnAdmission:
        harness = get_harness(str(params["harness"]))
        parent_id = self._optional_text(params.get("initiating_participant_id"))
        if (
            parent_id is not None
            and self._participant_in_connection(parent_id, unit.connection) is None
        ):
            raise BadRequest(f"no initiating participant {parent_id!r} exists")
        workspace_request = self._workspace_request(params)
        self._validate_workspace_request(workspace_request)
        cwd = self._workspace_cwd(workspace_request, unit)
        request = self._spawn_request(
            params,
            parent_id=parent_id,
            cwd=cwd,
            worktree=workspace_request.worktree,
            base_ref=workspace_request.base_ref,
        )
        if shutil.which(harness.binary) is None:
            raise BadRequest(f"{harness.binary!r} is not on PATH")
        request = self.spawner._resolve_resume_reference(request)
        predecessor, overlay = self.spawner._validate_before_create(request, harness)
        if overlay is not None and overlay.cwd is not None:
            if workspace_request.workspace_id is not None and overlay.cwd != cwd:
                raise BadRequest("resume workspace does not match the predecessor's trusted cwd")
            cwd = overlay.cwd
            request = replace(request, cwd=cwd)
            if workspace_request.workspace_id is None:
                workspace_request = replace(workspace_request, cwd=cwd)
        provider_id, generation = self._select_provider(params.get("provider"), unit)
        provider = self.store.providers.get(provider_id, connection=unit.connection)
        assert provider is not None
        return _SpawnAdmission(
            provider_id=provider_id,
            provider_generation=generation,
            provider_selector=provider.selector,
            parent_id=parent_id,
            workspace_request=workspace_request,
            request=request,
            resume_predecessor=predecessor,
            resume_overlay=overlay,
            cwd=cwd,
        )

    def _workspace_cwd(self, request: WorkspaceRequest, unit: WriteUnit) -> str:
        if request.workspace_id is None:
            assert request.cwd is not None
            return request.cwd
        workspace = self.store.workspaces.get(request.workspace_id, connection=unit.connection)
        if workspace is None:
            raise BadRequest(f"no workspace {request.workspace_id!r} exists")
        if workspace.state != WorkspaceState.ACTIVE.value:
            raise BadRequest(f"workspace {workspace.workspace_id!r} is not active")
        return workspace.path

    def _workspace_cwd_for_preparation(self, request: WorkspaceRequest) -> str:
        if request.workspace_id is None:
            assert request.cwd is not None
            return request.cwd
        workspace = self.store.workspaces.get(request.workspace_id)
        if workspace is None:
            raise BadRequest(f"no workspace {request.workspace_id!r} exists")
        if workspace.state != WorkspaceState.ACTIVE.value:
            raise BadRequest(f"workspace {workspace.workspace_id!r} is not active")
        return workspace.path

    def _reserve_spawn_job(
        self,
        unit: WriteUnit,
        *,
        participant_id: str,
        parent_id: str | None,
        client_id: str,
        prompt: str,
        response_format: str | None,
        timestamp: float,
    ) -> Job:
        job = Job(
            handle=participant_id,
            caller_id=parent_id or ("cli" if client_id == "private-rpc" else None),
            target_id=participant_id,
            kind="spawn",
            prompt=prompt,
            state=JobState.RUNNING.value,
            result=None,
            error_code=None,
            created_at=timestamp,
            finished_at=None,
            response_format=response_format,
            actor_client_id=client_id,
            actor_participant_id=parent_id,
        )
        unit.connection.execute(insert(jobs_table).values(**job.to_dict()))
        return job

    def _stage_spawn_caches(
        self, unit: WriteUnit, participant: Participant, job: Job, cwd: str, *, has_prompt: bool
    ) -> None:
        unit.after_commit(
            lambda: self.daemon.jobs.attach_touch_accumulator(participant.id, cwd=cwd)
        )
        if participant.name is not None:
            unit.after_commit(
                lambda: self.registry.remember_reserved_name(participant.id, participant.name or "")
            )
        unit.after_commit(
            lambda: self.store.bus_append(
                "participant.created",
                to_id=participant.id,
                from_id=participant.parent_id,
                payload={
                    "tier": str(participant.tier),
                    "harness": participant.harness,
                    "cwd": cwd,
                    "has_prompt": has_prompt,
                },
            )
        )
        unit.after_commit(
            lambda: self.store.bus_append(
                "job.created",
                from_id=job.caller_id,
                to_id=participant.id,
                payload={"handle": participant.id, "kind": "spawn"},
            )
        )

    def _spawn_acceptance_events(
        self,
        unit: WriteUnit,
        participant: Participant,
        job: Job,
        workspace: WorkspaceReservation,
        timestamp: float,
    ) -> tuple[JournalEventRecord, ...]:
        first = self.store.journal.current_sequence(connection=unit.connection) + 1
        events = [
            self._participant_event(
                participant, timestamp, revision=first, connection=unit.connection
            ),
            self._job_event(job, timestamp, revision=first + 1),
        ]
        if workspace.created:
            events.append(
                JournalEventRecord(
                    kind="workspace.updated",
                    entity_id=workspace.workspace.workspace_id,
                    entity_revision=first + len(events),
                    payload=self.workspaces.project(
                        workspace.workspace, connection=unit.connection
                    ),
                    recorded_at=timestamp,
                )
            )
        events.append(
            self._workspace_usage_event(
                workspace.usage,
                timestamp,
                revision=first + len(events),
                connection=unit.connection,
            )
        )
        return tuple(events)

    def _start_spawn(
        self,
        acceptance: OperationAcceptance,
        captured: dict[str, object],
        params: Mapping[str, object],
        participant: Participant,
    ) -> None:
        provider_generation = captured["provider_generation"]
        assert type(provider_generation) is int

        async def side_effect() -> OperationOutcome:
            try:
                workspace = captured["workspace_reservation"]
                assert isinstance(workspace, WorkspaceReservation)
                workspace = await self.workspaces.materialize_creation(
                    workspace, reservation_id=acceptance.record.operation_id
                )
                captured["workspace_reservation"] = workspace
                self._mark_workspace_ready(acceptance.record.operation_id, workspace)
                self.daemon.jobs.replace_touch_accumulator(
                    participant.id, cwd=workspace.workspace.path
                )
                request = captured["request"]
                assert isinstance(request, SpawnRequest)
                request = replace(request, cwd=workspace.workspace.path, worktree=False)
                resume_predecessor = captured["resume_predecessor"]
                assert resume_predecessor is None or isinstance(resume_predecessor, Participant)
                resume_overlay = captured["resume_overlay"]
                assert resume_overlay is None or isinstance(resume_overlay, ResumeLaunchOverlay)
                provider = ProviderLaunchSelection(
                    provider_id=str(captured["provider_id"]),
                    provider_generation=provider_generation,
                    operation_id=acceptance.record.operation_id,
                    launch_id=acceptance.record.operation_id,
                    terminal_service=self.terminals,
                    mark_dispatched=lambda: self._mark_launch_dispatched(
                        acceptance.record.operation_id,
                        provider_generation,
                    ),
                    bind_terminal=lambda terminal: self._bind_spawn(
                        acceptance.record.operation_id,
                        participant.id,
                        workspace.usage.usage_id,
                        terminal,
                    ),
                )
                reservation = await self.spawner.prepare_provider_launch(
                    request,
                    participant,
                    child_cwd=workspace.workspace.path,
                    provider=provider,
                    workspace_usage_id=workspace.usage.usage_id,
                    resume_predecessor=resume_predecessor,
                    resume_overlay=resume_overlay,
                    prevalidated=True,
                )
                self._record_launch_plan(acceptance.record.operation_id, reservation)
                self._persist_provider_dispatch_target(
                    acceptance.record.operation_id,
                    str(captured["provider_id"]),
                    provider_generation,
                )
                attached = await self.spawner.launch(reservation)
                return OperationOutcome.succeeded(
                    phase="terminal_bound",
                    result={"participant_id": attached.id, "job_handle": attached.id},
                )
            except ProviderLaunchOutcome as exc:
                if exc.outcome.state == "failed":
                    await self._rollback_spawn_reservation(
                        acceptance.record.operation_id,
                        participant.id,
                        captured.get("workspace_reservation"),
                        error_code=self._outcome_error_code(exc.outcome),
                        definitive_refusal=True,
                    )
                return exc.outcome
            except (BadRequest, ProviderUnavailable) as exc:
                if not self._launch_was_dispatched(acceptance.record.operation_id):
                    await self._rollback_spawn_reservation(
                        acceptance.record.operation_id,
                        participant.id,
                        captured.get("workspace_reservation"),
                        error_code=exc.code,
                    )
                    return OperationOutcome.failed(
                        phase="launch_refused",
                        error={"code": exc.code, "message": str(exc)},
                    )
                return OperationOutcome.uncertain(
                    phase="terminal_create_outcome_unknown",
                    error={
                        "code": exc.code,
                        "message": "terminal creation may have executed; reconcile before retrying",
                    },
                )
            except TerminalIdentityMismatch as exc:
                return OperationOutcome.uncertain(
                    phase="provider_identity_uncertain",
                    error={"code": exc.code, "message": str(exc)},
                )
            except Exception as exc:
                if not self._launch_was_dispatched(acceptance.record.operation_id):
                    await self._rollback_spawn_reservation(
                        acceptance.record.operation_id,
                        participant.id,
                        captured.get("workspace_reservation"),
                        error_code="internal",
                    )
                    return OperationOutcome.failed(
                        phase="launch_preparation_failed",
                        error={"code": "internal", "message": str(exc)},
                    )
                return OperationOutcome.uncertain(
                    phase="terminal_create_outcome_unknown",
                    error={
                        "code": "internal",
                        "message": "terminal creation may have executed; reconcile before retrying",
                        "details": {"reason": type(exc).__name__},
                    },
                )
            except asyncio.CancelledError:
                raise

        self.operations.start(
            acceptance.record.operation_id,
            dispatch=DispatchIntent(phase="launch_preparing"),
            side_effect=side_effect,
        )

    def adopt(
        self,
        *,
        client_id: str,
        idempotency_key: str,
        params: Mapping[str, object],
    ) -> Mapping[str, object]:
        """Accept explicit adoption; the detached task always performs a fresh inspect."""
        captured: dict[str, object] = {}

        def prepare(operation_id: str, unit: WriteUnit) -> PreparedOperation:
            provider_id, generation = self._select_provider(
                params["provider_id"], unit, allow_selector=False
            )
            actor_participant_id = self._optional_text(params.get("initiating_participant_id"))
            if (
                actor_participant_id is not None
                and self._participant_in_connection(actor_participant_id, unit.connection) is None
            ):
                raise BadRequest(f"no initiating participant {actor_participant_id!r} exists")
            requested = self._optional_text(params.get("participant_id"))
            created_participant = requested is None
            if requested is None:
                participant = self.registry.create_spawned(
                    pid=new_id(),
                    harness="unknown",
                    cwd="",
                    parent_id=None,
                    has_prompt=False,
                    tier=Tier.ADOPTED,
                    origin=ParticipantOrigin.ADOPTED,
                    connection=unit.connection,
                )
                if participant.name is not None:
                    unit.after_commit(
                        lambda: self.registry.remember_reserved_name(
                            participant.id, participant.name or ""
                        )
                    )
            else:
                participant = self._participant_in_connection(requested, unit.connection)
                if participant is None:
                    raise BadRequest(f"no participant {requested!r} exists")
                if participant.origin not in {ParticipantOrigin.EXTERNAL, None}:
                    raise BadRequest("adoption can attach only an existing external participant")
                if (
                    self.store.terminal_bindings.get(participant.id, connection=unit.connection)
                    is not None
                ):
                    raise TerminalIdentityMismatch(
                        provider_id, str(params["terminal_id"]), "participant_already_bound"
                    )
            timestamp = now()
            first_revision = self.store.journal.current_sequence(connection=unit.connection) + 1
            captured.update(
                participant=participant,
                provider_id=provider_id,
                provider_generation=generation,
                created_participant=created_participant,
            )
            return PreparedOperation(
                record=PublicOperationRecord(
                    operation_id=operation_id,
                    kind="adopt",
                    actor_client_id=client_id,
                    actor_participant_id=actor_participant_id,
                    target_ids=(participant.id,),
                    state="accepted",
                    phase="adoption_reserved",
                    created_at=timestamp,
                    updated_at=timestamp,
                ),
                response={
                    "operation_id": operation_id,
                    "state": "accepted",
                    "participant_id": participant.id,
                    "job_handle": None,
                },
                events=(
                    self._participant_event(
                        participant,
                        timestamp,
                        revision=first_revision,
                        connection=unit.connection,
                    ),
                ),
            )

        acceptance = self.operations.accept_operation(
            client_id=client_id,
            idempotency_key=idempotency_key,
            method="frontend.participants.adopt",
            params=params,
            prepare=prepare,
        )
        if acceptance.replayed:
            return acceptance.response
        participant = captured["participant"]
        assert isinstance(participant, Participant)
        provider_generation = captured["provider_generation"]
        assert type(provider_generation) is int

        async def side_effect() -> OperationOutcome:
            try:
                inspected = await self.terminals.inspect(
                    str(captured["provider_id"]),
                    provider_generation,
                    str(params["terminal_id"]),
                    str(params["terminal_incarnation"]),
                )
                terminal = self._inspected_terminal(
                    inspected,
                    provider_id=str(captured["provider_id"]),
                    terminal_id=str(params["terminal_id"]),
                )
                self._validate_adoption(
                    participant,
                    terminal,
                    inspected,
                    provider_id=str(captured["provider_id"]),
                    provider_generation=provider_generation,
                    terminal_id=str(params["terminal_id"]),
                    terminal_incarnation=str(params["terminal_incarnation"]),
                )
                attached = self._bind_adoption(
                    acceptance.record.operation_id, participant, terminal
                )
                return OperationOutcome.succeeded(
                    phase="terminal_adopted", result={"participant_id": attached.id}
                )
            except (BadRequest, ProviderUnavailable, TerminalIdentityMismatch) as exc:
                if captured["created_participant"]:
                    self._rollback_adoption_reservation(participant.id)
                return OperationOutcome.failed(
                    phase="adoption_refused",
                    error={"code": exc.code, "message": str(exc)},
                )
            except Exception as exc:
                if captured["created_participant"]:
                    self._rollback_adoption_reservation(participant.id)
                return OperationOutcome.failed(
                    phase="adoption_refused",
                    error={"code": "internal", "message": str(exc)},
                )

        self.operations.start(
            acceptance.record.operation_id,
            dispatch=DispatchIntent(phase="terminal_inspection_started"),
            side_effect=side_effect,
        )
        return acceptance.response

    def _select_provider(
        self, requested: object, unit: WriteUnit, *, allow_selector: bool = True
    ) -> tuple[str, int]:
        token = (
            requested
            if isinstance(requested, str) and requested
            else self.default_provider_selector
        )
        record = self.store.providers.get(token, connection=unit.connection)
        if record is None and allow_selector:
            record = self.store.providers.get_by_selector(token, connection=unit.connection)
        if record is None:
            raise ProviderUnavailable(token, "not_registered")
        if TERMINAL_PROVIDER_CAPABILITY not in record.capabilities:
            raise ProviderUnavailable(record.provider_id, "missing_terminal_create_capability")
        if self.terminals.connections.health(record.provider_id) != "online":
            raise ProviderUnavailable(record.provider_id, "not_launchable")
        if not self.terminals.connections.is_current(record.provider_id, record.generation):
            raise ProviderUnavailable(record.provider_id, "no_current_callback_generation")
        return record.provider_id, record.generation

    def _record_launch_plan(self, operation_id: str, reservation: Reservation) -> None:
        plan = reservation.plan
        provider = reservation.provider
        assert provider is not None
        artifacts = tuple(str(path) for path in (*plan.files.keys(), *plan.private_files.keys()))
        with self.store.write_unit() as unit:
            encoded_facts = unit.connection.execute(
                select(launch_reservations.c.launch_facts).where(
                    launch_reservations.c.operation_id == operation_id
                )
            ).scalar_one()
            stored_facts = decode_json(str(encoded_facts))
            if not isinstance(stored_facts, Mapping):
                raise TypeError("stored launch facts are not an object")
            facts = {
                **stored_facts,
                "provider_generation": provider.provider_generation,
                "cwd": reservation.child_cwd,
                "argv": list(resolve_launch_command(plan)),
                "environment_keys": sorted({*plan.env, "THEATER_ID"}),
                "native": reservation.native is not None,
                "workspace_id": reservation.participant.workspace_id,
            }
            unit.connection.execute(
                update(launch_reservations)
                .where(launch_reservations.c.operation_id == operation_id)
                .values(
                    phase="plan_ready",
                    launch_facts=encode_json(facts),
                    artifact_refs=encode_json(list(artifacts)),
                    updated_at=now(),
                )
            )

    def _mark_workspace_ready(self, operation_id: str, reservation: WorkspaceReservation) -> None:
        with self.store.write_unit() as unit:
            changed = unit.connection.execute(
                update(launch_reservations)
                .where(
                    launch_reservations.c.operation_id == operation_id,
                    launch_reservations.c.dispatch_marker.is_(None),
                )
                .values(
                    workspace_usage_id=reservation.usage.usage_id,
                    phase="workspace_ready",
                    updated_at=now(),
                )
            )
            if changed.rowcount != 1:
                raise RuntimeError("workspace launch reservation changed before preparation")

    def _persist_provider_dispatch_target(
        self, operation_id: str, provider_id: str, generation: int
    ) -> None:
        """Fence terminal creation to its provider before the callback write."""
        self.operations.mark_provider_dispatch_target(
            operation_id,
            provider_id=provider_id,
            provider_generation=generation,
            phase="terminal_create_pending",
        )

    def _clear_provider_dispatch_target(
        self, operation_id: str, unit: WriteUnit, *, timestamp: float
    ) -> PublicOperationRecord:
        operation = self.store.operations.get(operation_id, connection=unit.connection)
        if operation is None:
            raise RuntimeError("public operation disappeared during launch rollback")
        updated = replace(
            operation,
            dispatch_provider_id=None,
            dispatch_provider_generation=None,
            updated_at=timestamp,
        )
        if not self.store.operations.replace(
            updated,
            expected_state=operation.state,
            expected_updated_at=operation.updated_at,
            connection=unit.connection,
        ):
            raise RuntimeError("public operation changed during launch rollback")
        return updated

    def _launch_was_dispatched(self, operation_id: str) -> bool:
        marker = self.store.conn.execute(
            select(launch_reservations.c.dispatch_marker).where(
                launch_reservations.c.operation_id == operation_id
            )
        ).scalar_one_or_none()
        return marker is not None

    async def _rollback_spawn_reservation(
        self,
        operation_id: str,
        participant_id: str,
        workspace_value: object,
        *,
        error_code: str,
        definitive_refusal: bool = False,
    ) -> None:
        timestamp = now()
        created_workspace_id: str | None = None
        with self.store.write_unit() as unit:
            launch = unit.connection.execute(
                select(launch_reservations).where(
                    launch_reservations.c.operation_id == operation_id,
                    launch_reservations.c.participant_id == participant_id,
                )
            ).first()
            if launch is None:
                raise RuntimeError("launch reservation disappeared during rollback")
            if launch._mapping["dispatch_marker"] is not None and not definitive_refusal:
                return
            operation = self._clear_provider_dispatch_target(
                operation_id, unit, timestamp=timestamp
            )
            events: list[JournalEventRecord] = [
                JournalEventRecord(
                    kind="operation.updated",
                    entity_id=operation.operation_id,
                    entity_revision=0,
                    payload=operation_event_payload(operation),
                    recorded_at=timestamp,
                )
            ]
            participant = self._participant_in_connection(participant_id, unit.connection)
            if participant is not None and participant.status is not Status.DEAD:
                participant.status = Status.DEAD
                participant.termination_reason = "spawn_failed"
                participant.terminated_at = timestamp
                participant.last_activity = timestamp
                self.registry.persist_in_connection(participant, unit.connection)
                events.append(
                    self._participant_event(
                        participant,
                        timestamp,
                        revision=0,
                        connection=unit.connection,
                    )
                )
            job_row = unit.connection.execute(
                select(jobs_table).where(jobs_table.c.handle == participant_id)
            ).first()
            if job_row is not None:
                job = Job.from_row(job_row._mapping)
                if job.state == JobState.RUNNING.value:
                    job = replace(
                        job,
                        state=JobState.CRASHED.value,
                        error_code=error_code,
                        finished_at=timestamp,
                    )
                    unit.connection.execute(
                        update(jobs_table)
                        .where(
                            jobs_table.c.handle == participant_id,
                            jobs_table.c.state == JobState.RUNNING.value,
                        )
                        .values(
                            state=JobState.CRASHED.value,
                            error_code=error_code,
                            finished_at=timestamp,
                        )
                    )
                    events.append(self._job_event(job, timestamp, revision=0))
            usage_id = launch._mapping["workspace_usage_id"]
            if usage_id is None and isinstance(workspace_value, WorkspaceReservation):
                usage_id = workspace_value.usage.usage_id
            if isinstance(usage_id, str):
                usage = self.store.workspaces.get_usage(usage_id, connection=unit.connection)
                if (
                    usage is not None
                    and usage.released_at is None
                    and usage.holder_kind == WorkspaceUsageHolderKind.RESERVATION.value
                    and usage.holder_id == operation_id
                    and self.store.workspaces.release_usage(
                        usage_id,
                        released_at=timestamp,
                        reason="launch_rolled_back",
                        connection=unit.connection,
                    )
                ):
                    released = replace(
                        usage,
                        released_at=timestamp,
                        release_reason="launch_rolled_back",
                    )
                    events.append(
                        self._workspace_usage_event(
                            released,
                            timestamp,
                            revision=0,
                            connection=unit.connection,
                        )
                    )
                    workspace = self.store.workspaces.get(
                        usage.workspace_id, connection=unit.connection
                    )
                    if (
                        workspace is not None
                        and workspace.creation_operation_id == operation_id
                        and workspace.state == WorkspaceState.ACTIVE.value
                    ):
                        created_workspace_id = workspace.workspace_id
            unit.connection.execute(
                update(launch_reservations)
                .where(launch_reservations.c.operation_id == operation_id)
                .values(phase="rolled_back", updated_at=timestamp)
            )
            first = self.store.journal.current_sequence(connection=unit.connection) + 1
            events = [
                replace(event, entity_revision=first + index) for index, event in enumerate(events)
            ]
            if events:
                self.store.journal.append_group(unit, events)
            unit.after_commit(
                lambda: self.daemon.jobs.finish(
                    participant_id,
                    state=JobState.CRASHED,
                    error_code=error_code,
                )
            )
            unit.after_commit(lambda: self.registry.mark_dead(participant_id))
        if created_workspace_id is not None:
            await self.workspaces.rollback_created_reservation(
                workspace_id=created_workspace_id,
                reservation_id=operation_id,
            )

    def _rollback_adoption_reservation(self, participant_id: str) -> None:
        timestamp = now()
        with self.store.write_unit() as unit:
            participant = self._participant_in_connection(participant_id, unit.connection)
            if participant is None or participant.origin is not ParticipantOrigin.ADOPTED:
                return
            if self.store.terminal_bindings.get(participant_id, connection=unit.connection):
                return
            participant.status = Status.DEAD
            participant.termination_reason = "adoption_failed"
            participant.terminated_at = timestamp
            participant.last_activity = timestamp
            self.registry.persist_in_connection(participant, unit.connection)
            revision = self.store.journal.current_sequence(connection=unit.connection) + 1
            self.store.journal.append_group(
                unit,
                [
                    self._participant_event(
                        participant,
                        timestamp,
                        revision=revision,
                        connection=unit.connection,
                    )
                ],
            )
            unit.after_commit(lambda: self.registry.mark_dead(participant_id))

    @staticmethod
    def _outcome_error_code(outcome: OperationOutcome) -> str:
        if outcome.error is not None and isinstance(outcome.error.get("code"), str):
            return str(outcome.error["code"])
        return "provider_unavailable"

    def _mark_launch_dispatched(self, operation_id: str, generation: int) -> None:
        with self.store.write_unit() as unit:
            changed = unit.connection.execute(
                update(launch_reservations)
                .where(
                    launch_reservations.c.operation_id == operation_id,
                    launch_reservations.c.dispatch_marker.is_(None),
                )
                .values(
                    phase="terminal_create_dispatched",
                    dispatch_marker=f"generation:{generation}",
                    updated_at=now(),
                )
            )
            if changed.rowcount != 1:
                raise RuntimeError("launch reservation was already dispatched")

    def _bind_spawn(
        self,
        operation_id: str,
        participant_id: str,
        reservation_usage_id: str,
        terminal: Mapping[str, object],
    ) -> Participant:
        with self.store.write_unit() as unit:
            timestamp = now()
            participant = self._participant_in_connection(participant_id, unit.connection)
            if participant is None:
                raise RuntimeError("reserved participant disappeared before terminal binding")
            binding = self._binding(participant_id, terminal)
            self._ensure_terminal_unbound(binding, unit.connection)
            self.store.terminal_bindings.bind(binding, connection=unit.connection)
            usage = self.store.workspaces.get_usage(
                reservation_usage_id, connection=unit.connection
            )
            if usage is None:
                raise RuntimeError("workspace reservation disappeared before terminal binding")
            participant_usage = WorkspaceUsageRecord(
                usage_id=new_id(),
                workspace_id=usage.workspace_id,
                holder_kind=WorkspaceUsageHolderKind.PARTICIPANT.value,
                holder_id=participant_id,
                acquired_at=now(),
            )
            self.store.workspaces.handoff_usage(
                reservation_usage_id=reservation_usage_id,
                participant_usage=participant_usage,
                handed_off_at=participant_usage.acquired_at,
                connection=unit.connection,
            )
            participant.workspace_id = usage.workspace_id
            self.registry.persist_in_connection(participant, unit.connection)
            completed_job = self._finish_promptless_job(
                participant_id,
                timestamp=timestamp,
                connection=unit.connection,
            )
            operation = self._persist_dispatch_identity(operation_id, terminal, unit)
            unit.connection.execute(
                update(launch_reservations)
                .where(launch_reservations.c.operation_id == operation_id)
                .values(phase="terminal_bound", updated_at=now())
            )
            self._append_binding_events(
                unit,
                participant,
                binding,
                participant_usage,
                operation=operation,
                completed_job=completed_job,
            )
            if completed_job is not None:
                unit.after_commit(lambda: self.daemon.jobs.notify_committed_finish(completed_job))
        return participant

    def _finish_promptless_job(
        self,
        participant_id: str,
        *,
        timestamp: float,
        connection,
    ) -> Job | None:
        row = connection.execute(
            select(jobs_table).where(jobs_table.c.handle == participant_id)
        ).first()
        if row is None:
            return None
        job = Job.from_row(row._mapping)
        if job.state != JobState.RUNNING.value or job.prompt:
            return None
        completed = replace(job, state=JobState.DONE.value, result="", finished_at=timestamp)
        changed = connection.execute(
            update(jobs_table)
            .where(
                jobs_table.c.handle == participant_id,
                jobs_table.c.state == JobState.RUNNING.value,
            )
            .values(state=JobState.DONE.value, result="", finished_at=timestamp)
        )
        if changed.rowcount != 1:
            raise RuntimeError("promptless spawn job changed before terminal binding")
        return completed

    def _bind_adoption(
        self,
        operation_id: str,
        participant: Participant,
        terminal: Mapping[str, object],
    ) -> Participant:
        with self.store.write_unit() as unit:
            current = self._participant_in_connection(participant.id, unit.connection)
            if current is None:
                raise RuntimeError("adoption participant disappeared")
            binding = self._binding(current.id, terminal)
            self._ensure_terminal_unbound(binding, unit.connection)
            self.store.terminal_bindings.bind(binding, connection=unit.connection)
            if current.origin is ParticipantOrigin.ADOPTED:
                current.tier = Tier.ADOPTED
            occupant = terminal["occupant"]
            assert isinstance(occupant, Mapping)
            harness = occupant.get("harness")
            if isinstance(harness, str) and harness:
                current.harness = harness
            cwd = occupant.get("cwd")
            if isinstance(cwd, str) and cwd:
                current.cwd = cwd
            self.registry.persist_in_connection(current, unit.connection)
            operation = self._persist_dispatch_identity(operation_id, terminal, unit)
            self._append_binding_events(unit, current, binding, None, operation=operation)
        return current

    def _persist_dispatch_identity(
        self, operation_id: str, terminal: Mapping[str, object], unit: WriteUnit
    ) -> PublicOperationRecord:
        operation = self.store.operations.get(operation_id, connection=unit.connection)
        if operation is None:
            raise RuntimeError("public operation disappeared before terminal binding")
        occupant = terminal["occupant"]
        process = terminal.get("process")
        assert isinstance(occupant, Mapping)
        assert process is None or isinstance(process, Mapping)
        generation = terminal["provider_generation"]
        if type(generation) is not int:
            raise TypeError("terminal provider generation must be an integer")
        updated = replace(
            operation,
            dispatch_provider_id=str(terminal["provider_id"]),
            dispatch_provider_generation=generation,
            dispatch_terminal_id=str(terminal["terminal_id"]),
            dispatch_terminal_incarnation=str(terminal["terminal_incarnation"]),
            dispatch_terminal_occupant_evidence=dict(occupant),
            dispatch_terminal_process_facts=None if process is None else dict(process),
            updated_at=now(),
        )
        if not self.store.operations.replace(
            updated,
            expected_state=operation.state,
            expected_updated_at=operation.updated_at,
            connection=unit.connection,
        ):
            raise RuntimeError("public operation changed during terminal binding")
        return updated

    def _ensure_terminal_unbound(self, candidate: TerminalBindingRecord, connection) -> None:
        for binding in self.store.terminal_bindings.list_for_provider(
            candidate.provider_id, connection=connection
        ):
            if binding.terminal_id != candidate.terminal_id:
                continue
            if binding.participant_id == candidate.participant_id:
                raise TerminalIdentityMismatch(
                    candidate.provider_id, candidate.terminal_id, "participant_already_bound"
                )
            reason = (
                "occupant_replaced"
                if binding.terminal_incarnation == candidate.terminal_incarnation
                and binding.occupant_evidence != candidate.occupant_evidence
                else "terminal_id_reused"
            )
            raise TerminalIdentityMismatch(candidate.provider_id, candidate.terminal_id, reason)

    def _append_binding_events(
        self,
        unit: WriteUnit,
        participant: Participant,
        binding: TerminalBindingRecord,
        usage: WorkspaceUsageRecord | None,
        *,
        operation: PublicOperationRecord,
        completed_job: Job | None = None,
    ) -> None:
        first = self.store.journal.current_sequence(connection=unit.connection) + 1
        timestamp = now()
        events = [
            self._participant_event(
                participant,
                timestamp,
                revision=first,
                connection=unit.connection,
            ),
            terminal_binding_event(
                self.store,
                binding,
                unit.connection,
                revision=first + 1,
                recorded_at=timestamp,
            ),
        ]
        if usage is not None:
            events.append(
                self._workspace_usage_event(
                    usage,
                    timestamp,
                    revision=first + 2,
                    connection=unit.connection,
                )
            )
        if completed_job is not None:
            events.append(self._job_event(completed_job, timestamp, revision=0))
        events.append(
            JournalEventRecord(
                kind="operation.updated",
                entity_id=operation.operation_id,
                entity_revision=first + len(events),
                payload=operation_event_payload(operation),
                recorded_at=timestamp,
            )
        )
        self.store.journal.append_group(unit, events)

    def _validate_adoption(
        self,
        participant: Participant,
        terminal: Mapping[str, object],
        inspected: Mapping[str, object],
        *,
        provider_id: str,
        provider_generation: int,
        terminal_id: str,
        terminal_incarnation: str,
    ) -> None:
        identity = (
            terminal.get("provider_id"),
            terminal.get("provider_generation"),
            terminal.get("terminal_id"),
            terminal.get("terminal_incarnation"),
        )
        if identity != (provider_id, provider_generation, terminal_id, terminal_incarnation):
            raise TerminalIdentityMismatch(provider_id, terminal_id, "inspection_identity")
        occupant = terminal.get("occupant")
        if not isinstance(occupant, Mapping):
            raise TerminalIdentityMismatch(
                str(terminal["provider_id"]), str(terminal["terminal_id"]), "occupant"
            )
        harness = occupant.get("harness")
        if not isinstance(harness, str) or not harness:
            raise TerminalIdentityMismatch(
                str(terminal["provider_id"]), str(terminal["terminal_id"]), "harness"
            )
        if participant.harness not in {"unknown", harness}:
            raise TerminalIdentityMismatch(
                str(terminal["provider_id"]), str(terminal["terminal_id"]), "harness"
            )
        process = terminal.get("process")
        if participant.pid is not None and (
            not isinstance(process, Mapping) or process.get("pid") != participant.pid
        ):
            raise TerminalIdentityMismatch(
                str(terminal["provider_id"]), str(terminal["terminal_id"]), "process"
            )
        lifecycle = inspected.get("lifecycle")
        if isinstance(lifecycle, Mapping) and lifecycle.get("alive") is False:
            raise TerminalIdentityMismatch(
                str(terminal["provider_id"]), str(terminal["terminal_id"]), "occupant_not_live"
            )
        if participant.session_id and is_trusted_provenance(participant.session_correlation):
            if not isinstance(lifecycle, Mapping):
                raise TerminalIdentityMismatch(
                    str(terminal["provider_id"]),
                    str(terminal["terminal_id"]),
                    "trusted_identity_missing",
                )
            reported_session = lifecycle.get("native_session_id") or lifecycle.get("session_id")
            if reported_session != participant.session_id:
                raise TerminalIdentityMismatch(
                    str(terminal["provider_id"]),
                    str(terminal["terminal_id"]),
                    "trusted_identity",
                )

    @staticmethod
    def _inspected_terminal(
        inspected: Mapping[str, object], *, provider_id: str, terminal_id: str
    ) -> Mapping[str, object]:
        terminal = inspected.get("terminal")
        if not isinstance(terminal, Mapping):
            raise TerminalIdentityMismatch(provider_id, terminal_id, "inspection_missing_identity")
        return terminal

    @staticmethod
    def _binding(participant_id: str, terminal: Mapping[str, object]) -> TerminalBindingRecord:
        occupant = terminal["occupant"]
        process = terminal.get("process")
        assert isinstance(occupant, Mapping)
        assert process is None or isinstance(process, Mapping)
        generation = terminal["provider_generation"]
        if type(generation) is not int:
            raise TypeError("terminal provider generation must be an integer")
        timestamp = now()
        return TerminalBindingRecord(
            participant_id=participant_id,
            provider_id=str(terminal["provider_id"]),
            provider_generation=generation,
            terminal_id=str(terminal["terminal_id"]),
            terminal_incarnation=str(terminal["terminal_incarnation"]),
            occupant_evidence=dict(occupant),
            process_facts=None if process is None else dict(process),
            health="healthy",
            report_revision=0,
            created_at=timestamp,
            updated_at=timestamp,
        )

    @staticmethod
    def _participant_in_connection(participant_id: str, connection) -> Participant | None:
        row = connection.execute(
            select(participants).where(participants.c.id == participant_id)
        ).first()
        return Participant.from_row(row._mapping) if row is not None else None

    @staticmethod
    def _workspace_request(params: Mapping[str, object]) -> WorkspaceRequest:
        raw = params.get("workspace")
        values = raw if isinstance(raw, Mapping) else {}
        workspace_id = values.get("workspace_id")
        cwd = values.get("cwd", params.get("cwd"))
        worktree = values.get("worktree", False)
        base_ref = values.get("base_ref")
        if workspace_id is not None and not isinstance(workspace_id, str):
            raise BadRequest("workspace_id must be a string")
        if cwd is not None and not isinstance(cwd, str):
            raise BadRequest("workspace cwd must be a string")
        if not isinstance(worktree, (bool, str)):
            raise BadRequest("workspace worktree must be a boolean or name")
        if base_ref is not None and not isinstance(base_ref, str):
            raise BadRequest("workspace base_ref must be a string")
        return WorkspaceRequest(
            workspace_id=workspace_id,
            cwd=cwd,
            worktree=worktree,
            base_ref=base_ref,
        )

    @staticmethod
    def _workspace_request_facts(request: WorkspaceRequest) -> dict[str, object]:
        return {
            "workspace_id": request.workspace_id,
            "cwd": request.cwd,
            "worktree": request.worktree,
            "base_ref": request.base_ref,
        }

    @staticmethod
    def _spawn_request(
        params: Mapping[str, object],
        *,
        parent_id: str | None,
        cwd: str,
        worktree: bool | str,
        base_ref: str | None,
    ) -> SpawnRequest:
        raw_wiring = params.get("wiring", RuntimeWiring.AUTO.value)
        try:
            wiring = RuntimeWiring(str(raw_wiring))
        except ValueError:
            raise BadRequest("wiring must be auto, native, or legacy") from None
        return SpawnRequest(
            harness=str(params["harness"]),
            prompt=str(params["prompt"]),
            cwd=cwd,
            approval=str(params["approval"]),
            parent_id=parent_id,
            worktree=worktree,
            base_branch=base_ref,
            model=ParticipantLaunchService._optional_text(params.get("model")),
            reasoning_effort=ParticipantLaunchService._optional_text(
                params.get("reasoning_effort")
            ),
            resume=ParticipantLaunchService._optional_text(params.get("resume")),
            name=ParticipantLaunchService._optional_text(params.get("name")),
            description=ParticipantLaunchService._optional_text(params.get("description")),
            wiring=wiring,
            response_format=ParticipantLaunchService._optional_text(params.get("response_format")),
        )

    @staticmethod
    def _validate_workspace_request(request: WorkspaceRequest) -> None:
        if request.workspace_id is not None:
            if (
                request.cwd is not None
                or request.worktree is not False
                or request.base_ref is not None
            ):
                raise BadRequest("workspace_id cannot be combined with cwd, worktree, or base_ref")
            return
        if request.cwd is None:
            raise BadRequest("workspace preparation requires workspace_id or cwd")
        if not isinstance(request.worktree, (bool, str)) or request.worktree == "":
            raise BadRequest("worktree must be false, true, or a non-empty name")
        if request.base_ref is not None and request.worktree is False:
            raise BadRequest("base_ref requires unique or named worktree creation")

    @staticmethod
    def _optional_text(value: object) -> str | None:
        return value if isinstance(value, str) and value else None

    def _participant_event(
        self,
        participant: Participant,
        timestamp: float,
        *,
        revision: int,
        connection,
    ) -> JournalEventRecord:
        return participant_event(
            self.store,
            participant,
            connection,
            revision=revision,
            recorded_at=timestamp,
        )

    @staticmethod
    def _job_event(job: Job, timestamp: float, *, revision: int) -> JournalEventRecord:
        return job_event(job, revision=revision, recorded_at=timestamp)

    def _workspace_usage_event(
        self,
        usage: WorkspaceUsageRecord,
        timestamp: float,
        *,
        revision: int,
        connection,
    ) -> JournalEventRecord:
        return workspace_usage_event(
            self.store,
            usage,
            connection,
            revision=revision,
            recorded_at=timestamp,
        )


__all__ = ["ParticipantLaunchService"]
