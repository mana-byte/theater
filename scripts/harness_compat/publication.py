"""Turn a qualification summary into workflow outputs and reviewable payload files."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from .contract import Check, Result, Route, is_stable, version_key
from .report import PublishReport, build_report, write_github_outputs


def _result(row: dict[str, Any]) -> Result:
    return Result(
        harness=row["harness"],
        version=row["version"],
        verdict=row["verdict"],
        checks=tuple(
            Check(check["name"], check["status"], check.get("detail", ""))
            for check in row.get("checks", [])
        ),
        adapter_digest=row.get("adapter_digest", ""),
    )


def _attempts(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Attempts per release: the most any of its rows (candidate, watch, composition) needed."""
    tried: dict[str, int] = {}
    for row in rows:
        made = sum(1 for item in row.get("attempts", []) if item["verdict"] != "not run")
        tried[row["version"]] = max(tried.get(row["version"], 0), made)
    return tried


def _carried(summary: dict[str, Any], rows: list[dict[str, Any]], open_failures: Any) -> list[str]:
    """Earlier failures this run neither fixed nor retested; only a human retires them."""
    gate = summary.get("gate") or {}
    floor = gate.get("floor")
    retired = set(gate.get("known_bad", ()))
    seen = {row["version"] for row in rows}
    if watch := summary.get("watch"):
        seen.add(watch["version"])
    return [
        version
        for version in open_failures
        # An entry that is not a release (a hand-edited ledger) can never recover: it stays.
        if not is_stable(version)
        or (
            version not in seen
            and version not in retired
            and (floor is None or version_key(version) >= version_key(floor))
        )
    ]


def _major_boundary(summary: dict[str, Any]) -> str:
    """A promoted release in a newer major than the gate's current ceiling needs a human look."""
    ceiling = (summary.get("gate") or {}).get("ceiling")
    if not ceiling:
        return ""
    major = int(ceiling.split(".")[0])
    crossed = [version for version in summary["greens"] if int(version.split(".")[0]) > major]
    if not crossed:
        return ""
    return (
        f"\n\n**Major version boundary:** {', '.join(crossed)} crosses from {major}.x. "
        "The proofs cover the protocol only; read the release notes before merging."
    )


def prepare_publication(
    summary: dict[str, Any], summary_path: Path, open_failures: Sequence[str] = ()
) -> PublishReport:
    """Write PR and issue body files and append the GitHub step outputs.

    ``open_failures`` are the releases the open issue lists: it closes only once each of them
    was proved again (or retired from the gate), never because some other release was green.
    """
    rows = [*summary["reused_results"], *summary["results"]]
    watch = summary.get("watch")
    distinct_watch = watch if watch is not None and not summary.get("watch_reused", False) else None
    failing = ("red", "tooling", "inconclusive")
    if distinct_watch is not None and distinct_watch["verdict"] in failing:
        rows.append(distinct_watch)
    results = tuple(_result(row) for row in rows)
    carried = _carried(summary, [*summary["results"], *summary["reused_results"]], open_failures)
    route_rows = [*rows]
    if distinct_watch is not None and distinct_watch["verdict"] not in failing:
        route_rows.append(distinct_watch)
    routes: list[Route] = [
        (route["method"], route["path"]) for row in route_rows for route in row.get("routes", [])
    ]
    admitted = [watch["version"]] if watch and watch["verdict"] == "red" else []
    report = build_report(
        summary["harness"],
        results,
        run_url=summary["run_url"],
        release_runs={row["version"]: row.get("run_url", "") for row in rows},
        routes=routes,
        admitted_versions=admitted,
        stale_warnings=summary.get("stale_warnings", ()),
        attempts=_attempts(rows),
        carried=carried,
    )
    recovered = bool(watch and watch["verdict"] == "green")
    action = report.issue_action
    if action == "none" and recovered and not carried:
        action = "close"
    elif action == "none" and carried and set(carried) != set(open_failures):
        action = "ledger"  # some listed releases recovered: shrink the stored list, keep labels
    withdraw = ",".join(summary.get("revoked", ()))
    report = replace(
        report,
        pr_needed=summary["published"],
        issue_action=action,
        pr_withdraw=withdraw,
        pr_body=report.pr_body + _major_boundary(summary),
    )
    stem = summary_path.with_suffix("")
    stem.with_name(stem.name + ".pr.md").write_text(report.pr_body + "\n", encoding="utf-8")
    stem.with_name(stem.name + ".issue.md").write_text(report.issue_body + "\n", encoding="utf-8")
    if output := os.environ.get("GITHUB_OUTPUT"):
        write_github_outputs(report, Path(output))
    return report
