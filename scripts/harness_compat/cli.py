"""GitHub workflow entry points; qualification has no local CLI or saved result mode."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import git
from .adapters import ADAPTER_NAMES, load_adapter
from .attention import report_workflow_failure
from .canaries import canary_version, run_canaries
from .diagnostics import exception_detail
from .policy import (
    affected_harnesses,
    check_routine_change,
    load_policy,
    policy_relative,
)
from .publish import SWEEP_EVENTS, check_publication_context, file_attention_issue, publish
from .run import Run, discover_releases, run_harness
from .summary import error_annotation, render_run


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.harness_compat")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser(
        "run", help="qualify every newer release; main runs publish issues/PRs"
    )
    run.add_argument("harness", choices=ADAPTER_NAMES)
    verify = commands.add_parser("verify", help="check proposed admissions, bounded by the PR")
    verify.add_argument("harness", choices=ADAPTER_NAMES)
    verify.add_argument("--base", required=True)
    verify.add_argument("--routine", action="store_true")
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


def _run(args: argparse.Namespace, root: Path) -> int:
    publishing = _publishes()
    if publishing:
        check_publication_context(root, os.environ)

    def completed(run: Run) -> None:
        published = publish(root, run, run_url=_run_url(os.environ["GITHUB_RUN_ID"]))
        print(f"{args.harness}: shared policy PR {published.pr}", flush=True)

    run = run_harness(root, load_adapter(args.harness), publish=completed if publishing else None)
    print(render_run(run))
    if run.errors:
        print(error_annotation("Harness compatibility", "; ".join(run.errors)))
        return 2
    if len(run.results) != len(run.releases):
        raise RuntimeError("qualification stopped before every newer release had an outcome")
    if any(result.verdict in {"inconclusive", "tooling"} for result in run.results):
        return 2
    return int(any(result.verdict == "red" for result in run.results))


def _verify(args: argparse.Namespace, root: Path) -> int:
    from theater.version_policy import VersionPolicy

    policy = load_policy(root, args.harness)
    relative = policy_relative(args.harness).as_posix()
    added = git.git(
        root, "diff", "--name-only", "--diff-filter=A", args.base, "HEAD", "--", relative
    )
    base = policy if added else VersionPolicy.from_text(git.show(root, args.base, relative))
    if args.routine and check_routine_change(root, args.base) != args.harness:
        raise ValueError("the routine PR selected a different harness")
    adapter = load_adapter(args.harness)
    releases = discover_releases(adapter, policy, base=base, routine=args.routine)
    run = run_harness(root, adapter, releases=releases)
    print(render_run(run))
    if run.errors or len(run.results) != len(releases):
        return 2
    if any(result.verdict in {"inconclusive", "tooling"} for result in run.results):
        return 2
    # An exclusion is conservative, including when a subsequent check passes.
    return int(
        any(policy.admits(result.version) and result.verdict != "green" for result in run.results)
    )


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
    """0: complete/pass; 1: contract failure; 2: inconclusive or automation failure."""
    args = _parser().parse_args(argv)
    if os.environ.get("GITHUB_ACTIONS") != "true":
        print("harness-compat entry points are reserved for GitHub workflows", file=sys.stderr)
        return 2
    handlers = {
        "run": _run,
        "verify": _verify,
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
        if args.command == "report":
            try:
                file_attention_issue(
                    f"Harness compatibility workflow: {args.run_id}",
                    f"Failure reporting needs human attention for run {args.run_id}.\n\n"
                    f"Run: {_run_url(args.run_id)}\n"
                    f"Reporter: {_run_url(os.environ['GITHUB_RUN_ID'])}\n\n{message}\n",
                )
            except Exception as reporting:
                print(
                    error_annotation("Failure issue publication", str(reporting)), file=sys.stderr
                )
        return 2
