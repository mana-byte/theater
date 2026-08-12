"""RPC method handlers for the daemon.

Each handler is registered via the @method decorator and dispatched by name
from the daemon's connection handler. The handlers are thin: they extract
parameters, call into the registry/jobs/observer, and return dicts.

Heavy logic (spawn, observe, rails) lives in dedicated modules; these
handlers just wire parameters to calls.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable

from theater import protocol
from theater.daemon.harness_detect import detect_harness
from theater.daemon.rails import check_budget, check_cycle, check_depth
from theater.daemon.spawner import SpawnRequest
from theater.harness import HARNESSES, describe, normalize
from theater.models import BadRequest, Busy, HumanPresent, NotAddressable, Status
from theater.tmux import client as tmux
from theater.tmux.presence import human_present

if TYPE_CHECKING:  # circular at runtime: server imports this module for its handlers
    from theater.daemon.server import Daemon

Handler = Callable[["Daemon", dict[str, Any]], Awaitable[Any]]
METHODS: dict[str, Handler] = {}


def method(name: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        METHODS[name] = fn
        return fn

    return register


def _require(params: dict, key: str) -> Any:
    if key not in params or params[key] in (None, ""):
        raise BadRequest(f"missing required parameter {key!r}")
    return params[key]


@method("ping")
async def _ping(daemon, params: dict) -> dict:
    return {"pong": True, "protocol": protocol.PROTOCOL_VERSION}


@method("hello")
async def _hello(daemon, params: dict) -> dict:
    """First contact. Establishes or confirms the caller's identity and tier."""
    participant = daemon.registry.register(
        harness=params.get("harness") or "unknown",
        pane=params.get("pane"),
        cwd=params.get("cwd"),
        session_id=params.get("session_id"),
        claimed_id=params.get("id"),
    )
    return participant.to_dict()


@method("participants.list")
async def _list(daemon, params: dict) -> list[dict]:
    include_dead = bool(params.get("include_dead"))
    return [p.to_dict() for p in daemon.registry.list(include_dead=include_dead)]


@method("participants.tree")
async def _tree(daemon, params: dict) -> list[dict]:
    return daemon.registry.tree()


@method("participants.get")
async def _get(daemon, params: dict) -> dict:
    return daemon.registry.get(_require(params, "id")).to_dict()


@method("participant.status")
async def _status(daemon, params: dict) -> dict:
    pid = _require(params, "id")
    raw = _require(params, "status")
    try:
        status = Status(raw)
    except ValueError:
        raise BadRequest(f"unknown status {raw!r}") from None
    daemon.registry.set_status(pid, status)
    return daemon.registry.get(pid).to_dict()


@method("participant.kill")
async def _kill(daemon, params: dict) -> dict:
    pid = _require(params, "id")
    await daemon.spawner.kill(pid)
    return {"id": pid, "killed": True}


@method("adopt")
async def _adopt(daemon, params: dict) -> dict:
    """Adopt a pane the user is already running a harness in."""
    pane = _require(params, "pane")
    override = params.get("harness")
    cwd = params.get("cwd")
    if not tmux.available():
        raise BadRequest("tmux is not available; cannot look up pane")
    panes = await tmux.list_panes()
    match = next((p for p in panes if p.pane_id == pane), None)
    if match is None:
        raise BadRequest(f"pane {pane!r} not found in tmux")
    harness = normalize(override) if override else detect_harness(
        match.current_command, match.pane_pid
    )
    if cwd is None:
        cwd = match.cwd
    participant = daemon.registry.register(
        harness=harness,
        pane=pane,
        cwd=cwd,
    )
    return participant.to_dict()


@method("participants.unmanaged")
async def _unmanaged(daemon, params: dict) -> list[dict]:
    """Panes running a known harness binary with no participant record."""
    if not tmux.available():
        return []
    panes = await tmux.list_panes()
    registered = {p.tmux_pane for p in daemon.registry.list() if p.tmux_pane}
    out: list[dict] = []
    for p in panes:
        if p.pane_id in registered:
            continue
        harness = detect_harness(p.current_command, p.pane_pid)
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


@method("spawn")
async def _spawn(daemon, params: dict) -> dict:
    req = SpawnRequest(
        harness=_require(params, "harness"),
        prompt=params.get("prompt") or "",
        cwd=_require(params, "cwd"),
        approval=_require(params, "approval"),
        parent_id=params.get("parent_id"),
        tmux_session=params.get("tmux_session"),
        window_name=params.get("window_name"),
        background=params.get("background", True),
        worktree=bool(params.get("worktree", False)),
        base_branch=params.get("base_branch"),
    )
    # Safety rails: reject before creating anything.
    rails = daemon.config.rails
    check_depth(daemon.store, req.parent_id, cap=rails.depth_cap)
    check_budget(daemon.store, req.parent_id, limit=rails.budget)

    participant = await daemon.spawner.spawn(req)
    # Create a job for this spawn so the caller can await the result.
    handle = participant.id  # the handle is the participant id itself.
    daemon.jobs.create(
        handle=handle,
        caller_id=params.get("parent_id") or "cli",
        target_id=participant.id,
        kind="spawn",
        prompt=req.prompt or "",
    )
    result = participant.to_dict()
    result["handle"] = handle
    return result


