"""Participant RPC handlers: hello, list, get, tree, status, rename, kill, adopt, unmanaged."""

from __future__ import annotations

from dataclasses import asdict

from theater.constants.daemon import PARTICIPANTS_LIST_MAX_LIMIT
from theater.daemon import workers
from theater.daemon.handoff.gitfacts import pin_git_facts
from theater.daemon.participants import mutations, termination
from theater.daemon.participants.resume import resume_state
from theater.daemon.presence import access as presence_access
from theater.daemon.rpc.params import _require
from theater.daemon.rpc.router import method
from theater.harness.contracts.runtime import RuntimeCapability
from theater.models import BadRequest, Status

_resume_state = resume_state
authorize_participant_mutation = mutations.authorize_participant_mutation
update_participant_metadata = mutations.update_participant_metadata
update_participant_status = mutations.update_participant_status
persist_participant_status = mutations.persist_participant_status
_require_verified_backend_stop = termination._require_verified_backend_stop
_stop_verified_detached_backend = termination._stop_verified_detached_backend
_TerminationUncertain = termination._TerminationUncertain
_terminate_provider_terminal = termination._terminate_provider_terminal
_authorize_termination = termination._authorize_termination
_require_absent_or_forget_hint = termination._require_absent_or_forget_hint
terminate_participant = termination.terminate_participant


def _with_presence(daemon, record: dict, *, self_view: bool = False) -> dict:
    """Attach the cached focus projection to one participant wire record.

    ``self_view`` is the agent reading itself: its play adds spawn options, or the live play an
    unpositioned descendant sits under.
    """
    route = daemon.controls.route_for(record["id"], RuntimeCapability.SEND)
    record["addressable"] = record.get("status") != Status.DEAD.value and route.route_available
    record["human_presence"] = presence_access.presence_snapshot(daemon, record["id"]).to_dict()
    plays = getattr(daemon, "plays", None)
    if plays is not None:
        played = plays.self_view(record["id"]) if self_view else plays.describe(record["id"])
        if played is not None:
            record["play"] = played
    return record


def _tree_with_presence(daemon, nodes: list[dict]) -> list[dict]:
    """Attach presence to returned tree nodes only; no new broad queries."""
    for node in nodes:
        _with_presence(daemon, node)
        _tree_with_presence(daemon, node["children"])
    return nodes


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

    if ids is not None and after_id is not None:
        raise BadRequest(
            "after_id cannot be used with ids; request ids without a cursor "
            "(limit still caps the result)"
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

    order = params.get("order", "oldest")
    if order not in ("oldest", "newest"):
        raise BadRequest(f"order must be 'oldest' or 'newest', got {order!r}")

    after, limit = _pagination(daemon, params, ids=ids)

    page = daemon.registry.list(
        include_dead=include_dead,
        ids=ids,
        parent_id=parent_id,
        after=after,
        limit=limit,
        newest_first=order == "newest",
    )

    live_peers = daemon.registry.list(include_dead=False) if include_dead else []

    result = []
    for p in page:
        d = _with_presence(daemon, p.to_dict())
        d["resume_state"] = resume_state(p, live_peers)
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
        record["resume_state"] = resume_state(p, live_peers)
        record["spawn_prompt"] = prompts.get(p.id)
        result.append(record)
    return result


@method("participants.tree")
async def _tree(daemon, params: dict) -> list[dict]:
    return _tree_with_presence(daemon, daemon.registry.tree())


@method("participants.get")
async def _get(daemon, params: dict) -> dict:
    include_git = params.get("include_git", False)
    if not isinstance(include_git, bool):
        raise BadRequest("include_git must be a boolean")
    participant = daemon.registry.resolve(_require(params, "id"))
    record = _with_presence(
        daemon, participant.to_dict(), self_view=params.get("self_view") is True
    )
    if include_git:
        record["git"] = await _current_git(participant.cwd)
    return record


async def _current_git(cwd: str | None) -> dict:
    """Observe the cwd's Git state now; ``branch`` stays the recorded workspace metadata.

    Never persisted: rewriting a recorded branch would defeat cleanup's moved-branch fence.
    """
    facts = await workers.to_thread(pin_git_facts, cwd, label="participants.git")
    return asdict(facts)


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


@method("participant.kill")
async def _kill(daemon, params: dict) -> dict:
    return await terminate_participant(
        daemon,
        _require(params, "id"),
        caller_id=params.get("caller_id") or "cli",
        forget=bool(params.get("forget")),
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
