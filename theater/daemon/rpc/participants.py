"""Participant RPC handlers: hello, list, get, tree, status, rename, kill, adopt, unmanaged.

Also owns ``_resume_state``, the generic resume pre-flight verdict used by
``participants.list`` and ``participants.recent_dead``.
"""

from __future__ import annotations

from theater import proc
from theater.constants.daemon import (
    BUS_KIND_PARTICIPANT_KILL_REQUESTED,
    PARTICIPANTS_LIST_MAX_LIMIT,
)
from theater.daemon import workers
from theater.daemon.harness_detect import detect_harness, detect_harness_async, match_binary
from theater.daemon.rpc.params import _require
from theater.daemon.rpc.router import method
from theater.daemon.runtime.tmux_reconcile import reconcile_tmux_inventory_locked
from theater.harness import HARNESSES, normalize, supports_resume
from theater.models import (
    BadRequest,
    JobState,
    NoSelfKill,
    NotYourChild,
    Participant,
    Status,
)
from theater.provenance import is_trusted_provenance
from theater.tmux import client as tmux


async def _verified_pane_locked(daemon, pane: str, *, context: str) -> tuple[tmux.Pane, str]:
    reconciliation = await reconcile_tmux_inventory_locked(daemon, context=context)
    identity = reconciliation.identity_for_pane(pane)
    if identity is None:
        raise BadRequest(f"cannot verify tmux ownership for pane {pane!r}; retry")
    snapshot = await tmux.pane_snapshot(pane)
    if snapshot is None or snapshot.server_identity != identity:
        raise BadRequest(f"cannot verify tmux ownership for pane {pane!r}; retry")
    return snapshot.pane, identity


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
    """First contact. Establishes or confirms the caller's identity and tier."""
    pane = params.get("pane")
    if pane is None:
        participant = daemon.registry.register(
            harness=params.get("harness") or "unknown",
            pane=None,
            cwd=params.get("cwd"),
            session_id=params.get("session_id"),
            claimed_id=params.get("id"),
        )
    else:
        if not isinstance(pane, str) or not pane:
            raise BadRequest("pane must be a non-empty tmux pane id, or absent")
        async with daemon._tmux_reconcile_lock:
            info, tmux_server_identity = await _verified_pane_locked(
                daemon,
                pane,
                context="hello",
            )
            participant = daemon.registry.register(
                harness=params.get("harness") or "unknown",
                pane=pane,
                pane_pid=info.pane_pid,
                cwd=params.get("cwd"),
                session_id=params.get("session_id"),
                claimed_id=params.get("id"),
                tmux_server_identity=tmux_server_identity,
            )
    return participant.to_dict()


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
        d = p.to_dict()
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
    return daemon.registry.tree()


@method("participants.get")
async def _get(daemon, params: dict) -> dict:
    return daemon.registry.resolve(_require(params, "id")).to_dict()


@method("participant.rename")
async def _rename(daemon, params: dict) -> dict:
    pid = _require(params, "id")
    name = _require(params, "name")
    return daemon.registry.rename(pid, name).to_dict()


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
    if target.status is Status.DEAD:
        raise BadRequest(f"cannot update participant {target.id!r}: it is dead")
    if caller.id not in (target.id, target.parent_id):
        raise NotYourChild(
            f"refusing to update {target.id!r}: its parent is "
            f"{target.parent_id!r}, not you ({caller.id!r})"
        )
    return daemon.registry.update_metadata(
        target.id,
        name=name,
        description=description,
    ).to_dict()


@method("participant.status")
async def _status(daemon, params: dict) -> dict:
    pid = _require(params, "id")
    raw = _require(params, "status")
    try:
        status = Status(raw)
    except ValueError:
        raise BadRequest(f"unknown status {raw!r}") from None
    target = daemon.registry.resolve(pid)
    daemon.registry.set_status(target.id, status)
    return daemon.registry.get(target.id).to_dict()


