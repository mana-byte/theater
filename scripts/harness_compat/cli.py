"""Command line entry points: local qualification, the full run, and the PR gate checks."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from .adapters import ADAPTER_NAMES, load_adapter
from .canaries import run_canaries
from .contract import Result
from .policy import affected_harnesses, check_routine_change, mismatches
from .publish import check_publication_context, publish
from .qualify import qualify_binary, qualify_release
from .run import run_harness
from .sandbox import make_sandbox
from .suite_runner import installed_version
from .summary import error_annotation, render_run


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.harness_compat")
    commands = parser.add_subparsers(dest="command", required=True)
    qualify = commands.add_parser(
        "qualify", help="install one exact release and run the live suite"
    )
    qualify.add_argument("harness", choices=ADAPTER_NAMES)
    qualify.add_argument("version")
    qualify.epilog = "arguments after a lone -- go to pytest, e.g. qualify vibe 3.0.2 -- -k mcp"
    suite = commands.add_parser("suite", help="run the live suite on an installed CLI")
    suite.add_argument("harness", choices=ADAPTER_NAMES)
    suite.add_argument("--binary", type=Path, help="default: the harness binary on PATH")
    suite.epilog = "arguments after a lone -- go to pytest, e.g. suite vibe -- -k mcp -x"
    run = commands.add_parser("run", help="qualify every stable release from the pinned minimum")
    run.add_argument("harness", choices=ADAPTER_NAMES)
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--publish", action="store_true", help="file issues, prepare the policy PR")
    mode.add_argument("--check", action="store_true", help="fail when the policy disagrees")
    run.add_argument("--run-url")
    gates = commands.add_parser("gates", help="list the harnesses whose plugin a change edits")
    gates.add_argument("--base", required=True, help="the revision the change is based on")
    gates.add_argument("--routine", action="store_true", help="enforce the routine gate-PR shape")
    canaries = commands.add_parser("canaries", help="prove the live suite notices broken plugins")
    canaries.add_argument("harness", choices=ADAPTER_NAMES)
    canaries.add_argument("--version", help="default: the policy's maximum")
    return parser


def _run_url() -> str:
    server = os.environ.get("GITHUB_SERVER_URL")
    repository = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if server and repository and run_id:
        return f"{server}/{repository}/actions/runs/{run_id}"
    return "local"


def _print_result(result: Result) -> int:
    """Checks to stdout; 0 green, 1 red, 2 for anything that is no verdict on the release."""
    for check in result.checks:
        print(f"{check.status:5} {check.name}: {check.detail}")
    print(f"{result.harness} {result.version}: {result.verdict}")
    return {"green": 0, "red": 1}.get(result.verdict, 2)


def _qualify(args: argparse.Namespace, root: Path) -> int:
    return _print_result(
        qualify_release(
            root, load_adapter(args.harness), args.version, pytest_args=args.pytest_args
        )
    )


def _suite(args: argparse.Namespace, root: Path) -> int:
    """Dev entry point: the suite against an installed binary, in a throwaway sandbox."""
    binary = args.binary or shutil.which(args.harness)
    if binary is None:
        raise ValueError(f"no {args.harness!r} binary on PATH; pass --binary")
    binary = Path(binary).resolve()
    version = installed_version(binary)
    with make_sandbox(args.harness) as sb:
        result = qualify_binary(
            root, sb, args.harness, binary, version, pytest_args=args.pytest_args
        )
    return _print_result(result)


def _run(args: argparse.Namespace, root: Path) -> int:
    if args.publish:
        check_publication_context(root, os.environ)
    run = run_harness(root, load_adapter(args.harness))
    found = mismatches(run.policy, run.results)
    summary = render_run(run, found)
    print(summary)
    regressions = [item for item in found if item.kind == "regression"]
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for item in regressions:
            print(
                error_annotation(
                    "Harness compatibility",
                    f"{run.harness} {item.version} is admitted but ended {item.verdict}",
                )
            )
    if args.publish:
        published = publish(root, run, run_url=args.run_url or _run_url())
        print(
            f"{run.harness}: pr {published.pr_action}; issues created "
            f"{len(published.created)}, already filed {len(published.existing)}"
        )
    return 1 if (found if args.check else regressions) else 0


def _gates(args: argparse.Namespace, root: Path) -> int:
    if args.routine:
        check_routine_change(root, args.base)
    print(json.dumps(affected_harnesses(root, args.base)))
    return 0


def _canaries(args: argparse.Namespace, root: Path) -> int:
    check = run_canaries(root, load_adapter(args.harness), version=args.version)
    print(f"{check.status:5} {check.name}: {check.detail}")
    return {"pass": 0, "fail": 1}.get(check.status, 2)


def main(argv: list[str] | None = None) -> int:
    """0: all clear; 1: red or a policy disagreement; 2: no verdict or an error."""
    argv = list(sys.argv[1:] if argv is None else argv)
    pytest_args: list[str] = []
    if "--" in argv:
        split = argv.index("--")
        argv, pytest_args = argv[:split], argv[split + 1 :]
    args = _parser().parse_args(argv)
    args.pytest_args = pytest_args
    root = Path.cwd()
    handlers = {
        "qualify": _qualify,
        "suite": _suite,
        "run": _run,
        "gates": _gates,
        "canaries": _canaries,
    }
    try:
        return handlers[args.command](args, root)
    except Exception as exc:
        print(f"harness-compat: {exc}", file=sys.stderr)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(error_annotation("Harness compatibility", str(exc)))
        return 2
