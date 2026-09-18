"""Participant RPC handlers: hello, list, get, tree, status, rename, kill, adopt, unmanaged.

Also owns ``_resume_state``, the generic resume pre-flight verdict used by
``participants.list`` and ``participants.recent_dead``.
"""

from __future__ import annotations

from dataclasses import replace

from theater.constants.daemon import (
    BUS_KIND_PARTICIPANT_KILL_REQUESTED,
    PARTICIPANTS_LIST_MAX_LIMIT,
)
from theater.daemon.events.publication import next_revision, participant_event
from theater.daemon.presence import access as presence_access
from theater.daemon.rpc.params import _require
from theater.daemon.rpc.router import method
from theater.harness import HARNESSES, normalize, supports_resume
from theater.harness.contracts.runtime import RuntimeCapability
from theater.models import (
    BadRequest,
    ControlOwnerKind,
    JobState,
    NoSelfKill,
    NotYourChild,
    Participant,
    Status,
    TheaterError,
    new_id,
    normalize_participant_description,
    now,
)
from theater.provenance import is_trusted_provenance


def _with_presence(daemon, record: dict) -> dict:
    """Attach the cached focus projection to one participant wire record."""
    route = daemon.controls.route_for(record["id"], RuntimeCapability.SEND)
    record["addressable"] = record.get("status") != Status.DEAD.value and route.route_available
    record["human_presence"] = presence_access.presence_snapshot(daemon, record["id"]).to_dict()
    return record


def _tree_with_presence(daemon, nodes: list[dict]) -> list[dict]:
    """Attach presence to returned tree nodes only; no new broad queries."""
    for node in nodes:
        _with_presence(daemon, node)
        _tree_with_presence(daemon, node["children"])
    return nodes


def authorize_participant_mutation(target: Participant, caller_id: str) -> None:
    """Apply current control ownership, keeping the local operator privileged."""
    if caller_id == "cli":
        return
    owner_kind = target.control_owner_kind or (
        ControlOwnerKind.PARTICIPANT
        if target.parent_id is not None
        else ControlOwnerKind.LOCAL_OPERATOR
    )
    owner_id = target.control_owner_id or target.parent_id
    if caller_id != target.id and (
        owner_kind is not ControlOwnerKind.PARTICIPANT or owner_id != caller_id
    ):
        raise NotYourChild(
            f"refusing to mutate {target.id!r}: its current control owner is "
            f"{owner_id or owner_kind.value!r}, not you ({caller_id!r})"
        )


def update_participant_metadata(
    daemon,
    participant_id: str,
    *,
    caller_id: str,
    name: str | None,
    description: str | None,
    unit=None,
) -> Participant:
    target = daemon.registry.resolve(participant_id)
    authorize_participant_mutation(target, caller_id)
    if unit is not None:
        if target.status is Status.DEAD:
            raise BadRequest(f"cannot update participant {target.id!r}: it is dead")
        normalized_description = (
            normalize_participant_description(description) if description is not None else None
        )
        if name is not None:
            daemon.registry._validate_name(target.id, name)
        updated = replace(target, name=name, description=normalized_description)
        daemon.registry.persist_in_connection(updated, unit.connection)
        event = participant_event(
            daemon.store,
            updated,
            unit.connection,
            revision=next_revision(daemon.store, unit.connection),
            recorded_at=now(),
        )
        daemon.store.journal.append_group(unit, [event])

        def update_live_name() -> None:
            if name is None:
                daemon.registry._names.pop(target.id, None)
            else:
                daemon.registry._names[target.id] = name

        unit.after_commit(update_live_name)
        return updated
    return daemon.registry.update_metadata(
        target.id,
        name=name,
        description=description,
    )


async def update_participant_status(
    daemon, participant_id: str, *, caller_id: str, status: Status
) -> Participant:
    target = daemon.registry.resolve(participant_id)
    authorize_participant_mutation(target, caller_id)
    await presence_access.require_absent(daemon, target.id)
    daemon.registry.set_status(target.id, status)
    return daemon.registry.get(target.id)


def persist_participant_status(daemon, participant_id: str, *, status: Status, unit) -> Participant:
    current = daemon.registry.get(participant_id)
    updated = replace(current, status=status, last_activity=now())
    daemon.registry.persist_in_connection(updated, unit.connection)
    daemon.store.journal.append_group(
        unit,
        [
            participant_event(
                daemon.store,
                updated,
                unit.connection,
                revision=next_revision(daemon.store, unit.connection),
                recorded_at=updated.last_activity,
            )
        ],
    )
    return updated


