"""Cost of redirecting a busy child: tool calls and description bytes, legacy vs send(if_busy).

Run in this tree for "after"; for "before" run this same file with the base tree's interpreter:
`uv run --project <base worktree> python scripts/bench/redirect_cost.py`. The script detects
whether `send` takes `if_busy` and measures the legacy call sequence when it does not.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

PAIR_WINDOW_SECONDS = 60.0
#: Delay before the fake observer finishes the interrupted job; legacy agents poll at this rate.
OBSERVER_DELAY_SECONDS = 0.6
POLL_SECONDS = 0.2


def _surface() -> dict:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from mcp_surface import measure

    result = asyncio.run(measure("vibe"))
    tools = result["servers"]["control"]["tools"]
    return {
        "total": result["total_bytes"],
        "count": result["tool_count"],
        "redirect_tools": {
            name: tools[name]
            for name in ("send", "steer_session", "queue_followup", "interrupt_session")
            if name in tools
        },
    }


def _history(db: Path) -> dict:
    """Historical interrupt -> send pairs to the same participant within the window."""
    if not db.exists():
        return {"pairs": 0, "interrupts": 0, "median_gap_s": None}
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT participant_id, kind, created_at FROM control_operations "
            "WHERE kind IN ('interrupt', 'send') ORDER BY participant_id, created_at"
        ).fetchall()
    finally:
        conn.close()
    interrupts, gaps, pending = 0, [], {}
    for pid, kind, created in rows:
        if kind == "interrupt":
            interrupts += 1
            pending[pid] = created
        elif pid in pending:
            gap = created - pending.pop(pid)
            if gap <= PAIR_WINDOW_SECONDS:
                gaps.append(gap)
    return {
        "pairs": len(gaps),
        "interrupts": interrupts,
        "median_gap_s": round(statistics.median(gaps), 1) if gaps else None,
    }


class _Rig:
    """In-process daemon plus fake native runtimes (tests/rig), all under a temp THEATER_HOME."""

    async def __aenter__(self) -> _Rig:
        from tests._presence_doubles import AbsentPresence
        from theater import paths
        from theater.client import DaemonClient
        from theater.daemon.server import Daemon

        self.home = tempfile.mkdtemp(prefix="thtr-", dir="/tmp")
        os.environ["THEATER_HOME"] = self.home
        paths.ensure_home()
        self.daemon = Daemon(harnesses={})
        await self.daemon.start()
        self.daemon.presence = AbsentPresence()
        self.client = DaemonClient(autostart=False)
        await self.client.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.client.aclose()
        await self.daemon.aclose()
        shutil.rmtree(self.home, ignore_errors=True)

    async def busy_child(self, *, steer: bool):
        from tests.rig.fake_runtime import FakeRuntime, FakeRuntimeIO, FakeRuntimeState
        from theater.daemon.persistence.repositories.runtime_bindings import (
            ParticipantRuntimeBinding,
        )
        from theater.harness.contracts.runtime import (
            CapabilityUnavailableReason,
            RuntimeCapability,
            RuntimeContext,
            RuntimeLifecyclePhase,
            RuntimeWiring,
        )
        from theater.models import Status

        daemon = self.daemon
        parent = daemon.registry.create_spawned(harness="vibe", cwd="/tmp")
        kid = daemon.registry.create_spawned(harness="vibe", cwd="/tmp", parent_id=parent.id)
        state = FakeRuntimeState(participant_id=kid.id, backend_generation=1)
        state.native_session_id = "thread-1"
        if not steer:
            state.unavailable = {RuntimeCapability.STEER: CapabilityUnavailableReason.WIRING_MODE}
        ctx = RuntimeContext(
            participant_id=kid.id, cwd="/tmp", io=FakeRuntimeIO(state), backend_generation=1
        )
        runtime = FakeRuntime(ctx)

        async def create():
            return runtime

        installed = await daemon.runtime_manager.get_or_create(
            kid.id, backend_generation=1, create=create
        )
        daemon.store.upsert_runtime_binding(
            ParticipantRuntimeBinding(
                participant_id=kid.id,
                harness="vibe",
                wiring=RuntimeWiring.NATIVE,
                backend_generation=1,
                lifecycle=RuntimeLifecyclePhase.ACTIVE,
                native_session_id="thread-1",
            )
        )
        daemon.runtime_manager.record_snapshot(kid.id, installed, await installed.snapshot())
        job = await self.client.call("send", target=kid.id, prompt="work", caller_id=parent.id)
        daemon.registry.set_status(kid.id, Status.WORKING)
        return parent, kid, state, job

    async def observe_interrupt(self, state, kid, job) -> None:
        """Stand in for the observer: finish the interrupted job, then report idle."""
        from theater.models import Status

        while not state.interrupted:  # noqa: ASYNC110 - the fake runtime has no event to wait on
            await asyncio.sleep(0.01)
        await asyncio.sleep(OBSERVER_DELAY_SECONDS)
        self.daemon.jobs.finish(job["handle"], state="killed", error_code="interrupted")
        self.daemon.registry.set_status(kid.id, Status.IDLE)


async def _replace(rig: _Rig, new_surface: bool) -> dict:
    parent, kid, state, job = await rig.busy_child(steer=True)
    task = asyncio.create_task(rig.observe_interrupt(state, kid, job))
    started = time.monotonic()
    if new_surface:
        out = await rig.client.call(
            "send", target=kid.id, prompt="new", caller_id=parent.id, if_busy="replace"
        )
        calls, route = 1, out["route"]
    else:
        await rig.client.call("participant.interrupt", target=kid.id, caller_id=parent.id)
        calls = 1
        while True:
            calls += 1  # one list_participants poll
            row = await rig.client.call("participants.get", id=kid.id)
            if row["status"] == "idle":
                break
            await asyncio.sleep(POLL_SECONDS)
        await rig.client.call("send", target=kid.id, prompt="new", caller_id=parent.id)
        calls, route = calls + 1, "interrupt+poll+send"
    await task
    return {"calls": calls, "route": route, "s": time.monotonic() - started}


async def _amend(rig: _Rig, new_surface: bool, *, steer: bool) -> dict:
    parent, kid, _state, _job = await rig.busy_child(steer=steer)
    if new_surface:
        out = await rig.client.call(
            "send", target=kid.id, prompt="amend", caller_id=parent.id, if_busy="amend"
        )
        return {"calls": 1, "route": out["route"]}
    try:
        await rig.client.call(
            "participant.steer", target=kid.id, prompt="amend", caller_id=parent.id
        )
    except Exception:
        await rig.client.call(
            "participant.queue_followup", target=kid.id, prompt="amend", caller_id=parent.id
        )
        return {"calls": 2, "route": "steer_session refused -> queue_followup"}
    return {"calls": 1, "route": "steer_session"}


async def _scenario(new_surface: bool) -> dict:
    """Count the calls each redirect needs against the tree's own daemon."""
    async with _Rig() as rig:
        return {
            "replace": await _replace(rig, new_surface),
            "amend steer=available": await _amend(rig, new_surface, steer=True),
            "amend steer=unavailable": await _amend(rig, new_surface, steer=False),
        }


