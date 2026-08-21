"""Argparse construction for the theater CLI.

Extracted from the former monolith so the command dispatch and shared seams in
``cli/__init__.py`` stay readable.  Pure construction — no side effects, no
imports of daemon or tmux modules.
"""

from __future__ import annotations

import argparse
import os

from theater import __version__
from theater.harness import APPROVALS


def _add_models_parser(sub) -> None:
    """Register `theater models`."""
    models = sub.add_parser("models", help="Show, or discover, the models a spawn may name.")
    models.add_argument(
        "--discover",
        metavar="HARNESS",
        default=None,
        help=(
            "Ask that CLI what models it can run and print a [models] block "
            "to paste. Not every CLI can be asked."
        ),
    )
    models.add_argument("--json", action="store_true")


def _add_name_parser(sub) -> None:
    """Register small participant-targeting commands."""
    kill = sub.add_parser(
        "kill",
        help="Kill a participant's pane. Use the id for destructive actions.",
    )
    kill.add_argument(
        "id",
        help=(
            "Participant id or name. Names are live-only and recyclable; a "
            "dead participant has no name. Use the id for destructive "
            "targeting — a recycled name can point at a successor."
        ),
    )

    name = sub.add_parser("name", help="Rename a participant.")
    name.add_argument("target", help="Participant id or current name (live participants only).")
    name.add_argument("new_name", help="The new name.")

    candidates = sub.add_parser(
        "candidates",
        help="List transcript candidates for operator recovery.",
    )
    candidates.add_argument("id", help="Stable participant id or live name.")
    candidates.add_argument("--json", action="store_true")

    bind = sub.add_parser(
        "bind",
        help="Bind a participant to a transcript candidate (same-UID operator authority).",
        description=(
            "Bind a participant to a transcript candidate. This is CLI/operator RPC "
            "authority, the same trust boundary as `theater kill`: it is not human "
            "authentication. A same-user process that can run theater or speak the "
            "daemon socket can invoke it. The stable-id confirmation prevents "
            "accidental alias-based binding."
        ),
    )
    bind.add_argument("id", help="Stable participant id or live name.")
    bind.add_argument("candidate", help="Candidate path or session URI from `theater candidates`.")
    bind.add_argument(
        "--confirm-id",
        required=True,
        help="Must exactly equal the stable target participant id.",
    )
    bind.add_argument(
        "--transfer-from",
        default=None,
        help="Current owner stable participant id, required to transfer an owned candidate.",
    )
    bind.add_argument(
        "--transfer-confirm-id",
        default=None,
        help="Must exactly equal --transfer-from when transferring ownership.",
    )


def _add_gc_parser(sub) -> None:
    """Register `theater gc`."""
    gc = sub.add_parser("gc", help="Sweep old data from the database now.")
    gc.add_argument(
        "--vacuum",
        action="store_true",
        help=(
            "Rewrite the entire database file to reclaim disk space. "
            "Takes an exclusive lock for the duration — the daemon blocks."
        ),
    )
    gc.add_argument("--json", action="store_true")


def _add_receipt_parser(sub) -> None:
    """Register the hidden hook ingestion commands."""
    receipt = sub.add_parser("transcript-receipt", help=argparse.SUPPRESS)
    receipt.add_argument("--id", required=True)
    receipt.add_argument("--token-file", required=True)

    alias = sub.add_parser("claude-receipt", help=argparse.SUPPRESS)
    alias.add_argument("--id", required=True)
    alias.add_argument("--token-file", required=True)


