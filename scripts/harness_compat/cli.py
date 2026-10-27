"""GitHub workflow entry points; qualification has no local CLI or saved result mode."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from theater.version_policy import VersionPolicy, version_key

from . import git
from .adapters import ADAPTER_NAMES, load_adapter
from .attention import report_workflow_failure
from .canaries import canary_version, run_canaries
from .diagnostics import exception_detail
from .policy import (
    affected_harnesses,
    check_routine_change,
    check_routine_floor,
    load_policy,
    policy_relative,
)
from .publish import SWEEP_EVENTS, check_publication_context, publish
from .run import Run, discovered_releases, run_harness, select_releases, split_shards
from .summary import error_annotation, render_failures, render_run


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.harness_compat")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser(
        "run", help="qualify every newer release; main runs publish issues/PRs"
    )
    run.add_argument("harness", choices=ADAPTER_NAMES)
    run.add_argument("--budget-minutes", type=float, help="stop starting releases after this long")
    run.add_argument(
        "--max-range",
        type=int,
        default=10,
        help="raise the floor when more releases than this are in range",
    )
    verify = commands.add_parser("verify", help="check proposed admissions, bounded by the PR")
    verify.add_argument("harness", choices=ADAPTER_NAMES)
    verify.add_argument("--base", required=True)
    verify.add_argument("--routine", action="store_true")
    verify.add_argument("--releases", help="comma-separated releases, from the fan-out shards")
    verify.add_argument(
        "--max-range",
        type=int,
        default=10,
        help="the floor a routine PR must carry exactly",
    )
    shards = commands.add_parser("shards", help="emit the PR verify shard matrix for one harness")
    shards.add_argument("harness", choices=ADAPTER_NAMES)
    shards.add_argument("--base", required=True)
    shards.add_argument("--routine", action="store_true")
    plan = commands.add_parser("plan", help="select workflow matrix harnesses")
    plan.add_argument("harness", nargs="?", default="all", choices=("all", *ADAPTER_NAMES))
    plan.add_argument("--base")
    plan.add_argument("--routine", action="store_true")
    canaries = commands.add_parser("canaries", help="workflow self-check of deliberate mutations")
    canaries.add_argument("harness", choices=ADAPTER_NAMES)
    report = commands.add_parser("report", help="report a failed workflow from trusted main")
    report.add_argument("run_id", type=int)
    return parser


def _run_url(run_id: int | str) -> str:
    return (
        f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/"
        f"{os.environ['GITHUB_REPOSITORY']}/actions/runs/{run_id}"
    )


def _publishes() -> bool:
    return (
        os.environ.get("GITHUB_REF") == "refs/heads/main"
        and os.environ.get("GITHUB_EVENT_NAME") in SWEEP_EVENTS
    )


def _show(run: Run) -> None:
    """The table, then every failure the run had: the cause belongs where a reader stops."""
    print(render_run(run))
    if failures := render_failures(run):
        print(failures)


def _run(args: argparse.Namespace, root: Path) -> int:
    if args.max_range < 1:
        raise ValueError("--max-range must hold at least one release")
    publishing = _publishes()
    if publishing:
        check_publication_context(root, os.environ)

    def completed(run: Run) -> None:
        published = publish(
            root,
            run,
            run_url=_run_url(os.environ["GITHUB_RUN_ID"]),
            max_range=args.max_range,
        )
        print(
            f"{args.harness}: shared ceiling PR #{published.pr}"
            if published.pr is not None
            else f"{args.harness}: outcome published; no ceiling PR",
            flush=True,
        )

    run = run_harness(
        root,
        load_adapter(args.harness),
        publish=completed if publishing else None,
        budget_seconds=args.budget_minutes * 60 if args.budget_minutes is not None else None,
    )
    _show(run)
    if run.errors:
        print(error_annotation("Harness compatibility", "; ".join(run.errors)))
        return 2
    if len(run.results) != len(run.releases):
        raise RuntimeError("qualification stopped before every newer release had an outcome")
    if any(result.verdict in {"inconclusive", "tooling"} for result in run.results):
        return 2
    return 0


def _pr_window(
    root: Path, harness: str, base: str, routine: bool
) -> tuple[VersionPolicy, VersionPolicy, list[str], list[str]]:
    """The PR's policy, the base policy, the verify window and the full discovery."""
    policy = load_policy(root, harness)
    relative = policy_relative(harness).as_posix()
    added = git.git(root, "diff", "--name-only", "--diff-filter=A", base, "HEAD", "--", relative)
    base_policy = policy if added else VersionPolicy.from_text(git.show(root, base, relative))
    if routine and check_routine_change(root, base) != harness:
        raise ValueError("the routine PR selected a different harness")
    found = discovered_releases(load_adapter(harness))
    window = select_releases(found, policy, name=harness, base=base_policy, routine=routine)
    return policy, base_policy, window, found