def theater_root() -> str:
    import theater

    return str(Path(theater.__file__).resolve().parent.parent)


def _terminal_amend_table() -> dict:
    """Per built-in harness: does its terminal declare a steer plan (else amend queues)?"""
    from theater.harness import HARNESSES

    return {
        name: ("steer" if harness.controls.steer is not None else "queued")
        for name, harness in sorted(HARNESSES.items())
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path.home() / ".theater/var/state/theater.db")
    args = parser.parse_args()
    surface = _surface()
    new_surface = "steer_session" not in surface["redirect_tools"]
    history = _history(args.db)
    sys.path.insert(0, theater_root())
    scenario = asyncio.run(_scenario(new_surface))
    label = "after (send if_busy)" if new_surface else "before (legacy tools)"
    print(f"== {label} — theater at {theater_root()}")
    print(f"surface: tools={surface['count']} total={surface['total']} B")
    for name, size in surface["redirect_tools"].items():
        print(f"  {size:6d} B  {name}")
    print(
        f"history: interrupts={history['interrupts']} interrupt->send pairs within "
        f"{PAIR_WINDOW_SECONDS:.0f} s={history['pairs']} median gap={history['median_gap_s']} s"
    )
    per_pair = 1 if new_surface else 2
    print(f"  calls for those pairs (polls excluded): {history['pairs'] * per_pair}")
    for case, row in scenario.items():
        extra = f" wall={row['s']:.2f} s" if "s" in row else ""
        print(f"scenario {case}: calls={row['calls']} route={row['route']}{extra}")
    if new_surface:
        print("terminal amend route per harness:", _terminal_amend_table())


if __name__ == "__main__":
    main()
