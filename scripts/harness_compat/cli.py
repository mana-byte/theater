"""Command line entry point for scheduled and local compatibility runs."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from .adapters import ADAPTER_NAMES, load_adapter
from .publication import prepare_publication
from .runner import run_harness


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.harness_compat")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("discover", "run", "check"):
        action = commands.add_parser(command)
        action.add_argument("--harness", choices=ADAPTER_NAMES)
        if command == "run":
            action.add_argument("--version")
            action.add_argument("--max-candidates", type=int, default=5)
            action.add_argument("--publish", action="store_true")
            action.add_argument("--summary-json", type=Path)
            action.add_argument("--run-url")
    return parser


def _adapters(name: str | None) -> list[Any]:
    if name:
        return [load_adapter(name)]
    loaded = []
    for candidate in ADAPTER_NAMES:
        try:
            loaded.append(load_adapter(candidate))
        except ImportError:
            continue
    return loaded


def _validate_run(args: argparse.Namespace) -> None:
    if args.version and not args.harness:
        raise ValueError("--version requires --harness")
    if args.max_candidates < 1:
        raise ValueError("--max-candidates must be positive")
    if args.publish and os.environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("--publish is reserved for GitHub Actions")
    if args.publish and not args.harness:
        raise ValueError("--publish requires --harness")


def _run_url() -> str:
    server = os.environ.get("GITHUB_SERVER_URL")
    repository = os.environ.get("GITHUB_REPOSITORY")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if server and repository and run_id:
        return f"{server}/{repository}/actions/runs/{run_id}"
    return "local"


def main(argv: list[str] | None = None) -> int:
    """0: all clear; 1: red/tooling; 2: usage or infrastructure error."""
    args = _parser().parse_args(argv)
    root = Path.cwd()
    try:
        adapters = _adapters(args.harness)
        if args.command == "discover":
            versions = {adapter.name: adapter.discover() for adapter in adapters}
            print(json.dumps(versions, sort_keys=True))
            return 0
        if args.command == "check":
            print(json.dumps({adapter.name: adapter.verified(root).kind for adapter in adapters}))
            return 0
        if not adapters:
            print("harness-compat: no harness adapters import successfully", file=sys.stderr)
            return 2
        _validate_run(args)
        summaries = []
        for adapter in adapters:
            branch = f"refs/remotes/origin/chore/compat-{adapter.name}" if args.publish else None
            summaries.append(
                run_harness(
                    adapter,
                    root,
                    version=args.version,
                    max_candidates=args.max_candidates,
                    publish=args.publish,
                    run_url=args.run_url or _run_url(),
                    evidence_branch=branch,
                )
            )
        document = {"harnesses": summaries}
        summary_path = args.summary_json
        if args.publish and summary_path is None:
            summary_path = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())) / (
                f"harness-compat-{adapters[0].name}.json"
            )
        if summary_path:
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(
                json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
        if args.publish and summary_path:
            prepare_publication(summaries[0], summary_path)
        print(json.dumps(document, sort_keys=True))
        if any(summary["inconclusive"] for summary in summaries):
            return 2
        return int(any(summary["issues"] for summary in summaries))
    except Exception as exc:
        print(f"harness-compat: {exc}", file=sys.stderr)
        return 2