def _resume_state(p: Participant, live_peers: list[Participant]) -> str:
    """Derive the resume verdict for one participant without extra DB queries.

    Covers the generic identity and capability gates spawn_session checks before
    plus an opt-in harness preflight. The verdict remains point-in-time:
    external transcript state may change before spawn.

    The gates, in the order spawn_session hits them:

    1. ``live``                  — _resolve_resume_reference refuses if the
                                   named participant is still alive.
    2. ``no_session_id``         — _resolve_resume_reference refuses next when
                                   no harness session id has been recorded.
    3. ``harness_cannot_resume`` — check_resume (called from
                                   _validate_before_create) refuses before any
                                   identity check runs.
    4. ``owned_by_live``         — a live trusted session owner or recovery
                                   successor already claims this predecessor.
    5. ``untrusted``             — _validate_resume_identity then raises
                                   when no trusted dead match exists.
    6. ``harness_resume_rejected`` — an opt-in harness preflight refused.
    7. ``resumable``             — all available current gates passed.

    ``live_peers`` must be the set of currently live participants so that the
    owned_by_live check can find peers sharing a session id or predecessor id.
    """
    if p.status is not Status.DEAD:
        return "live"
    if any(other.resumed_from_id == p.id for other in live_peers):
        return "owned_by_live"
    if not p.session_id:
        return "no_session_id"
    harness = HARNESSES.get(normalize(p.harness))
    if harness is None or not supports_resume(harness):
        return "harness_cannot_resume"
    for other in live_peers:
        if (
            normalize(other.harness) == normalize(p.harness)
            and other.session_id == p.session_id
            and is_trusted_provenance(other.session_correlation)
        ):
            return "owned_by_live"
    if not is_trusted_provenance(p.session_correlation):
        return "untrusted"
    try:
        harness.resume_preflight(predecessor=p)
    except BadRequest:
        return "harness_resume_rejected"
    return "resumable"


def _pagination(
    daemon,
    params: dict,
    *,
    ids: list[str] | None,
) -> tuple[tuple[float, str] | None, int | None]:
    raw_limit = params.get("limit")
    if raw_limit is not None and (
        isinstance(raw_limit, bool)
        or not isinstance(raw_limit, int)
        or not 1 <= raw_limit <= PARTICIPANTS_LIST_MAX_LIMIT
    ):
        raise BadRequest(
            f"limit must be an integer between 1 and {PARTICIPANTS_LIST_MAX_LIMIT}, or absent"
        )

    after_id = params.get("after_id")
    if after_id is not None and (not isinstance(after_id, str) or not after_id):
        raise BadRequest("after_id must be a non-empty participant id, or absent")

    if ids is not None and (raw_limit is not None or after_id is not None):
        raise BadRequest(
            "limit and after_id cannot be used with ids; request ids without pagination"
        )

    after = None
    if after_id is not None:
        cursor = daemon.store.get_participant(after_id)
        if cursor is None:
            raise BadRequest(
                f"after_id {after_id!r} is no longer retained; "
                "restart pagination from the first page"
            )
        after = (cursor.created_at, cursor.id)

    return after, raw_limit


@method("hello")
async def _hello(daemon, params: dict) -> dict:
    """First contact; terminal identity is established only by provider binding."""
    pane = params.get("pane")
    if pane is not None:
        raise BadRequest(
            "pane-only attachment was removed; use frontend.participants.adopt with exact "
            "provider terminal identity"
        )
    participant = daemon.registry.register(
        harness=params.get("harness") or "unknown",
        pane=None,
        cwd=params.get("cwd"),
        session_id=params.get("session_id"),
        claimed_id=params.get("id"),
    )
    return _with_presence(daemon, participant.to_dict())


@method("participants.list")
async def _list(daemon, params: dict) -> list[dict]:
    include_dead = bool(params.get("include_dead"))

    parent_id = params.get("parent_id")
    if parent_id is not None and (not isinstance(parent_id, str) or not parent_id):
        raise BadRequest("parent_id must be a non-empty participant id, or absent")

    raw_ids = params.get("ids")
    if raw_ids is None:
        ids: list[str] | None = None
    else:
        if not isinstance(raw_ids, list):
            raise BadRequest("ids must be a list of non-empty strings, or absent")
        for item in raw_ids:
            if not isinstance(item, str) or not item:
                raise BadRequest(
                    "ids must be a list of non-empty strings; "
                    "an empty string would widen the query to all rows"
                )
        if len(raw_ids) > PARTICIPANTS_LIST_MAX_LIMIT:
            raise BadRequest(f"ids list is capped at {PARTICIPANTS_LIST_MAX_LIMIT} entries")
        ids = raw_ids

    after, limit = _pagination(daemon, params, ids=ids)

    page = daemon.registry.list(
        include_dead=include_dead,
        ids=ids,
        parent_id=parent_id,
        after=after,
        limit=limit,
    )

    live_peers = daemon.registry.list(include_dead=False) if include_dead else []

    result = []
    for p in page:
        d = _with_presence(daemon, p.to_dict())
        d["resume_state"] = _resume_state(p, live_peers)
        result.append(d)
    return result