def _add_process_parsers(sub) -> None:
    """Register daemon-side process entry points."""
    daemon = sub.add_parser("daemon", help="Run the registry daemon in the foreground.")
    daemon.add_argument("--log-level", default=os.environ.get("THEATER_LOG_LEVEL", "INFO"))
    daemon.add_argument(
        "--timing",
        action="store_true",
        default=os.environ.get("THEATER_TIMING", "") not in ("", "0"),
        help=(
            "Log every timed operation, not just the slow ones. The env var "
            "THEATER_TIMING=1 does the same and reaches a daemon that autostarted "
            "itself, which the flag cannot."
        ),
    )

    mcp = sub.add_parser("mcp", help="Run the per-agent MCP server on stdio.")
    mcp.add_argument("--id", dest="participant_id", default=None)
    mcp.add_argument("--harness", default="unknown")


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="theater",
        description="Cross-harness orchestration for coding agents.",
    )
    # `--version` prints and exits during parsing, before argparse checks for a subcommand.
    p.add_argument(
        "--version",
        "-v",
        action="version",
        version=f"theater {__version__}",
    )
    sub = p.add_subparsers(dest="command", required=True)

    _add_process_parsers(sub)
    _add_receipt_parser(sub)

    ls = sub.add_parser("ls", help="List participants.")
    once = ls.add_mutually_exclusive_group()
    once.add_argument("--json", action="store_true")
    once.add_argument("--watch", action="store_true", help="Redraw until interrupted.")
    ls.add_argument(
        "--all",
        action="store_true",
        help=(
            "Include dead participants. Dead rows have no name (shown as '-'); "
            "use the id for historical access (retention-bounded)."
        ),
    )
    ls.add_argument("--tree", action="store_true", help="Show lineage.")
    ls.add_argument("--interval", type=float, default=1.0, help="Seconds per redraw.")

    spawn = sub.add_parser("spawn", help="Start an agent in a new tmux window.")
    # No `choices`: legal harnesses are not in the registry when the parser is built.
    spawn.add_argument("harness", nargs="?", default=None)
    spawn.add_argument("prompt", nargs="?", default="")
    spawn.add_argument(
        "--prompt",
        dest="prompt_flag",
        default=None,
        help="The prompt, when no harness is named and the favourite is used.",
    )
    spawn.add_argument(
        "--approval",
        choices=APPROVALS,
        required=True,
        help="Tool approval policy for the new agent. No default, on purpose.",
    )
    spawn.add_argument("--cwd", default=None)
    spawn.add_argument("--parent", dest="parent_id", default=None)
    spawn.add_argument(
        "--model",
        default=None,
        help=(
            "Model for the new agent, spelled as its harness expects. "
            "Not validated: an unknown name fails in the pane, not here."
        ),
    )
    spawn.add_argument(
        "--reasoning-effort",
        default=None,
        help=(
            "Reasoning effort for the new agent (e.g. low, medium, high). "
            "Not validated: an unknown value fails in the pane, not here."
        ),
    )
    spawn.add_argument(
        "--worktree",
        nargs="?",
        const=True,
        default=False,
        type=str,
        help=(
            "Create a git worktree for the child. Bare --worktree creates an "
            "isolated worktree (unique index and HEAD). --worktree NAME creates "
            "or joins a named shared linked worktree — multiple children with "
            "the same name share one directory and branch. Expert mode: the "
            "index and HEAD are shared, concurrent git add/commit operations "
            "can interfere, and Theater does not enforce file ownership."
        ),
    )
    spawn.add_argument(
        "--base-branch",
        default=None,
        help="Base branch for the worktree. Defaults to current HEAD.",
    )
    spawn.add_argument(
        "--foreground",
        action="store_true",
        help="Switch to the new window instead of leaving it in the background.",
    )
    spawn.add_argument("--json", action="store_true")

    bus = sub.add_parser("bus", help="Show the normalized event feed.")
    bus.add_argument("-f", "--follow", action="store_true")
    bus.add_argument("-n", "--limit", type=int, default=50)
    bus.add_argument(
        "--kind",
        default=None,
        help="Only events whose kind starts with this, e.g. 'agent.' or 'agent.tool'.",
    )
    bus.add_argument("--json", action="store_true", help="One JSON object per line.")
    bus.add_argument("--interval", type=float, default=0.4, help="Seconds per poll.")

    adopt = sub.add_parser(
        "adopt",
        help="Adopt the pane you are running in as a Theater participant.",
    )
    adopt.add_argument(
        "--harness",
        default=None,
        help="Override harness detection. By default the pane's current command is matched.",
    )
    adopt.add_argument("--json", action="store_true")

    harnesses = sub.add_parser("harnesses", help="List the coding CLIs Theater knows how to drive.")
    harnesses.add_argument("--json", action="store_true")

    conf = sub.add_parser("config", help="Show resolved settings and where they came from.")
    conf.add_argument(
        "topic",
        nargs="?",
        choices=["path"],
        default=None,
        help="'path' prints the config file location and nothing else.",
    )
    conf.add_argument("--json", action="store_true")

    _add_models_parser(sub)
    _add_name_parser(sub)
    _add_gc_parser(sub)

    stats = sub.add_parser("stats", help="How turns have been ending, per harness.")
    stats.add_argument(
        "--window",
        type=float,
        default=None,
        metavar="HOURS",
        help=(
            "Only turns started in the last N hours. Default: all retained "
            "history — retention is finite, so older rows may be gone."
        ),
    )
    stats.add_argument("--json", action="store_true")

    sub.add_parser("regie", help="Launch the régie TUI (run inside tmux).")

    sub.add_parser("stop", help="Shut the daemon down.")

    sub.add_parser(
        "restart",
        help="Restart the daemon, applying config changes. Agents keep running.",
    )

    return p
