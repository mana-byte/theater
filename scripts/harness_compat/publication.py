"""Turn a qualification summary into workflow outputs and reviewable payload files."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

from .contract import Route, is_stable, version_key
from .report import PublishReport, build_report, write_github_outputs
from .runner import RunRow, RunState


def _attempts(rows: list[RunRow]) -> dict[str, int]:
    """Attempts per release: the most any of its rows (candidate, watch, composition) needed."""
    tried: dict[str, int] = {}
    for row in rows:
        made = sum(1 for item in row.attempts or () if item["verdict"] != "not run")
        version = row.result.version
        tried[version] = max(tried.get(version, 0), made)
    return tried


def _carried(state: RunState, open_failures: Sequence[str]) -> list[str]:
    """Earlier failures this run neither fixed nor retested; only a human retires them."""
    retired = state.gate.known_bad
    seen = {row.result.version for row in (*state.rows, *state.reused_rows)}
    if state.watch:
        seen.add(state.watch.result.version)
    return [
        version
        for version in open_failures
        # An entry that is not a release (a hand-edited ledger) can never recover: it stays.
        if not is_stable(version)
        or (
            version not in seen
            and version not in retired
            and version_key(version) >= version_key(state.gate.floor)
        )
    ]


def _major_boundary(state: RunState) -> str:
    """A promoted release in a newer major than the gate's current ceiling needs a human look."""
    major = int(state.gate.ceiling.split(".")[0])
    crossed = [version for version in state.greens if int(version.split(".")[0]) > major]
    if not crossed:
        return ""
    return (
        f"\n\n**Major version boundary:** {', '.join(crossed)} crosses from {major}.x. "
        "The proofs cover the protocol only; read the release notes before merging."
    )


def prepare_publication(
    state: RunState,
    summary_path: Path,
    open_failures: Sequence[str] = (),
) -> PublishReport:
    """Write PR and issue body files and append the GitHub step outputs.

    ``open_failures`` are the releases the open issue lists: it closes only once each of them
    was proved again (or retired from the gate), never because some other release was green.
    """
    rows = [*state.reused_rows, *state.rows]
    watch = state.watch
    distinct_watch = watch if watch is not None and not state.watch_reused else None
    failing = ("red", "tooling", "inconclusive")
    if distinct_watch is not None and distinct_watch.result.verdict in failing:
        rows.append(distinct_watch)
    results = tuple(row.result for row in rows)
    carried = _carried(state, open_failures)
    route_rows = [*rows]
    if distinct_watch is not None and distinct_watch.result.verdict not in failing:
        route_rows.append(distinct_watch)
    routes: list[Route] = [
        (route["method"], route["path"]) for row in route_rows for route in row.routes
    ]
    admitted = [watch.result.version] if watch and watch.result.verdict == "red" else []
    report = build_report(
        state.harness,
        results,
        run_url=state.run_url,
        release_runs={row.result.version: row.run_url for row in rows},
        routes=routes,
        admitted_versions=admitted,
        stale_warnings=state.stale_warnings,
        attempts=_attempts(rows),
        carried=carried,
    )
    recovered = bool(watch and watch.result.verdict == "green")
    action = report.issue_action
    if action == "none" and recovered and not carried:
        action = "close"
    elif action == "none" and carried and set(carried) != set(open_failures):
        action = "ledger"  # some listed releases recovered: shrink the stored list, keep labels
    withdraw = ",".join(state.revoked)
    report = replace(
        report,
        pr_needed=state.published,
        issue_action=action,
        pr_withdraw=withdraw,
        pr_body=report.pr_body + _major_boundary(state),
    )
    stem = summary_path.with_suffix("")
    stem.with_name(stem.name + ".pr.md").write_text(report.pr_body + "\n", encoding="utf-8")
    stem.with_name(stem.name + ".issue.md").write_text(report.issue_body + "\n", encoding="utf-8")
    if output := os.environ.get("GITHUB_OUTPUT"):
        write_github_outputs(report, Path(output))
    return report