@method("participant.kill")
async def _kill(daemon, params: dict) -> dict:
    pid = _require(params, "id")
    caller_id = params.get("caller_id") or "cli"

    target = daemon.registry.resolve(pid)
    pid = target.id

    if caller_id != "cli":
        if target.id == caller_id:
            raise NoSelfKill(f"refusing to kill {pid!r}: that is you, not your child")
        if target.parent_id != caller_id:
            raise NotYourChild(
                f"refusing to kill {pid!r}: its parent is "
                f"{target.parent_id!r}, not you ({caller_id!r})"
            )
    if target.status is Status.DEAD:
        return {"id": pid, "killed": False, "reason": "already_dead"}

    participant = target
    if target.tmux_pane:
        async with daemon._tmux_reconcile_lock:
            reconciliation = await reconcile_tmux_inventory_locked(
                daemon,
                context="kill",
                retire_missing=False,
            )
            refreshed = daemon.store.get_participant(pid)
            if refreshed is None or refreshed.status is Status.DEAD:
                return {"id": pid, "killed": False, "reason": "already_dead"}
            expected_identity = reconciliation.identity_for_pane(refreshed.tmux_pane)
            if expected_identity is None:
                raise BadRequest(
                    f"cannot kill {pid!r}: tmux pane inventory is inconclusive "
                    "or no longer contains it"
                )
            if refreshed.tmux_server_identity != expected_identity:
                raise BadRequest(f"cannot kill {pid!r}: tmux pane ownership is not verified")
            if refreshed.pid is None:
                raise BadRequest(f"cannot kill {pid!r}: tmux pane process is not verified")
            participant = await daemon.spawner.kill_pane(
                pid,
                expected_server_identity=expected_identity,
                expected_pane_pid=refreshed.pid,
            )

    # finish is first-terminal-write-wins. The marker tells the reaper to leave this alone.
    daemon.store.bus_append(
        BUS_KIND_PARTICIPANT_KILL_REQUESTED,
        from_id=caller_id,
        to_id=pid,
    )
    daemon._explicit_kills.add(pid)
    try:
        if not participant.tmux_pane:
            participant = await daemon.spawner.kill_pane(
                pid,
                expected_server_identity=None,
                expected_pane_pid=None,
            )
        # Finish jobs before teardown: job completion hashes files in the worktree.
        for job in daemon.store.running_jobs_for_target(pid):
            daemon.jobs.finish(job.handle, state=JobState.KILLED, error_code="killed")
        await daemon.spawner.teardown(participant)
    finally:
        daemon._explicit_kills.discard(pid)

    return {"id": pid, "killed": True}


@method("adopt")
async def _adopt(daemon, params: dict) -> dict:
    """Adopt a pane the user is already running a harness in."""
    pane = _require(params, "pane")
    override = params.get("harness")
    cwd = params.get("cwd")
    if not tmux.available():
        raise BadRequest("tmux is not available; cannot look up pane")
    async with daemon._tmux_reconcile_lock:
        match, tmux_server_identity = await _verified_pane_locked(
            daemon,
            pane,
            context="adopt",
        )
        harness = (
            normalize(override)
            if override
            else await detect_harness_async(
                match.current_command, match.pane_pid, detector=detect_harness
            )
        )
        if cwd is None:
            cwd = match.cwd
        participant = daemon.registry.register(
            harness=harness,
            pane=pane,
            pane_pid=match.pane_pid,
            cwd=cwd,
            tmux_server_identity=tmux_server_identity,
        )
    return participant.to_dict()


@method("participants.unmanaged")
async def _unmanaged(daemon, params: dict) -> list[dict]:
    """Panes running a known harness binary with no participant record."""
    if not tmux.available():
        return []
    panes = await tmux.list_panes()
    registered = {p.tmux_pane for p in daemon.registry.list() if p.tmux_pane}
    candidates = [p for p in panes if p.pane_id not in registered]

    # Capture the process table once when some candidate needs it.
    needs_walk = any(match_binary(p.current_command, HARNESSES) is None for p in candidates)
    snapshot = (
        await workers.to_thread(proc.ProcessSnapshot.capture, label="unmanaged.capture")
        if needs_walk
        else None
    )

    out: list[dict] = []
    for p in candidates:
        harness = detect_harness(p.current_command, p.pane_pid, snapshot)
        if harness != "unknown":
            out.append(
                {
                    "pane": p.pane_id,
                    "command": p.current_command,
                    "harness": harness,
                    "cwd": p.cwd,
                    "session": p.session,
                    "window_name": p.window_name,
                }
            )
    return out
