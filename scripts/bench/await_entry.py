"""Measure WP-A await changes: inline answer, quiet_s, Claude wait ceiling, unknown-handle hints.

Each measurement calls the *importable* theater code, so running the script under the base
commit's project (``--base PATH``, via ``uv run --project PATH``) measures "before" for real.
Reads ~/.theater's DB read-only and the trajectory extracts; writes nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import statistics
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from types import SimpleNamespace

DB = Path.home() / ".theater/var/state/theater.db"
DATA = Path("/tmp/theater-trajectory-analysis/data")
ENV = "CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS"
#: What a Theater-launched claude child now receives (absent before the change).
LAUNCH_ENV_MS = "360000"
BAD_HANDLES = ["scout", "#7", "7", "ghost"]


def _p95(values: list[int]) -> int:
    return sorted(values)[int(0.95 * (len(values) - 1))] if values else 0


def completed_await_jobs() -> list[dict]:
    """One job row per completed await end, as the daemon serializes it."""
    from theater.models import Job

    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    handles = [
        json.loads(p).get("handle")
        for (p,) in db.execute("select payload from bus where kind='job.await.end'")
        if json.loads(p).get("state") == "completed"
    ]
    rows = {r["handle"]: r for r in db.execute("select * from jobs where state='done'")}
    return [Job.from_row(rows[h]).to_dict() for h in handles if h in rows]


def measure_a1() -> dict:
    from theater.mcp.toolsets.projection import job_entry

    jobs = completed_await_jobs()
    with_result = [job_entry(job) for job in jobs]
    inline = [e for e in with_result if e.get("result")]
    added = [len(json.dumps({"result": e["result"]})) - 2 for e in inline]
    return {
        "completed": len(jobs),
        "inline": len(inline),
        "mean_bytes": round(statistics.mean(added)) if added else 0,
        "p95_bytes": _p95(added),
    }


def _short(name: str) -> str:
    return re.split(r"__|\.", name)[-1]


def next_call_after_done() -> dict[str, Counter]:
    """Per harness: the next Theater call after an await that returned a done entry."""
    done = re.compile(r"""\\?['"]state\\?['"]: ?\\?['"]done""")
    out: dict[str, Counter] = {}
    # full/{pi,vibe}.jsonl hold only wait-lane calls, so their next call is unobservable.
    calls, results = defaultdict(list), {}
    for line in (DATA / "claude_native.jsonl").open():
        d = json.loads(line)
        if d.get("k") == "call" and "theater" in str(d.get("name")):
            calls[d["pid"]].append((int(d["i"]), _short(d["name"]), d["id"]))
        elif d.get("k") == "result":
            results[d["id"]] = str(d.get("text"))
    out["claude"] = _follow(calls, lambda cid: bool(done.search(results.get(cid, ""))))
    scripts = defaultdict(list)
    for line in (DATA / "codex_native.jsonl").open():
        d = json.loads(line)
        if d.get("t") == "script" and d["calls"]:
            scripts[d["pid"]].append(d)
    codex: Counter = Counter()
    for runs in scripts.values():
        runs.sort(key=lambda s: s["seq"])
        flat = [(s, c["tool"]) for s in runs for c in s["calls"]]
        for i, (script, tool) in enumerate(flat):
            if tool == "await_sessions" and "job_terminal" in script["await_reasons"]:
                codex[_short(flat[i + 1][1]) if i + 1 < len(flat) else "(none)"] += 1
    out["codex"] = codex
    return out


def _follow(calls, is_done) -> Counter:
    nxt: Counter = Counter()
    for seq in calls.values():
        seq.sort()
        for i, (_, name, cid) in enumerate(seq):
            if name == "await_sessions" and is_done(cid):
                nxt[seq[i + 1][1] if i + 1 < len(seq) else "(none)"] += 1
    return nxt


def measure_a3() -> dict:
    try:
        from theater.harness.registry.mcp import mcp_foreground_ceiling as await_ceiling
    except ImportError:
        try:
            from theater.mcp.server import await_ceiling
        except ImportError:
            await_ceiling = None
    else:
        from theater.harness.registry.install import install_configured

        install_configured()
    ceilings = {}
    for value in (None, "120000", LAUNCH_ENV_MS, "0"):
        if value is None:
            os.environ.pop(ENV, None)
        else:
            os.environ[ENV] = value
        ceilings[value or "unset"] = await_ceiling("claude") if await_ceiling else None
    os.environ.pop(ENV, None)
    waits = []
    for line in (DATA / "claude_native.jsonl").open():
        d = json.loads(line)
        if d.get("k") == "call" and d.get("name", "").endswith("await_sessions"):
            waits.append(float(json.loads(d["input"]).get("max_wait", 150)))
    # Root claude (unset env, 120 s threshold): the effective wait is clamped to the ceiling.
    root = ceilings["unset"]
    effective = [min(w, root) if root is not None else w for w in waits]
    backgrounded = sum(1 for w in effective if w > 120)
    return {"ceilings": ceilings, "awaits": len(waits), "over_threshold": backgrounded}


def measure_a5() -> str:
    from theater.daemon.rpc.jobs import _jobs_await
    from theater.models import BadRequest, NotFound, Status

    live = SimpleNamespace(id="abc123", status=Status.WORKING)
    job = SimpleNamespace(handle="abc123#7", target_id="abc123")

    def resolve(token):
        if token in ("scout", "abc123"):
            return live
        raise NotFound(token)

    daemon = SimpleNamespace(
        registry=SimpleNamespace(resolve=resolve),
        jobs=SimpleNamespace(get=lambda h: job if h == job.handle else None),
        store=SimpleNamespace(
            get_participant=lambda pid: None,
            oldest_running_job_for_target=lambda pid: job,
            list_participants=lambda include_dead=False: [live],
        ),
    )
    try:
        asyncio.run(_jobs_await(daemon, {"handles": BAD_HANDLES, "max_wait": 0}))
    except BadRequest as exc:
        return str(exc)
    return "(no error)"


def measure_a2() -> object:
    from theater.daemon.awaiting import AwaitTarget
    from theater.daemon.observation.reducer import QuietClock
    from theater.daemon.observation.service import Observer
    from theater.daemon.rpc import jobs as rpc_jobs
    from theater.models import Job, JobState

    job = Job(
        handle="abc123#7", caller_id="me", target_id="abc123", kind="send", prompt=None,
        state=JobState.RUNNING, result=None, error_code=None, created_at=0.0, finished_at=None,
    )  # fmt: skip
    host = SimpleNamespace(
        _watch_contexts={"abc123": SimpleNamespace(clock=QuietClock(quiet_since=100.0))},
        _monotonic=lambda: 142.5,
    )
    observer = SimpleNamespace(
        quiet_seconds=lambda pid: (
            Observer.quiet_seconds(host, pid)  # type: ignore[arg-type]
            if hasattr(Observer, "quiet_seconds")
            else None
        )
    )
    daemon = SimpleNamespace(
        observer=observer, presence=None, store=SimpleNamespace(get_participant=lambda pid: None)
    )
    entry = rpc_jobs._entry(daemon, AwaitTarget(handle=job.handle, target_id="abc123", job=job), {})
    return entry.get("quiet_s", "absent")


def measure() -> dict:
    return {"a1": measure_a1(), "a2": measure_a2(), "a3": measure_a3(), "a5": measure_a5()}


def _row(item: str, metric: str, before: object, after: object) -> None:
    print(f"{item:4} | {metric:64} | {before!s:>24} | {after!s:>24}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print this tree's raw measurements")
    parser.add_argument("--base", help="worktree of the base commit to measure 'before' in")
    args = parser.parse_args()
    after = measure()
    if args.json:
        print(json.dumps(after))
        return
    if args.base:
        raw = subprocess.run(
            ["uv", "run", "--project", args.base, "python", __file__, "--json"],
            check=True, capture_output=True, text=True, cwd=args.base,
        ).stdout  # fmt: skip
        before = json.loads(raw)
    else:
        before = after
    b1, a1 = before["a1"], after["a1"]
    _row("A1", "completed awaits (done jobs, read-only DB)", b1["completed"], a1["completed"])
    share = f"{a1['inline']}/{a1['completed']} ({a1['inline'] / max(a1['completed'], 1):.0%})"
    _row(
        "A1", "entries carrying the final answer inline", f"{b1['inline']}/{b1['completed']}", share
    )
    _row(
        "A1",
        "inline result bytes per entry, mean / p95",
        f"{b1['mean_bytes']} / {b1['p95_bytes']}",
        f"{a1['mean_bytes']} / {a1['p95_bytes']}",
    )
    for harness, nxt in next_call_after_done().items():
        total = sum(nxt.values())
        reads = nxt.get("read_transcript", 0)
        avoidable = round(reads * a1["inline"] / max(a1["completed"], 1))
        _row(
            "A1",
            f"{harness}: next call read_transcript after done await",
            f"{reads}/{total}",
            f"{reads - avoidable}/{total} ({avoidable} avoidable, max)",
        )
    _row(
        "A2", "quiet_s on a running entry quiet since t=100, now t=142.5", before["a2"], after["a2"]
    )
    for env, ceiling in after["a3"]["ceilings"].items():
        _row("A3", f"claude await ceiling, {ENV}={env}", before["a3"]["ceilings"][env], ceiling)
    awaits = after["a3"]["awaits"]
    _row(
        "A3",
        "root-claude awaits with effective wait > 120 s threshold",
        f"{before['a3']['over_threshold']}/{awaits}",
        f"{after['a3']['over_threshold']}/{awaits}",
    )
    print(f"\nA5 handles {BAD_HANDLES}\n  before: {before['a5']}\n  after:  {after['a5']}")


if __name__ == "__main__":
    sys.exit(main())
