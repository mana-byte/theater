"""Introspection commands: harnesses, config, models, stats."""

from __future__ import annotations

import asyncio
import json
import sys
import time

from theater import config, paths
from theater import harness as harness_registry
from theater.cli.errors import BadUsage
from theater.cli.render import _format_floor, _models_block
from theater.client import DaemonClient, call_sync
from theater.formatting import clip_harness, pad_to_width, tilde
from theater.harness import HARNESSES, describe
from theater.protocol import RemoteError


def _harness_rows() -> tuple[list[dict], str | None]:
    """The harness list, and why it is not the daemon's answer if it is not.

    The daemon is authoritative — it is the process that will refuse a spawn,
    and it holds the config as of its own start. But it is asked with autostart
    off: `theater harnesses` answers "what can I pass to spawn", which must
    work before anything else does and must not be the thing that launches a
    daemon. With none running, the local registry is the same answer anyway,
    because the daemon would read the same config when it starts.
    """

    async def go():
        async with DaemonClient(autostart=False) as client:
            return await client.call("harnesses")

    try:
        rows = asyncio.run(go())
    except (FileNotFoundError, ConnectionRefusedError, ConnectionError, OSError):
        return describe(), "no daemon running"
    except RemoteError as exc:
        # A daemon older than this CLI has no such method.
        if exc.code != "unknown_method":
            raise
        return describe(), "the running daemon predates this command"
    assert isinstance(rows, list)
    return rows, None


def cmd_harnesses(args) -> int:
    """List the harness adapters, as the daemon sees them."""
    rows, fallback = _harness_rows()
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    print(f"{pad_to_width('', 2)} {'NAME':<10} {'SOURCE':<8} {'BINARY':<10} {'INSTALLED':<10} PATH")
    for r in rows:
        # A plugin that would not load has no binary to look for, so say broken.
        mark = "broken" if r.get("error") else ("yes" if r["installed"] else "no")
        print(
            f"{pad_to_width(r['icon'], 2)} {r['name']:<10} {r.get('source', '-'):<8} "
            f"{r['binary'] or '-':<10} {mark:<10} "
            f"{tilde(r['path']) if r['path'] else '-'}"
        )
    missing = [r["name"] for r in rows if not r["installed"] and not r.get("error")]
    if missing:
        print(f"\nnot on PATH: {', '.join(missing)} — spawn will refuse these")
    for r in rows:
        if r.get("error"):
            print(f"\n{r['name']}: {r['error']}")
    if fallback:
        # Worth saying which list this is: a daemon already up may hold a different one.
        print(f"\n{fallback} — read from this process's registry")
    return 0


def cmd_config(args) -> int:
    """Show the resolved settings, each tagged with where it came from.

    Prints the *resolved* view rather than the file, because the question this
    answers is "did my edit take effect", which the file cannot answer. Like
    `harnesses`, it reads local data and never contacts the daemon — a config
    error is exactly the thing that stops the daemon from starting, so this has
    to work when nothing else does.

    A malformed file is reported here as the daemon would reject it, so the
    same message is available before the first confusing start-up failure.
    """
    if args.topic == "path":
        print(paths.config_path())
        return 0

    try:
        loaded = config.load()
    except config.ConfigError as exc:
        print(f"theater: {exc}", file=sys.stderr)
        return 1

    rows = config.describe(loaded)
    if args.json:
        print(
            json.dumps(
                {
                    "path": str(loaded.path),
                    "exists": loaded.exists,
                    "settings": [
                        {"key": key, "value": value, "source": source}
                        for key, value, source in rows
                    ],
                },
                indent=2,
            )
        )
        return 0

    where = tilde(str(loaded.path))
    print(f"{where}{'' if loaded.exists else '  (no file yet — all defaults)'}\n")
    width = max(len(key) for key, _, _ in rows)
    for key, value, source in rows:
        mark = "" if source == "default" else "  <- config.toml"
        print(f"{key:<{width}}  {value}{mark}")
    if loaded.exists:
        print("\nchanges apply on `theater restart`")
    return 0


