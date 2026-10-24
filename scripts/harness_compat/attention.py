"""Recover human-attention reporting after setup, crash or hosted-runner timeout failures."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from theater.version_policy import version_key

from .adapters import ADAPTER_NAMES
from .contract import Check, Result, Verdict
from .policy import load_policy
from .publish import Gh, api, check_publication_context, file_attention_issue, publish, run_gh
from .run import Run

_WORKFLOWS = {"Harness compatibility", "Harness compatibility canaries"}
_FAILED = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
_RECOVERED: dict[str, Verdict] = {
    "red": "red",
    "inconclusive": "inconclusive",
    "tooling": "tooling",
    "green": "tooling",
}
_MARKER = re.compile(
    r"^(?:\S+\s+)?compat:(start|result|published) (\w+) (\d+\.\d+\.\d+)"
    r"(?: (green|red|inconclusive|tooling))?\s*$"
)


def recover_failures(
    log: str, harness: str, *, run_url: str, publication_required: bool = True
) -> tuple[Result, ...]:
    """Recover failures and unfinished publication; logs never authorize a pass."""
    started: set[str] = set()
    finished: dict[str, str] = {}
    published: set[str] = set()
    for line in log.splitlines():
        match = _MARKER.fullmatch(line)
        if match is None or match[2] != harness:
            continue
        event, _, version, verdict = match.groups()
        version_key(version)
        if event == "start":
            started.add(version)
        elif event == "result" and version in started and verdict is not None:
            finished[version] = verdict
        elif event == "published" and version in finished:
            published.add(version)
    pending = (
        started - published
        if publication_required
        else started - {version for version, verdict in finished.items() if verdict == "green"}
    )
    return tuple(
        Result(
            harness,
            version,
            _RECOVERED.get(finished.get(version, "inconclusive"), "inconclusive"),
            (
                Check(
                    "workflow",
                    "error",
                    f"Workflow log reported {finished.get(version, 'no final outcome')}; "
                    f"qualification/publication needs attention: {run_url}",
                ),
            ),
        )
        for version in sorted(pending, key=version_key)
    )


def report_workflow_failure(root: Path, run_id: int, *, gh: Gh = run_gh) -> None:
    """Run from trusted main via workflow_run, including when qualification never started."""
    check_publication_context(root, os.environ)
    failed_run = api(gh, f"actions/runs/{run_id}")
    if failed_run["name"] not in _WORKFLOWS or failed_run["event"] == "workflow_run":
        raise ValueError("not a compatibility qualification or canary run")
    if failed_run["conclusion"] not in _FAILED:
        return
    jobs = json.loads(
        gh(
            "api",
            "--paginate",
            "--slurp",
            f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/jobs?per_page=100",
            "--jq",
            "[.[].jobs[]]",
        )
    )
    bad = [job for job in jobs if job["conclusion"] in _FAILED]
    run_url = failed_run["html_url"]
    failures: list[Exception] = []
    try:
        file_attention_issue(
            f"Harness compatibility workflow: {run_id}",
            f"Workflow ended **{failed_run['conclusion']}**. Human attention is required.\n\n"
            f"Run: {run_url}\n\n"
            + "\n".join(f"- {job['name']}: {job['conclusion']}" for job in bad)
            + "\n\nInspect the failing step for discovery, setup, cleanup or publication errors.\n",
            gh=gh,
        )
    except Exception as exc:
        failures.append(exc)
    trusted_sweep = (
        failed_run["event"] in {"schedule", "workflow_dispatch"}
        and failed_run["head_branch"] == "main"
    )
    jobs_by_name = {
        label: (name, publication_required)
        for name in ADAPTER_NAMES
        for label, publication_required in (
            (f"Qualify {name}", trusted_sweep),
            (f"Verify {name} plugin and policy", False),
            (f"Canaries {name}", False),
        )
    }
    for job in bad:
        if job["name"] not in jobs_by_name or failed_run["event"] not in {
            "schedule",
            "workflow_dispatch",
            "pull_request",
        }:
            continue
        harness, publication_required = jobs_by_name[job["name"]]
        try:
            log = gh("api", f"repos/{{owner}}/{{repo}}/actions/jobs/{job['id']}/logs")
            results = recover_failures(
                log, harness, run_url=run_url, publication_required=publication_required
            )
            if not results:
                continue
            policy = load_policy(root, harness)
            # Log identities may propose conservative exclusions; they never authorize a pass.
            run = Run(harness, policy, tuple(r.version for r in results), results)
            publish(root, run, run_url=run_url, gh=gh)
        except Exception as exc:
            failures.append(exc)
    if failures:
        raise ExceptionGroup("workflow failure reporting", failures)