@method("jobs.await")
async def _jobs_await(daemon, params: dict) -> list[dict]:
    """Wait for one or more jobs to finish, up to max_wait seconds."""
    handles = params.get("handles") or []
    if not handles:
        raise BadRequest("at least one handle is required")
    max_wait = float(params.get("max_wait", 60.0))
    caller_id = params.get("caller_id")
    if caller_id:
        check_cycle(daemon.store, caller_id, handles)
    jobs = await daemon.jobs.await_jobs(handles, max_wait=max_wait)
    return [j.to_dict() for j in jobs]


@method("jobs.status")
async def _jobs_status(daemon, params: dict) -> dict:
    """Get the current state of a single job."""
    handle = _require(params, "handle")
    job = daemon.jobs.get(handle)
    if job is None:
        raise BadRequest(f"no job {handle!r}")
    return job.to_dict()


@method("bus.tail")
async def _bus_tail(daemon, params: dict) -> list[dict]:
    return daemon.store.bus_tail(
        limit=int(params.get("limit", 100)), after_id=int(params.get("after_id", 0))
    )


@method("send")
async def _send(daemon, params: dict) -> dict:
    """Send a prompt to an already-running agent via tmux send-keys."""
    target_id = _require(params, "target")
    prompt = _require(params, "prompt")
    caller_id = params.get("caller_id") or "cli"

    target = daemon.registry.get(target_id)
    if not target.addressable:
        raise NotAddressable(
            f"participant {target_id!r} is not addressable (tier={target.tier})"
        )
    if not target.tmux_pane:
        raise NotAddressable(
            f"participant {target_id!r} has no pane to send to"
        )

    if await human_present(target.tmux_pane):
        raise HumanPresent(
            f"a human is present at {target.tmux_pane}; not injecting"
        )

    running = daemon.store.running_jobs_for_target(target_id)
    busy = [j for j in running if j.kind == "send"]
    if busy:
        raise Busy(
            f"participant {target_id!r} has a running send job"
        )

    await tmux.send_keys(target.tmux_pane, prompt)

    handle = f"{target_id}#{daemon._next_send_seq()}"
    daemon.jobs.create(
        handle=handle,
        caller_id=caller_id,
        target_id=target_id,
        kind="send",
        prompt=prompt,
    )

    daemon.store.bus_append(
        "agent.send",
        from_id=caller_id,
        to_id=target_id,
        payload={"handle": handle, "prompt": prompt[:200]},
    )

    result = daemon.jobs.get(handle)
    assert result is not None
    return result.to_dict()


@method("harnesses")
async def _harnesses(daemon, params: dict) -> list[dict]:
    """What this daemon can actually spawn.

    The registry is importable by anyone, so this looks redundant — but the
    daemon reads its config once at start and never reloads, so a config edit
    leaves the CLI and the régie holding a *newer* harness set than the process
    that has to honour it. Offering a spawn the daemon then refuses is the
    failure this method exists to prevent, and it becomes real the moment the
    set stops being a hardcoded literal.
    """
    return describe()


@method("shutdown")
async def _shutdown(daemon, params: dict) -> dict:
    daemon.stop()
    return {"stopping": True}


#: Kinds `read_transcript` reports. ERROR is dropped: the caller is an agent
#: reading what was said, and a harness-level error record is not part of the
#: conversation.
_READABLE = ("assistant", "user", "tool_call", "tool_result")


@method("read_transcript")
async def _read_transcript(daemon, params: dict) -> dict:
    """Read a participant's session back, with the text unclipped.

    Goes through the harness's `Source`, not through `find_transcript`, so a
    harness whose output is a database answers this as well as one that writes
    a file. The source opened here is short-lived and separate from the
    observer's: reading history must not move the watcher's cursor.
    """
    pid = _require(params, "id")
    last_n = int(params.get("last_n", 5))

    p = daemon.registry.get(pid)
    if p is None:
        raise BadRequest(f"no participant {pid!r}")

    harness_name = normalize(p.harness)
    harness = HARNESSES.get(harness_name)
    if harness is None:
        raise BadRequest(f"cannot read transcript: harness {p.harness!r} is not known")

    source = harness.open_source(cwd=p.cwd, session_id=p.session_id, after=None)
    try:
        history = await source.history(last_n=last_n)
    finally:
        await source.aclose()

    events = [
        {
            "index": event.raw_index,
            "role": str(event.kind),
            "text": event.text or "",
            "tool_name": event.tool_name,
            "turn_end": event.turn_end,
        }
        for event in history.events
        if event.kind.value in _READABLE
    ]
    return {"id": pid, "events": events, "path": history.location}