def cmd_models(args) -> int:
    """Show, or discover, the models a spawn may name for each harness.

    Two jobs, because they are two halves of one task. Bare, it answers "what
    will `--model` accept", which is a config question. With `--discover`, it
    asks the CLI itself what it can run and prints that as a config block —
    the on-ramp, since the allowlist starts empty and an empty list refuses
    every `--model`.

    Discovery is an authoring aid and never a gate: nothing here is consulted
    at spawn time, and the block is a suggestion the human edits down to the
    models they actually want spent on. Like `config`, this reads local data
    and never contacts the daemon — the allowlist is enforced from the file,
    so the file is the honest thing to report.
    """
    loaded = config.load()

    if args.discover:
        name = harness_registry.normalize(args.discover)
        adapter = HARNESSES.get(name)
        if adapter is None:
            known = ", ".join(sorted(HARNESSES))
            raise BadUsage(f"unknown harness {args.discover!r}; known: {known}")
        try:
            found = adapter.discover_models()
        except NotImplementedError as exc:
            # Not a failure of this run: the CLI has no way to be asked.
            print(f"theater: {exc}", file=sys.stderr)
            print(
                f"theater: list them by hand under [{config.MODELS_SECTION}] "
                f"in {tilde(str(loaded.path))}",
                file=sys.stderr,
            )
            return 1
        if not found:
            # Asked and answered: none; usually a provider that is not logged in yet.
            print(
                f"theater: {name} reported no models — it may not be authenticated yet",
                file=sys.stderr,
            )
            return 1
        if args.json:
            print(json.dumps({"harness": name, "models": found}, indent=2))
            return 0
        print(f"# {len(found)} found — paste into {tilde(str(loaded.path))},")
        print("# keeping only the models you want spawns to be able to name")
        print(_models_block(name, found, config.MODELS_SECTION))
        return 0

    if args.json:
        print(json.dumps(loaded.models, indent=2))
        return 0

    # Every known harness, not just the configured ones: the absent entries are the point.
    names = sorted(set(HARNESSES) | set(loaded.models))
    if not names:
        print("no harnesses registered")
        return 0
    width = max(len(name) for name in names)
    print(f"{tilde(str(loaded.path))}\n")
    for name in names:
        allowed = loaded.models_for(name)
        listed = ", ".join(allowed) if allowed else "-  (--model refused)"
        print(f"{name:<{width}}  {listed}")
    print("\n`theater models --discover <harness>` prints a block to paste")
    return 0


def _print_coverage(data: dict, args) -> None:
    """Show how far back the data actually goes."""
    coverage = data.get("coverage") or {}
    jobs_from = coverage.get("jobs_from")
    bus_from = coverage.get("bus_from")

    print()
    print(f"coverage: jobs from {_format_floor(jobs_from)}")
    print(f"          bus from {_format_floor(bus_from)}")

    if args.window is None:
        return
    requested = time.time() - float(args.window) * 3600.0
    asked = time.strftime("%Y-%m-%d %H:%M", time.localtime(requested))
    for label, floor in (("jobs", jobs_from), ("bus", bus_from)):
        if floor is not None and requested < floor:
            print(f"          asked back to {asked} — {label} data starts later than the window")


def cmd_stats(args) -> int:
    """How turns have been ending, per harness.

    The number that matters is RESCUED: a turn the observer never saw end, so
    the daemon waited out the rescue timer and handed the caller the last thing
    the agent was heard to say. The caller cannot tell that apart from a real
    answer — it reads as a slightly odd reply, or a slow one — so without this
    command a harness whose transcript format has drifted degrades invisibly.

    A high rate for one harness is a parser problem. A high rate everywhere is
    a problem with how turn ends are matched to jobs.
    """
    data = call_sync("stats", window=args.window)
    assert isinstance(data, dict)
    if args.json:
        print(json.dumps(data, indent=2))
        return 0

    rows = data.get("harnesses") or []
    if not rows:
        print("no turns recorded yet")
        _print_coverage(data, args)
        return 0

    print(
        f"{'HARNESS':<12} {'TURNS':>6} {'CLEAN':>6} {'RESCUED':>8} "
        f"{'FAILED':>7} {'RUNNING':>8}  RESCUE RATE"
    )
    for r in rows:
        finished = (r["clean"] or 0) + (r["rescued"] or 0)
        # Against finished turns, not all: one still running is not yet evidence either way.
        rate = f"{100 * r['rescued'] / finished:.0f}%" if finished else "-"
        print(
            f"{clip_harness(r['harness'], 12):<12} {r['turns']:>6} {r['clean']:>6} "
            f"{r['rescued']:>8} {r['failed']:>7} {r['running']:>8}  {rate:>11}"
        )

    refusals = data.get("refusals") or {}
    if refusals:
        listed = ", ".join(f"{k} {v}" for k, v in sorted(refusals.items()))
        print(f"\nrefused before delivery: {listed}")
    _print_coverage(data, args)
    return 0