def _verify(args: argparse.Namespace, root: Path) -> int:
    if args.max_range < 1:
        raise ValueError("--max-range must hold at least one release")
    policy, base_policy, window, found = _pr_window(root, args.harness, args.base, args.routine)
    if args.routine:
        check_routine_floor(policy, base_policy, found, args.max_range)
    releases = (
        [version for version in args.releases.split(",") if version] if args.releases else window
    )
    outside = sorted(set(releases) - set(window), key=version_key)
    if outside:
        raise ValueError(f"the shard releases {', '.join(outside)} are outside the PR window")
    run = run_harness(root, load_adapter(args.harness), releases=releases)
    _show(run)
    if run.errors or len(run.results) != len(releases):
        return 2
    if any(result.verdict in {"inconclusive", "tooling"} for result in run.results):
        return 2
    # An exclusion is conservative, including when a subsequent check passes.
    return int(
        any(policy.admits(result.version) and result.verdict != "green" for result in run.results)
    )


def _shard_range(shard: list[str]) -> str:
    """The shard's version span, used as its job name."""
    return shard[0] if len(shard) == 1 else f"{shard[0]}-{shard[-1]}"


def _shards(args: argparse.Namespace, root: Path) -> int:
    _, _, releases, _ = _pr_window(root, args.harness, args.base, args.routine)
    include = [
        {"range": _shard_range(shard), "versions": ",".join(shard)}
        for shard in split_shards(releases)
    ]
    print(json.dumps({"include": include}, separators=(",", ":")))
    return 0


def _plan(args: argparse.Namespace, root: Path) -> int:
    if args.base:
        if args.routine:
            check_routine_change(root, args.base)
        names = affected_harnesses(root, args.base)
    else:
        if args.routine:
            raise ValueError("--routine needs --base")
        names = list(ADAPTER_NAMES) if args.harness == "all" else [args.harness]
    print(json.dumps(names))
    return 0


def _canaries(args: argparse.Namespace, root: Path) -> int:
    adapter = load_adapter(args.harness)
    version = canary_version(root, adapter)
    print(f"canaries {args.harness} {version}", flush=True)
    check = run_canaries(root, adapter, version=version)
    print(f"{check.status:5} {check.name}: {check.detail}")
    return {"pass": 0, "fail": 1}.get(check.status, 2)


def _report(args: argparse.Namespace, root: Path) -> int:
    report_workflow_failure(root, args.run_id)
    return 0


def main(argv: list[str] | None = None) -> int:
    """0: complete/pass; 1: rejected admission; 2: inconclusive or automation failure."""
    args = _parser().parse_args(argv)
    if os.environ.get("GITHUB_ACTIONS") != "true":
        print("harness-compat entry points are reserved for GitHub workflows", file=sys.stderr)
        return 2
    handlers = {
        "run": _run,
        "verify": _verify,
        "shards": _shards,
        "plan": _plan,
        "canaries": _canaries,
        "report": _report,
    }
    try:
        return handlers[args.command](args, Path.cwd())
    except (Exception, KeyboardInterrupt) as exc:
        message = exception_detail(exc)
        print(f"harness-compat: {message}", file=sys.stderr)
        print(error_annotation("Harness compatibility", message))
        return 2
