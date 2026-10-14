"""Turn a qualification summary into workflow outputs and reviewable payload files."""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from .contract import Check, Result, Route
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


def prepare_publication(summary: dict[str, Any], summary_path: Path) -> PublishReport:
    """Write PR and issue body files and append the GitHub step outputs."""
    rows = [*summary["reused_results"], *summary["results"]]
    watch = summary.get("watch")
    distinct_watch = watch if watch is not None and not summary.get("watch_reused", False) else None
    if distinct_watch is not None and distinct_watch["verdict"] in ("red", "tooling"):
        rows.append(distinct_watch)
    results = tuple(_result(row) for row in rows)
    route_rows = [*rows]
    if distinct_watch is not None and distinct_watch["verdict"] not in ("red", "tooling"):
        route_rows.append(distinct_watch)
    routes: list[Route] = [
        (route["method"], route["path"]) for row in route_rows for route in row.get("routes", [])
    ]
    admitted = [watch["version"]] if watch and watch["verdict"] == "red" else []
    report = build_report(
        summary["harness"],
        results,
        run_url=summary["run_url"],
        pr_versions=summary["greens"] if summary["gate_kind"] == "exact" else None,
        release_runs={row["version"]: row.get("run_url", "") for row in rows},
        routes=routes,
        admitted_versions=admitted,
        stale_warnings=summary.get("stale_warnings", ()),
    )
    recovered = bool(watch and watch["verdict"] == "green")
    if summary["inconclusive"]:
        report = replace(report, pr_needed=False, issue_action="none")
    else:
        action = report.issue_action
        if action == "none" and recovered:
            action = "close"
        report = replace(report, pr_needed=summary["published"], issue_action=action)
    stem = summary_path.with_suffix("")
    stem.with_name(stem.name + ".pr.md").write_text(report.pr_body + "\n", encoding="utf-8")
    stem.with_name(stem.name + ".issue.md").write_text(report.issue_body + "\n", encoding="utf-8")
    if output := os.environ.get("GITHUB_OUTPUT"):
        write_github_outputs(report, Path(output))
    return report
