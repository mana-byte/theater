"""Replay historical failed kills through the presence wait and the forget path (WP-B45).

1. Reads every failed participants.terminate operation (read-only theater.db) and buckets it by
   the presence reason the refusal named.
2. Transient provider reasons: runs the REAL PresenceMonitor.require_absent N times against a fake
   provider whose generation flips back after a latency drawn from U(0, 8) s (time scaled down).
3. no-terminal-presence-evidence: checks the ghost preconditions of each target in the database.
4. Demo: the REAL forget_refusal on an unbound dead-pid orphan, a bound one, a live pid, and an
   uncertain spawn (in-memory SQLite).
"Before" is measured by running this file against the base commit, where require_absent does one
refresh and no forget path exists:
  uv run --project /tmp/base-wb45 python scripts/bench/kill_replay.py
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
import random
import re
import sqlite3
import subprocess
import time
from collections import Counter
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

DB = Path.home() / ".theater/var/state/theater.db"
TRANSIENT = (
    "provider-generation-stale",
    "provider-presence-invalidated",
    "provider-presence-changed-during-inspect",
)
BOUND_S = 5.0
MAX_FLIP_S = 8.0
SCALE = 0.2  # wall seconds per simulated second: the real loop runs, just faster


def _reason(error: dict) -> str:
    match = re.search(r"\(([^)]+)\); not mutating", error.get("message", ""))
    return match.group(1) if match else f"{error.get('code')}: {error.get('message', '')[:60]}"


def history(db: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = conn.execute(
        "select target_ids, error from public_operations "
        "where kind='participants.terminate' and state='failed'"
    ).fetchall()
    return [(json.loads(t)[0], _reason(json.loads(e))) for t, e in rows]


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def ghost_verdict(db: Path, participant_id: str) -> str:
    """forgettable / operator-only / not-forgettable under the new preconditions."""
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    if conn.execute(
        "select 1 from terminal_bindings where participant_id=?", (participant_id,)
    ).fetchone():
        return "not-forgettable (bound)"
    pid = conn.execute("select pid from participants where id=?", (participant_id,)).fetchone()
    if pid and _pid_alive(pid[0]):
        return "not-forgettable (live pid)"
    phase = conn.execute(
        "select phase from launch_reservations where participant_id=?", (participant_id,)
    ).fetchone()
    states = {
        row[0]
        for row in conn.execute(
            "select state from public_operations, json_each(public_operations.target_ids) "
            "where kind='spawn' and json_each.value=?",
            (participant_id,),
        )
    }
    if states & {"accepted", "running"}:
        return "not-forgettable (spawn in flight)"
    if (phase and phase[0] not in {"terminal_bound", "rolled_back"}) or "uncertain" in states:
        return "operator-only (uncertain spawn)"
    return "forgettable"


# ---- real monitor against a flipping provider ----------------------------------------


class _Connections:
    def __init__(self, flip_at: float) -> None:
        self.flip_at = flip_at

    def _current(self) -> bool:
        return time.monotonic() >= self.flip_at

    def is_current(self, provider_id: str, generation: int) -> bool:
        return self._current()

    def current_generation(self, provider_id: str) -> int | None:
        return 7 if self._current() else None

    def health(self, provider_id: str) -> str:
        return "online"


def _binding():
    from theater.models import TerminalBindingRecord

    return TerminalBindingRecord(
        participant_id="p",
        provider_id="prov",
        provider_generation=7,
        terminal_id="t",
        terminal_incarnation="i",
        occupant_evidence={"occupant_id": "o"},
        process_facts={"pid": 42, "started_at": 1.0, "executable": "/bin/agent"},
        health="healthy",
        report_revision=0,
        created_at=1.0,
        updated_at=1.0,
    )


async def _trial(latency_s: float) -> float | None:
    """Wall seconds until admission, or None when require_absent refused."""
    from theater.daemon.presence import PresenceMonitor
    from theater.models import HumanPresent, Participant

    bindings = SimpleNamespace(value=_binding())
    bindings.get = lambda pid: bindings.value if pid == "p" else None
    participant = Participant(id="p", harness="pi")
    registry = SimpleNamespace(
        store=SimpleNamespace(terminal_bindings=bindings),
        get=lambda pid: participant,
        list=lambda **_: [participant],
    )
    started = time.monotonic()
    connections = _Connections(started + latency_s * SCALE)
    revision = 0

    async def inspect_terminal(provider_id, generation, terminal_id, incarnation, **_):
        nonlocal revision
        revision += 1
        bindings.value = replace(bindings.value, report_revision=revision)
        return {
            "provider_generation": 7,
            "report_revision": revision,
            "terminal": {
                "provider_id": "prov",
                "provider_generation": 7,
                "terminal_id": "t",
                "terminal_incarnation": "i",
                "occupant": {"occupant_id": "o"},
                "process": {"pid": 42, "started_at": 1.0, "executable": "/bin/agent"},
            },
            "presence": {"state": "absent", "revision": revision, "reason": "focus-absent"},
        }

    service = SimpleNamespace(connections=connections, inspect=inspect_terminal)
    kwargs = {"stale_after": 60.0}
    if "transient_wait" in inspect.signature(PresenceMonitor.__init__).parameters:
        kwargs["transient_wait"] = BOUND_S * SCALE
    monitor = PresenceMonitor(registry, **kwargs)
    monitor.configure_terminal_service(service)
    try:
        await monitor.require_absent("p")
    except HumanPresent:
        return None
    finally:
        await monitor.aclose()
    return (time.monotonic() - started) / SCALE


async def simulate(trials: int, seed: int) -> tuple[float, float | None]:
    rng = random.Random(seed)
    latencies = [rng.uniform(0.0, MAX_FLIP_S) for _ in range(trials)]
    results = await asyncio.gather(*(_trial(latency) for latency in latencies))
    admitted = sorted(r for r in results if r is not None)
    median = admitted[len(admitted) // 2] if admitted else None
    return len(admitted) / trials, median


# ---- forget demo ------------------------------------------------------------------


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def forget_demo() -> list[tuple[str, str]]:
    try:
        from theater.daemon.forgetting import forget_refusal
    except ImportError:
        cases = ("unbound dead-pid orphan", "bound participant", "live pid", "uncertain spawn")
        return [(case, "no forget path: kill refuses human_present forever") for case in cases]
    from sqlalchemy import create_engine, insert

    from theater.daemon.schema import launch_reservations, metadata, public_operations
    from theater.models import Participant

    engine = create_engine("sqlite://")
    metadata.create_all(engine, tables=[launch_reservations, public_operations])
    conn = engine.connect()
    bound_ids: set[str] = set()
    store = SimpleNamespace(
        conn=conn,
        terminal_bindings=SimpleNamespace(
            get=lambda pid, connection=None: object() if pid in bound_ids else None
        ),
        get_runtime_binding=lambda pid: None,
    )
    route = SimpleNamespace(route_available=False)
    daemon = SimpleNamespace(store=store, controls=SimpleNamespace(route_for=lambda *_: route))

    def verdict(target: Participant, caller: str) -> str:
        refusal = forget_refusal(daemon, target, caller)
        return "accepted" if refusal is None else f"refused: {str(refusal).split(': ', 1)[1]}"

    orphan = Participant(id="orphan", pid=_dead_pid())
    bound = Participant(id="bound")
    bound_ids.add(bound.id)
    live = Participant(id="live", pid=os.getpid())
    uncertain = Participant(id="uncertain", pid=_dead_pid())
    conn.execute(
        insert(public_operations).values(
            operation_id="op",
            kind="spawn",
            actor_client_id="c",
            target_ids=json.dumps([uncertain.id]),
            state="uncertain",
            phase="terminal_create_outcome_unknown",
            created_at=1.0,
            updated_at=1.0,
        )
    )
    conn.execute(
        insert(launch_reservations).values(
            operation_id="op",
            participant_id=uncertain.id,
            provider_id="prov",
            adapter="vibe",
            phase="terminal_create_dispatched",
            launch_facts="{}",
            artifact_refs="[]",
            created_at=1.0,
            updated_at=1.0,
        )
    )
    return [
        ("unbound dead-pid orphan", verdict(orphan, "agent")),
        ("bound participant", verdict(bound, "agent")),
        ("live pid", verdict(live, "agent")),
        ("uncertain spawn (agent)", verdict(uncertain, "agent")),
        ("uncertain spawn (cli)", verdict(uncertain, "cli")),
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=DB)
    parser.add_argument("--trials", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=45)
    args = parser.parse_args()

    failed = history(args.db)
    reasons = Counter(reason for _, reason in failed)
    rate, median = asyncio.run(simulate(args.trials, args.seed))
    has_forget = forget_demo()[0][1] != "no forget path: kill refuses human_present forever"
    ghosts = Counter(
        ghost_verdict(args.db, target)
        for target, reason in failed
        if reason == "no-terminal-presence-evidence"
    )

    print(f"failed participants.terminate operations: {len(failed)}")
    print(f"{'reason':<46} {'n':>3}  outcome under this build")
    for reason, count in reasons.most_common():
        if reason in TRANSIENT:
            outcome = f"{rate:.1%} of trials admitted (flip U(0,{MAX_FLIP_S:.0f})s)"
        elif reason == "no-terminal-presence-evidence":
            outcome = (
                ", ".join(f"{v} {k}" for k, v in ghosts.most_common())
                if has_forget
                else "refused forever (no forget path)"
            )
        elif reason == "focus_events_unavailable":
            outcome = "refused at once (a human may be there)"
        else:
            outcome = "unchanged (provider-side refusal)"
        print(f"{reason[:46]:<46} {count:>3}  {outcome}")
    transient_n = sum(reasons[r] for r in TRANSIENT)
    forgettable = ghosts["forgettable"] + ghosts["operator-only (uncertain spawn)"]
    print()
    print(
        f"transient trials: N={args.trials}, flip latency U(0,{MAX_FLIP_S:.0f})s, "
        f"admitted {rate:.1%}"
        + (f", median time-to-admit {median:.2f}s" if median is not None else "")
    )
    print(f"expected recovered transient kills: {transient_n * rate:.1f} of {transient_n}")
    print(
        f"no-terminal kills now resolvable by forget: "
        f"{forgettable if has_forget else 0} of {sum(ghosts.values())}"
    )
    print()
    print("forget preconditions demo:")
    for case, verdict in forget_demo():
        print(f"  {case:<26} {verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
