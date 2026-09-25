"""Spawn admission, workspace reservation, and durable acceptance."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError

from theater.daemon.operations import (
    OperationAcceptance,
    PreparedOperation,
    request_digest,
)
from theater.daemon.persistence.transactions import WriteUnit
from theater.daemon.rails import check_budget, check_depth
from theater.daemon.schema import jobs as jobs_table
from theater.daemon.spawning.provider_launch_parts._common import _SpawnAdmission
from theater.daemon.spawning.provider_launch_parts._host import ParticipantLaunchHost
from theater.daemon.worktrees.service import (
    WorkspacePreparation,
    WorkspaceRequest,
    WorkspaceReservation,
)
from theater.frontend.capabilities import MethodClass
from theater.harness import get as get_harness
from theater.harness.contracts.runtime import RuntimeWiring
from theater.models import (
    BadRequest,
    Job,
    JobState,
    JournalEventRecord,
    LaunchReservationRecord,
    Participant,
    PublicOperationRecord,
    WorkspaceState,
    new_id,
    now,
)


class SpawnAdmission(ParticipantLaunchHost):
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
        # The store's autocommit view is safe here: no await separates this recheck from the insert.
        rails = self.daemon.config.rails
        check_depth(self.store, admission.parent_id, cap=rails.depth_cap)
        check_budget(self.store, admission.parent_id, limit=rails.budget)
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
        if self._resolve_binary(harness.binary) is None:
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
        def attach_touch_accumulator() -> None:
            self.daemon.jobs.attach_touch_accumulator(participant.id, cwd=cwd)

        unit.after_commit(attach_touch_accumulator)
        if participant.name is not None:
            unit.after_commit(
                lambda: self.registry.remember_reserved_name(participant.id, participant.name or "")
            )

        def publish_participant() -> None:
            self.store.bus_append(
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

        def publish_job() -> None:
            self.store.bus_append(
                "job.created",
                from_id=job.caller_id,
                to_id=participant.id,
                payload={"handle": participant.id, "kind": "spawn"},
            )

        unit.after_commit(publish_participant)
        unit.after_commit(publish_job)

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
