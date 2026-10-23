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
from .runner import run_harness_state
from .summary import error_annotation, render_harness, render_overview, workflow_annotations


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scripts.harness_compat")
    commands = parser.add_subparsers(dest="command", required=True)
    summarize = commands.add_parser("summarize")
    summarize.add_argument("directory", type=Path)
    summarize.add_argument(
        "--expect", default="", help="comma-separated harnesses that must report"
    )
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


def _open_failures() -> list[str]:
    """The releases the open issue lists, tolerant of the spaces a human may add."""
    ledger = os.environ.get("HARNESS_COMPAT_OPEN_FAILURES", "")
    return [item.strip() for item in ledger.split(",") if item.strip()]


def exit_code(summaries: list[dict[str, Any]]) -> int:
    """1: a red or tooling row; 2: only exhausted inconclusive rows; 0: nothing to act on."""
    rows = [row for summary in summaries for row in summary["issues"]]
    if any(row["verdict"] in ("red", "tooling") for row in rows):
        return 1
    return 2 if any(summary["inconclusive"] for summary in summaries) else 0


def _append_step_summary(text: str) -> None:
    if target := os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(target).open("a", encoding="utf-8") as stream:
            stream.write(text + "\n")
    else:
        print(text, file=sys.stderr)


def _summarize(args: argparse.Namespace) -> int:
    summaries = [
        summary
        for path in sorted(args.directory.rglob("compat-summary.json"))
        for summary in json.loads(path.read_text(encoding="utf-8"))["harnesses"]
    ]
    table, missing = render_overview(summaries, [n for n in args.expect.split(",") if n])
    _append_step_summary(table)
    return 1 if missing else exit_code(summaries)


def _run(args: argparse.Namespace, root: Path, adapters: list[Any]) -> int:
    _validate_run(args)
    states = [
        run_harness_state(
            adapter,
            root,
            version=args.version,
            max_candidates=args.max_candidates,
            publish=args.publish,
            run_url=args.run_url or _run_url(),
        )
        for adapter in adapters
    ]
    summaries = [state.to_summary() for state in states]
    document = {"harnesses": summaries}
    summary_path = args.summary_json
    if args.publish and summary_path is None:
        summary_path = Path(os.environ.get("RUNNER_TEMP", tempfile.gettempdir())) / (
            f"harness-compat-{adapters[0].name}.json"
        )
    if args.publish and summary_path:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        report = prepare_publication(states[0], summary_path, _open_failures())
        summaries[0]["publication"] = {"pr": report.pr_needed, "issue": report.issue_action}
    if summary_path:
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(
            json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    print(json.dumps(document, sort_keys=True))
    if os.environ.get("GITHUB_ACTIONS") == "true":
        for summary in summaries:
            _append_step_summary(render_harness(summary))
            for command in workflow_annotations(summary):
                print(command)
    return exit_code(summaries)


def main(argv: list[str] | None = None) -> int:
    """0: all clear; 1: red/tooling or a missing result; 2: inconclusive or a usage error."""
    args = _parser().parse_args(argv)
    root = Path.cwd()
    try:
        if args.command == "summarize":
            return _summarize(args)
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
        return _run(args, root, adapters)
    except Exception as exc:
        print(f"harness-compat: {exc}", file=sys.stderr)
        if os.environ.get("GITHUB_ACTIONS") == "true":
            print(error_annotation("Harness compatibility", str(exc)))
        return 2