@method("participants.recent_dead")
async def _recent_dead(daemon, params: dict) -> list[dict]:
    limit = params.get("limit", 20)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise BadRequest("limit must be an integer between 1 and 20")
    live_peers = daemon.registry.list(include_dead=False)
    live_session_ids = {p.session_id for p in live_peers if p.session_id}
    rows = daemon.store.list_recent_dead(limit=limit, exclude_session_ids=live_session_ids or None)
    ids = [p.id for p in rows]
    prompts = daemon.store.spawn_prompts_for_targets(ids)
    result = []
    for p in rows:
        record = p.to_dict()
        record["resume_state"] = _resume_state(p, live_peers)
        record["spawn_prompt"] = prompts.get(p.id)
        result.append(record)
    return result


@method("participants.tree")
async def _tree(daemon, params: dict) -> list[dict]:
    return _tree_with_presence(daemon, daemon.registry.tree())


@method("participants.get")
async def _get(daemon, params: dict) -> dict:
    return _with_presence(daemon, daemon.registry.resolve(_require(params, "id")).to_dict())


@method("participant.rename")
async def _rename(daemon, params: dict) -> dict:
    pid = _require(params, "id")
    name = _require(params, "name")
    target = daemon.registry.resolve(pid)
    return _with_presence(daemon, daemon.registry.rename(target.id, name).to_dict())


@method("participant.update")
async def _update(daemon, params: dict) -> dict:
    caller_id = _require(params, "caller_id")
    if not isinstance(caller_id, str):
        raise BadRequest("caller_id must be a participant id")
    raw_target = params.get("target")
    if raw_target is not None and (not isinstance(raw_target, str) or not raw_target):
        raise BadRequest("target must be a non-empty participant id or live name, or null")
    name = params.get("name")
    description = params.get("description")
    if name is None and description is None:
        raise BadRequest("participant.update requires at least one of name or description")

    caller = daemon.registry.resolve(caller_id)
    target = daemon.registry.resolve(raw_target if raw_target is not None else caller.id)
    return _with_presence(
        daemon,
        update_participant_metadata(
            daemon,
            target.id,
            caller_id=caller.id,
            name=name,
            description=description,
        ).to_dict(),
    )


@method("participant.status")
async def _status(daemon, params: dict) -> dict:
    pid = _require(params, "id")
    raw = _require(params, "status")
    try:
        status = Status(raw)
    except ValueError:
        raise BadRequest(f"unknown status {raw!r}") from None
    target = await update_participant_status(daemon, pid, caller_id=pid, status=status)
    return _with_presence(daemon, target.to_dict())


async def _require_verified_backend_stop(daemon, pid: str, caller_id: str) -> None:
    """Terminate the verified native backend of ``pid`` before pane/worktree cleanup.

    When the backend's stop cannot be proven (missing identity, adoption
    failure, or termination failure), raise so the caller leaves the worktree
    and runtime binding preserved for the reaper's retries.
    """
    from theater.daemon.spawning.frontend import close_frontend_runtime, is_frontend_binding

    del caller_id
    binding = daemon.store.get_runtime_binding(pid)
    if binding is None:
        return
    if is_frontend_binding(binding):
        try:
            await close_frontend_runtime(daemon, pid)
        except Exception:
            stopped = False
        else:
            stopped = True
    else:
        stopped = await _stop_verified_detached_backend(daemon, pid, binding)
    if not stopped:
        raise TheaterError(
            f"kill of {pid!r}: the backend teardown could not be verified; "
            "the runtime binding and worktree are preserved for the reaper "
            "to retry — inspect the backend process before retrying"
        )


async def _stop_verified_detached_backend(daemon, pid: str, binding) -> bool:
    """Stop one exact detached backend without mutating queued control state."""
    from theater.daemon.harness_runtime.errors import BackendIdentityMismatch

    if binding.backend_pid is None or binding.backend_started_at is None:
        return False
    if daemon.runtime_manager.backend(pid) is None:
        try:
            await daemon.runtime_manager.adopt_backend(
                pid,
                backend_generation=binding.backend_generation,
                pid=binding.backend_pid,
                started_at=binding.backend_started_at,
                endpoint=binding.endpoint,
            )
        except BackendIdentityMismatch:
            hub = getattr(daemon.observer, "live", None)
            if hub is not None:
                hub.unregister(pid)
            return True
        except Exception:
            return False
    try:
        await daemon.runtime_manager.teardown(pid, backend_generation=binding.backend_generation)
    except Exception:
        return False
    hub = getattr(daemon.observer, "live", None)
    if hub is not None:
        hub.unregister(pid)
    return True


