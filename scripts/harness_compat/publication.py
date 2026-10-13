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
    if watch and watch["verdict"] in ("red", "tooling"):
        rows.append(watch)
    results = tuple(_result(row) for row in rows)
    route_rows = [*rows]
    if watch and watch["verdict"] not in ("red", "tooling"):
        route_rows.append(watch)
    routes: list[Route] = [
        (route["method"], route["path"]) for row in route_rows for route in row.get("routes", [])
    ]
    admitted = [watch["version"]] if watch and watch["verdict"] == "red" else []
    report = build_report(
        summary["harness"],
        results,
        run_url=summary["run_url"],
        routes=routes,
        admitted_versions=admitted,
        evidence_only=bool(summary.get("evidence_only")),
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
