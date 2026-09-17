"""Curated domain methods for the frozen frontend public catalog."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from theater.frontend._results import (
    AcceptedOperation,
    FrontendResult,
    JobsAwaitResult,
    OperationAwaitResult,
    Page,
    StateFollowResult,
    decode_accepted_operation,
    decode_jobs_await,
    decode_operation_await,
    decode_page,
    decode_state_follow,
    freeze_object,
    result_of,
)
from theater.frontend.dto import (
    Controls,
    EventCursor,
    HarnessCatalogEntry,
    Job,
    Operation,
    Participant,
    Provider,
    SnapshotPage,
    TerminalIdentity,
    Workspace,
)
from theater.frontend.dto._wire import JSONValue, freeze_json

if TYPE_CHECKING:
    from theater.frontend.client import FrontendClient
    from theater.frontend.dto import Response


class _Unset:
    pass


_UNSET = _Unset()


class _Facade:
    def __init__(self, client: FrontendClient) -> None:
        self._client = client

    async def _call(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        idempotency_key: str | None = None,
    ) -> Response:
        return await self._client._request(method, params, idempotency_key=idempotency_key)


class ContractClient(_Facade):
    async def get(self) -> FrontendResult[Mapping[str, JSONValue]]:
        return result_of(await self._call("frontend.contract.get", {}), freeze_object)


class SchemasClient(_Facade):
    async def get(self) -> FrontendResult[Mapping[str, JSONValue]]:
        return result_of(await self._call("frontend.schemas.get", {}), freeze_object)


class HealthClient(_Facade):
    async def get(self) -> FrontendResult[Mapping[str, JSONValue]]:
        return result_of(await self._call("frontend.health.get", {}), freeze_object)


class StateClient(_Facade):
    async def snapshot(self, *, page_size: object = _UNSET) -> FrontendResult[SnapshotPage]:
        return result_of(
            await self._call("frontend.state.snapshot", _params(page_size=page_size)),
            SnapshotPage.from_wire,
        )

    async def page(self, snapshot_id: str, page: int) -> FrontendResult[SnapshotPage]:
        return result_of(
            await self._call("frontend.state.page", {"snapshot_id": snapshot_id, "page": page}),
            SnapshotPage.from_wire,
        )

    async def release(self, snapshot_id: str) -> FrontendResult[Mapping[str, JSONValue]]:
        return result_of(
            await self._call("frontend.state.release", {"snapshot_id": snapshot_id}), freeze_object
        )

    async def follow(
        self,
        cursor: EventCursor | Mapping[str, object],
        *,
        wait_seconds: object = _UNSET,
        limit: object = _UNSET,
    ) -> FrontendResult[StateFollowResult]:
        cursor_value = cursor.to_wire() if isinstance(cursor, EventCursor) else cursor
        return result_of(
            await self._call(
                "frontend.state.follow",
                _params(cursor=cursor_value, wait_seconds=wait_seconds, limit=limit),
            ),
            decode_state_follow,
        )


class ParticipantsClient(_Facade):
    async def list(
        self,
        *,
        cursor: object = _UNSET,
        limit: object = _UNSET,
        status: object = _UNSET,
        owner_id: object = _UNSET,
    ) -> FrontendResult[Page[Participant]]:
        return result_of(
            await self._call(
                "frontend.participants.list",
                _params(cursor=cursor, limit=limit, status=status, owner_id=owner_id),
            ),
            lambda value: decode_page(value, Participant.from_wire),
        )

    async def get(self, participant_id: str) -> FrontendResult[Participant]:
        return result_of(
            await self._call("frontend.participants.get", {"participant_id": participant_id}),
            Participant.from_wire,
        )

    async def tree(self, participant_id: str) -> FrontendResult[Page[Participant]]:
        return result_of(
            await self._call("frontend.participants.tree", {"participant_id": participant_id}),
            lambda value: decode_page(value, Participant.from_wire),
        )

    async def spawn(
        self,
        harness: str,
        prompt: str,
        approval: str,
        *,
        idempotency_key: str,
        cwd: object = _UNSET,
        provider: object = _UNSET,
        workspace: object = _UNSET,
        model: object = _UNSET,
        reasoning_effort: object = _UNSET,
        resume: object = _UNSET,
        name: object = _UNSET,
        description: object = _UNSET,
        initiating_participant_id: object = _UNSET,
    ) -> FrontendResult[AcceptedOperation]:
        return result_of(
            await self._call(
                "frontend.participants.spawn",
                _params(
                    harness=harness,
                    prompt=prompt,
                    approval=approval,
                    cwd=cwd,
                    provider=provider,
                    workspace=workspace,
                    model=model,
                    reasoning_effort=reasoning_effort,
                    resume=resume,
                    name=name,
                    description=description,
                    initiating_participant_id=initiating_participant_id,
                ),
                idempotency_key=idempotency_key,
            ),
            decode_accepted_operation,
        )

    async def adopt(
        self,
        provider_id: str,
        terminal_id: str,
        terminal_incarnation: str,
        *,
        idempotency_key: str,
        participant_id: object = _UNSET,
        initiating_participant_id: object = _UNSET,
    ) -> FrontendResult[AcceptedOperation]:
        return result_of(
            await self._call(
                "frontend.participants.adopt",
                _params(
                    provider_id=provider_id,
                    terminal_id=terminal_id,
                    terminal_incarnation=terminal_incarnation,
                    participant_id=participant_id,
                    initiating_participant_id=initiating_participant_id,
                ),
                idempotency_key=idempotency_key,
            ),
            decode_accepted_operation,
        )

    async def update(
        self,
        participant_id: str,
        *,
        idempotency_key: str,
        name: object = _UNSET,
        description: object = _UNSET,
    ) -> FrontendResult[Participant]:
        return result_of(
            await self._call(
                "frontend.participants.update",
                _params(participant_id=participant_id, name=name, description=description),
                idempotency_key=idempotency_key,
            ),
            Participant.from_wire,
        )

    async def status(
        self, participant_id: str, status: str, *, idempotency_key: str
    ) -> FrontendResult[Participant]:
        return result_of(
            await self._call(
                "frontend.participants.status",
                {"participant_id": participant_id, "status": status},
                idempotency_key=idempotency_key,
            ),
            Participant.from_wire,
        )

    async def terminate(
        self, participant_id: str, *, idempotency_key: str
    ) -> FrontendResult[AcceptedOperation]:
        return result_of(
            await self._call(
                "frontend.participants.terminate",
                {"participant_id": participant_id},
                idempotency_key=idempotency_key,
            ),
            decode_accepted_operation,
        )

    async def transfer_control(
        self,
        participants: Sequence[Mapping[str, object]],
        new_owner: Mapping[str, object],
        *,
        idempotency_key: str,
    ) -> FrontendResult[Mapping[str, JSONValue]]:
        return result_of(
            await self._call(
                "frontend.participants.transfer_control",
                {"participants": list(participants), "new_owner": dict(new_owner)},
                idempotency_key=idempotency_key,
            ),
            freeze_object,
        )


class ControlsClient(_Facade):
    async def get(self, participant_id: str) -> FrontendResult[Controls]:
        return result_of(
            await self._call("frontend.controls.get", {"participant_id": participant_id}),
            Controls.from_wire,
        )

    async def send(
        self,
        participant_id: str,
        prompt: str,
        *,
        idempotency_key: str,
        response_format: object = _UNSET,
    ) -> FrontendResult[AcceptedOperation]:
        return await self._prompt_operation(
            "frontend.controls.send",
            participant_id,
            prompt,
            idempotency_key=idempotency_key,
            response_format=response_format,
        )

    async def steer(
        self,
        participant_id: str,
        prompt: str,
        *,
        idempotency_key: str,
        expected_turn_id: object = _UNSET,
    ) -> FrontendResult[AcceptedOperation]:
        return result_of(
            await self._call(
                "frontend.controls.steer",
                _params(
                    participant_id=participant_id,
                    prompt=prompt,
                    expected_turn_id=expected_turn_id,
                ),
                idempotency_key=idempotency_key,
            ),
            decode_accepted_operation,
        )

    async def queue_followup(
        self,
        participant_id: str,
        prompt: str,
        *,
        idempotency_key: str,
        response_format: object = _UNSET,
    ) -> FrontendResult[AcceptedOperation]:
        return await self._prompt_operation(
            "frontend.controls.queue_followup",
            participant_id,
            prompt,
            idempotency_key=idempotency_key,
            response_format=response_format,
        )

    async def interrupt(
        self, participant_id: str, *, idempotency_key: str
    ) -> FrontendResult[AcceptedOperation]:
        return result_of(
            await self._call(
                "frontend.controls.interrupt",
                {"participant_id": participant_id},
                idempotency_key=idempotency_key,
            ),
            decode_accepted_operation,
        )

    async def update_settings(
        self,
        participant_id: str,
        *,
        idempotency_key: str,
        model: object = _UNSET,
        reasoning_effort: object = _UNSET,
    ) -> FrontendResult[AcceptedOperation]:
        return result_of(
            await self._call(
                "frontend.controls.settings.update",
                _params(
                    participant_id=participant_id,
                    model=model,
                    reasoning_effort=reasoning_effort,
                ),
                idempotency_key=idempotency_key,
            ),
            decode_accepted_operation,
        )

    async def _prompt_operation(
        self,
        method: str,
        participant_id: str,
        prompt: str,
        *,
        idempotency_key: str,
        response_format: object,
    ) -> FrontendResult[AcceptedOperation]:
        return result_of(
            await self._call(
                method,
                _params(
                    participant_id=participant_id,
                    prompt=prompt,
                    response_format=response_format,
                ),
                idempotency_key=idempotency_key,
            ),
            decode_accepted_operation,
        )


class OperationsClient(_Facade):
    async def list(
        self,
        *,
        cursor: object = _UNSET,
        limit: object = _UNSET,
        unsettled_only: object = _UNSET,
        target_id: object = _UNSET,
    ) -> FrontendResult[Page[Operation]]:
        return result_of(
            await self._call(
                "frontend.operations.list",
                _params(
                    cursor=cursor,
                    limit=limit,
                    unsettled_only=unsettled_only,
                    target_id=target_id,
                ),
            ),
            lambda value: decode_page(value, Operation.from_wire),
        )

    async def get(self, operation_id: str) -> FrontendResult[Operation]:
        return result_of(
            await self._call("frontend.operations.get", {"operation_id": operation_id}),
            Operation.from_wire,
        )

    async def await_(
        self, operation_id: str, *, wait_seconds: object = _UNSET
    ) -> FrontendResult[OperationAwaitResult]:
        return result_of(
            await self._call(
                "frontend.operations.await",
                _params(operation_id=operation_id, wait_seconds=wait_seconds),
            ),
            decode_operation_await,
        )

    async def reconcile(self, operation_id: str) -> FrontendResult[Operation]:
        return result_of(
            await self._call("frontend.operations.reconcile", {"operation_id": operation_id}),
            Operation.from_wire,
        )


class JobsClient(_Facade):
    async def list(
        self,
        *,
        cursor: object = _UNSET,
        limit: object = _UNSET,
        state: object = _UNSET,
        participant_id: object = _UNSET,
    ) -> FrontendResult[Page[Job]]:
        return result_of(
            await self._call(
                "frontend.jobs.list",
                _params(cursor=cursor, limit=limit, state=state, participant_id=participant_id),
            ),
            lambda value: decode_page(value, Job.from_wire),
        )

    async def get(self, job_handle: str) -> FrontendResult[Job]:
        return result_of(
            await self._call("frontend.jobs.get", {"job_handle": job_handle}), Job.from_wire
        )

    async def await_(
        self, job_handles: Sequence[str], *, wait_seconds: object = _UNSET
    ) -> FrontendResult[JobsAwaitResult]:
        return result_of(
            await self._call(
                "frontend.jobs.await",
                _params(job_handles=list(job_handles), wait_seconds=wait_seconds),
            ),
            decode_jobs_await,
        )


class ProviderTerminalsClient(_Facade):
    async def list(
        self,
        provider_id: str,
        *,
        cursor: object = _UNSET,
        limit: object = _UNSET,
        refresh: object = _UNSET,
    ) -> FrontendResult[Page[TerminalIdentity]]:
        return result_of(
            await self._call(
                "frontend.providers.terminals.list",
                _params(provider_id=provider_id, cursor=cursor, limit=limit, refresh=refresh),
            ),
            lambda value: decode_page(value, TerminalIdentity.from_wire),
        )

    async def inspect(
        self, provider_id: str, terminal_id: str, terminal_incarnation: str
    ) -> FrontendResult[Mapping[str, JSONValue]]:
        return result_of(
            await self._call(
                "frontend.providers.terminals.inspect",
                {
                    "provider_id": provider_id,
                    "terminal_id": terminal_id,
                    "terminal_incarnation": terminal_incarnation,
                },
            ),
            freeze_object,
        )


class ProvidersClient(_Facade):
    def __init__(self, client: FrontendClient) -> None:
        super().__init__(client)
        self.terminals = ProviderTerminalsClient(client)

    async def register(
        self,
        selector: str,
        kind: str,
        credential_verifier: str,
        capabilities: Sequence[str],
        limits: Mapping[str, object],
        *,
        idempotency_key: str,
    ) -> FrontendResult[Provider]:
        return result_of(
            await self._call(
                "frontend.providers.register",
                {
                    "selector": selector,
                    "kind": kind,
                    "credential_verifier": credential_verifier,
                    "capabilities": list(capabilities),
                    "limits": dict(limits),
                },
                idempotency_key=idempotency_key,
            ),
            Provider.from_wire,
        )

    async def update(
        self,
        provider_id: str,
        *,
        idempotency_key: str,
        capabilities: object = _UNSET,
        limits: object = _UNSET,
    ) -> FrontendResult[Provider]:
        if capabilities is not _UNSET:
            capabilities = _array_value(capabilities, "capabilities")
        if limits is not _UNSET:
            limits = _mapping_value(limits, "limits")
        return result_of(
            await self._call(
                "frontend.providers.update",
                _params(provider_id=provider_id, capabilities=capabilities, limits=limits),
                idempotency_key=idempotency_key,
            ),
            Provider.from_wire,
        )

    async def list(
        self, *, cursor: object = _UNSET, limit: object = _UNSET
    ) -> FrontendResult[Page[Provider]]:
        return result_of(
            await self._call("frontend.providers.list", _params(cursor=cursor, limit=limit)),
            lambda value: decode_page(value, Provider.from_wire),
        )

    async def get(self, provider_id: str) -> FrontendResult[Provider]:
        return result_of(
            await self._call("frontend.providers.get", {"provider_id": provider_id}),
            Provider.from_wire,
        )

    async def heartbeat(
        self,
        provider_generation: int,
        report_revision: int,
        *,
        facts: object = _UNSET,
    ) -> FrontendResult[Mapping[str, JSONValue]]:
        return result_of(
            await self._call(
                "frontend.providers.heartbeat",
                _params(
                    provider_generation=provider_generation,
                    report_revision=report_revision,
                    facts=facts,
                ),
            ),
            freeze_object,
        )

    async def report(
        self,
        provider_generation: int,
        report_revision: int,
        *,
        facts: object = _UNSET,
    ) -> FrontendResult[Mapping[str, JSONValue]]:
        return result_of(
            await self._call(
                "frontend.providers.report",
                _params(
                    provider_generation=provider_generation,
                    report_revision=report_revision,
                    facts=facts,
                ),
            ),
            freeze_object,
        )


class WorkspacesClient(_Facade):
    async def register(
        self,
        ownership_kind: str,
        owner_id: str,
        path: str,
        *,
        idempotency_key: str,
        canonical_repository_root: object = _UNSET,
        branch: object = _UNSET,
        resolved_base_commit: object = _UNSET,
    ) -> FrontendResult[Workspace]:
        return result_of(
            await self._call(
                "frontend.workspaces.register",
                _params(
                    ownership_kind=ownership_kind,
                    owner_id=owner_id,
                    path=path,
                    canonical_repository_root=canonical_repository_root,
                    branch=branch,
                    resolved_base_commit=resolved_base_commit,
                ),
                idempotency_key=idempotency_key,
            ),
            Workspace.from_wire,
        )

    async def list(
        self,
        *,
        cursor: object = _UNSET,
        limit: object = _UNSET,
        state: object = _UNSET,
    ) -> FrontendResult[Page[Workspace]]:
        return result_of(
            await self._call(
                "frontend.workspaces.list", _params(cursor=cursor, limit=limit, state=state)
            ),
            lambda value: decode_page(value, Workspace.from_wire),
        )

    async def get(self, workspace_id: str) -> FrontendResult[Workspace]:
        return result_of(
            await self._call("frontend.workspaces.get", {"workspace_id": workspace_id}),
            Workspace.from_wire,
        )

    async def cleanup(
        self,
        workspace_id: str,
        *,
        idempotency_key: str,
        force: object = _UNSET,
        delete_branch: object = _UNSET,
        force_branch: object = _UNSET,
    ) -> FrontendResult[AcceptedOperation]:
        return result_of(
            await self._call(
                "frontend.workspaces.cleanup",
                _params(
                    workspace_id=workspace_id,
                    force=force,
                    delete_branch=delete_branch,
                    force_branch=force_branch,
                ),
                idempotency_key=idempotency_key,
            ),
            decode_accepted_operation,
        )

    async def prepare_delete(
        self, workspace_id: str, *, idempotency_key: str
    ) -> FrontendResult[Mapping[str, JSONValue]]:
        return result_of(
            await self._call(
                "frontend.workspaces.prepare_delete",
                {"workspace_id": workspace_id},
                idempotency_key=idempotency_key,
            ),
            freeze_object,
        )

    async def confirm_delete(
        self, workspace_id: str, token: str, *, idempotency_key: str
    ) -> FrontendResult[Mapping[str, JSONValue]]:
        return await self._delete_fence(
            "frontend.workspaces.confirm_delete", workspace_id, token, idempotency_key
        )

    async def cancel_delete(
        self, workspace_id: str, token: str, *, idempotency_key: str
    ) -> FrontendResult[Mapping[str, JSONValue]]:
        return await self._delete_fence(
            "frontend.workspaces.cancel_delete", workspace_id, token, idempotency_key
        )

    async def _delete_fence(
        self, method: str, workspace_id: str, token: str, idempotency_key: str
    ) -> FrontendResult[Mapping[str, JSONValue]]:
        return result_of(
            await self._call(
                method,
                {"workspace_id": workspace_id, "token": token},
                idempotency_key=idempotency_key,
            ),
            freeze_object,
        )


class ScratchpadClient(_Facade):
    async def namespaces(
        self, *, cursor: object = _UNSET, limit: object = _UNSET
    ) -> FrontendResult[Page[JSONValue]]:
        return result_of(
            await self._call("frontend.scratchpad.namespaces", _params(cursor=cursor, limit=limit)),
            lambda value: decode_page(value, _json_value),
        )

    async def get(
        self,
        namespace: str,
        *,
        keys: object = _UNSET,
        cursor: object = _UNSET,
        limit: object = _UNSET,
    ) -> FrontendResult[Page[JSONValue]]:
        if keys is not _UNSET:
            keys = _array_value(keys, "keys")
        return result_of(
            await self._call(
                "frontend.scratchpad.get",
                _params(namespace=namespace, keys=keys, cursor=cursor, limit=limit),
            ),
            lambda value: decode_page(value, _json_value),
        )

    async def write(
        self,
        namespace: str,
        value: str,
        *,
        idempotency_key: str,
        key: object = _UNSET,
    ) -> FrontendResult[Mapping[str, JSONValue]]:
        return result_of(
            await self._call(
                "frontend.scratchpad.write",
                _params(namespace=namespace, value=value, key=key),
                idempotency_key=idempotency_key,
            ),
            freeze_object,
        )

    async def delete(
        self,
        namespace: str,
        *,
        idempotency_key: str,
        keys: object = _UNSET,
        clear: object = _UNSET,
    ) -> FrontendResult[Mapping[str, JSONValue]]:
        if keys is not _UNSET:
            keys = _array_value(keys, "keys")
        return result_of(
            await self._call(
                "frontend.scratchpad.delete",
                _params(namespace=namespace, keys=keys, clear=clear),
                idempotency_key=idempotency_key,
            ),
            freeze_object,
        )


class CatalogsClient(_Facade):
    async def harnesses(
        self, *, provider: object = _UNSET
    ) -> FrontendResult[Page[HarnessCatalogEntry]]:
        return result_of(
            await self._call("frontend.catalogs.harnesses", _params(provider=provider)),
            lambda value: decode_page(value, HarnessCatalogEntry.from_wire),
        )

    async def models(self, *, provider: object = _UNSET) -> FrontendResult[Page[JSONValue]]:
        return result_of(
            await self._call("frontend.catalogs.models", _params(provider=provider)),
            lambda value: decode_page(value, _json_value),
        )


class SkillsClient(_Facade):
    async def list(self) -> FrontendResult[Page[JSONValue]]:
        return result_of(
            await self._call("frontend.skills.list", {}),
            lambda value: decode_page(value, _json_value),
        )

    async def load(self, name: str) -> FrontendResult[Mapping[str, JSONValue]]:
        return result_of(await self._call("frontend.skills.load", {"name": name}), freeze_object)


class TranscriptsClient(_Facade):
    async def read(
        self,
        participant_id: str,
        *,
        cursor: object = _UNSET,
        max_bytes: object = _UNSET,
    ) -> FrontendResult[Mapping[str, JSONValue]]:
        return result_of(
            await self._call(
                "frontend.transcripts.read",
                _params(participant_id=participant_id, cursor=cursor, max_bytes=max_bytes),
            ),
            freeze_object,
        )

    async def candidates(self, participant_id: str) -> FrontendResult[Page[JSONValue]]:
        return result_of(
            await self._call("frontend.transcripts.candidates", {"participant_id": participant_id}),
            lambda value: decode_page(value, _json_value),
        )

    async def bind(
        self,
        participant_id: str,
        location: str,
        *,
        idempotency_key: str,
        prior_owner_id: object = _UNSET,
    ) -> FrontendResult[Mapping[str, JSONValue]]:
        return result_of(
            await self._call(
                "frontend.transcripts.bind",
                _params(
                    participant_id=participant_id,
                    location=location,
                    prior_owner_id=prior_owner_id,
                ),
                idempotency_key=idempotency_key,
            ),
            freeze_object,
        )


class RecallClient(_Facade):
    async def query(
        self, path: str, *, cursor: object = _UNSET, limit: object = _UNSET
    ) -> FrontendResult[Page[JSONValue]]:
        return result_of(
            await self._call(
                "frontend.recall.query", _params(path=path, cursor=cursor, limit=limit)
            ),
            lambda value: decode_page(value, _json_value),
        )

    async def read(
        self,
        segment_id: str,
        *,
        offset: object = _UNSET,
        max_bytes: object = _UNSET,
    ) -> FrontendResult[Mapping[str, JSONValue]]:
        return result_of(
            await self._call(
                "frontend.recall.read",
                _params(segment_id=segment_id, offset=offset, max_bytes=max_bytes),
            ),
            freeze_object,
        )


class TrajectoryClient(_Facade):
    async def snapshot(
        self, participant_id: str, *, before: object = _UNSET, limit: object = _UNSET
    ) -> FrontendResult[Mapping[str, JSONValue]]:
        return result_of(
            await self._call(
                "frontend.trajectory.snapshot",
                _params(participant_id=participant_id, before=before, limit=limit),
            ),
            freeze_object,
        )

    async def follow(
        self, stream_id: str, cursor: object, *, wait_seconds: object = _UNSET
    ) -> FrontendResult[Mapping[str, JSONValue]]:
        return result_of(
            await self._call(
                "frontend.trajectory.follow",
                _params(stream_id=stream_id, cursor=cursor, wait_seconds=wait_seconds),
            ),
            freeze_object,
        )

    async def close(self, stream_id: str) -> FrontendResult[Mapping[str, JSONValue]]:
        return result_of(
            await self._call("frontend.trajectory.close", {"stream_id": stream_id}), freeze_object
        )

    async def locate(
        self, participant_id: str, record_id: str
    ) -> FrontendResult[Mapping[str, JSONValue]]:
        return result_of(
            await self._call(
                "frontend.trajectory.locate",
                {"participant_id": participant_id, "record_id": record_id},
            ),
            freeze_object,
        )

    async def search(
        self, participant_id: str, query: str, *, limit: object = _UNSET
    ) -> FrontendResult[Page[JSONValue]]:
        return result_of(
            await self._call(
                "frontend.trajectory.search",
                _params(participant_id=participant_id, query=query, limit=limit),
            ),
            lambda value: decode_page(value, _json_value),
        )


class UsageClient(_Facade):
    async def totals(self, *, since: object = _UNSET) -> FrontendResult[Mapping[str, JSONValue]]:
        return result_of(
            await self._call("frontend.usage.totals", _params(since=since)), freeze_object
        )

    async def summary(self, *, since: object = _UNSET) -> FrontendResult[Mapping[str, JSONValue]]:
        return result_of(
            await self._call("frontend.usage.summary", _params(since=since)), freeze_object
        )

    async def by_harness(
        self, *, since: object = _UNSET
    ) -> FrontendResult[Mapping[str, JSONValue]]:
        return result_of(
            await self._call("frontend.usage.by_harness", _params(since=since)), freeze_object
        )


class DiagnosticsClient(_Facade):
    async def stats(self) -> FrontendResult[Mapping[str, JSONValue]]:
        return result_of(await self._call("frontend.stats.get", {}), freeze_object)

    async def bus_tail(
        self, *, after_id: object = _UNSET, limit: object = _UNSET
    ) -> FrontendResult[Page[JSONValue]]:
        return result_of(
            await self._call("frontend.bus.tail", _params(after_id=after_id, limit=limit)),
            lambda value: decode_page(value, _json_value),
        )


def _params(**values: object) -> dict[str, object]:
    return {name: value for name, value in values.items() if value is not _UNSET}


def _array_value(value: object, label: str) -> list[object]:
    if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
        raise TypeError(f"{label} must be an array")
    return list(value)


def _mapping_value(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise TypeError(f"{label} must be an object with string keys")
    return dict(value)


def _json_value(value: object) -> JSONValue:
    return freeze_json(value)


__all__ = [
    "CatalogsClient",
    "ContractClient",
    "ControlsClient",
    "DiagnosticsClient",
    "HealthClient",
    "JobsClient",
    "OperationsClient",
    "ParticipantsClient",
    "ProviderTerminalsClient",
    "ProvidersClient",
    "RecallClient",
    "SchemasClient",
    "ScratchpadClient",
    "SkillsClient",
    "StateClient",
    "TrajectoryClient",
    "TranscriptsClient",
    "UsageClient",
    "WorkspacesClient",
]