class _TerminationUncertain(TheaterError):
    code = "provider_unavailable"

    def __init__(self, participant_id: str) -> None:
        self.details = {"possibly_executed": True, "participant_id": participant_id}
        super().__init__(
            f"termination of {participant_id!r} may have executed but exit was not "
            "verified; workspace usage remains held"
        )


async def _terminate_provider_terminal(
    daemon, participant_id: str, caller_id: str, operation_id: str | None
) -> None:
    result = await daemon.controls.terminate_provider(
        participant_id,
        caller_id=caller_id,
        callback_operation_id=operation_id or f"private-terminate-{new_id()}",
    )
    delivery = result.get("delivery")
    if delivery == "unknown" or (delivery == "accepted" and not result.get("exit_confirmed")):
        raise _TerminationUncertain(participant_id)
    if delivery != "accepted" or result.get("exit_confirmed") is not True:
        error_value = result.get("error")
        detail = (
            error_value.get("message")
            if isinstance(error_value, dict)
            else "provider rejected termination"
        )
        raise TheaterError(str(detail))


def _authorize_termination(target: Participant, caller_id: str) -> None:
    if caller_id == "cli":
        return
    if target.id == caller_id:
        raise NoSelfKill(f"refusing to kill {target.id!r}: that is you, not your child")
    owner_kind = target.control_owner_kind or (
        ControlOwnerKind.PARTICIPANT
        if target.parent_id is not None
        else ControlOwnerKind.LOCAL_OPERATOR
    )
    owner_id = target.control_owner_id or target.parent_id
    if owner_kind is not ControlOwnerKind.PARTICIPANT or owner_id != caller_id:
        raise NotYourChild(
            f"refusing to kill {target.id!r}: its current control owner is "
            f"{owner_id or owner_kind.value!r}, not you ({caller_id!r})"
        )


async def terminate_participant(
    daemon,
    pid: str,
    *,
    caller_id: str,
    operation_id: str | None = None,
) -> dict:
    """Terminate an exact provider/native terminal and retain its workspace."""
    target = daemon.registry.resolve(pid)
    pid = target.id

    _authorize_termination(target, caller_id)
    if target.status is Status.DEAD:
        return {"id": pid, "killed": False, "reason": "already_dead"}

    # Focus protection before any kill side effect; dead targets answer above.
    await presence_access.require_absent(daemon, pid)

    terminal_binding = daemon.store.terminal_bindings.get(pid)
    if terminal_binding is not None:
        await _terminate_provider_terminal(daemon, pid, caller_id, operation_id)

    daemon._explicit_kills.add(pid)
    try:
        # Verify every execution surface before committing participant, job, or usage state.
        try:
            await _require_verified_backend_stop(daemon, pid, caller_id)
        except Exception as exc:
            if operation_id is not None:
                raise _TerminationUncertain(pid) from exc
            raise
        await daemon.controls.cancel_queued_followups(pid)
        daemon.store.bus_append(
            BUS_KIND_PARTICIPANT_KILL_REQUESTED,
            from_id=caller_id,
            to_id=pid,
        )
        # Job completion hashes files before teardown releases workspace usage.
        for job in daemon.store.running_jobs_for_target(pid):
            daemon.jobs.finish(job.handle, state=JobState.KILLED, error_code="killed")
        # Keep the durable binding through every awaited verification/cancellation boundary.
        # From here teardown performs only synchronous registry and usage transitions.
        daemon.store.delete_runtime_binding(pid)
        await daemon.spawner.teardown(target)
    finally:
        daemon._explicit_kills.discard(pid)

    return {"id": pid, "killed": True}


@method("participant.kill")
async def _kill(daemon, params: dict) -> dict:
    return await terminate_participant(
        daemon,
        _require(params, "id"),
        caller_id=params.get("caller_id") or "cli",
    )


@method("adopt")
async def _adopt(daemon, params: dict) -> dict:
    """Reject the retired pane-only adoption shape."""
    del daemon, params
    raise BadRequest(
        "pane-only adoption was removed; use frontend.participants.adopt with an exact "
        "provider generation, terminal incarnation, occupant, process, and trusted identity"
    )


@method("participants.unmanaged")
async def _unmanaged(daemon, params: dict) -> list[dict]:
    """Legacy private shape cannot safely express provider terminal identity."""
    del daemon, params
    return []
