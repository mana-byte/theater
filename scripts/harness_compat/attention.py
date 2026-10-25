"""Recover human-attention reporting after setup, crash or hosted-runner timeout failures."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from theater.version_policy import version_key

from .adapters import ADAPTER_NAMES
from .contract import Check, Result, Verdict
from .policy import load_policy
from .publish import (
    SWEEP_EVENTS,
    Gh,
    api,
    check_publication_context,
    file_attention_issue,
    publish,
    run_gh,
)
from .run import Run

_WORKFLOWS = {
    "Harness compatibility": ".github/workflows/harness-compat.yml",
    "Harness compatibility canaries": ".github/workflows/harness-compat-canaries.yml",
}
_FAILED = {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}
_RECOVERY_JOBS = {
    label: (name, publication_required)
    for name in ADAPTER_NAMES
    for label, publication_required in (
        (f"Qualify {name}", True),
        (f"Verify {name} plugin and policy", False),
        (f"Canaries {name}", False),
    )
}
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
    log: str,
    harness: str,
    *,
    run_url: str,
    publication_required: bool = True,
    include_unfinished: bool = True,
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
    if not include_unfinished:
        pending.intersection_update(finished)
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


def _workflow(run: dict[str, Any]) -> str:
    """The workflow's own name: its ``run-name`` makes the API report ``<name> (<event>)``."""
    return run["name"].removesuffix(f" ({run['event']})")


def _superseded(run: dict[str, Any], gh: Gh) -> bool:
    """Whether concurrency replaced this PR or canary run with a newer candidate."""
    if run["event"] == "pull_request":
        for reference in run["pull_requests"]:
            current = api(gh, f"pulls/{reference['number']}")
            if current["state"] == "closed" or current["head"]["sha"] != run["head_sha"]:
                return True
        return False
    if _workflow(run) != "Harness compatibility canaries" or run["event"] not in SWEEP_EVENTS:
        return False
    query = urlencode({"branch": run["head_branch"], "per_page": 100})
    newer = api(gh, f"actions/workflows/{run['workflow_id']}/runs?{query}")["workflow_runs"]
    return any(
        other["id"] > run["id"]
        and other["path"].partition("@")[0] == _WORKFLOWS[_workflow(run)]
        and (other["head_repository"] or {}).get("full_name") == run["head_repository"]["full_name"]
        and other["head_branch"] == run["head_branch"]
        and other["event"] in SWEEP_EVENTS
        for other in newer
    )


def report_workflow_failure(root: Path, run_id: int, *, gh: Gh = run_gh) -> None:
    """Run from trusted main via workflow_run, including when qualification never started."""
    check_publication_context(root, os.environ)
    failed_run = api(gh, f"actions/runs/{run_id}")
    workflow = _workflow(failed_run)
    if workflow not in _WORKFLOWS or failed_run["event"] == "workflow_run":
        raise ValueError("not a compatibility qualification or canary run")
    if failed_run["conclusion"] not in _FAILED:
        return
    pages = json.loads(
        gh(
            "api",
            "--paginate",
            "--slurp",
            f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/jobs?per_page=100",
        )
    )
    jobs = [job for page in pages for job in page["jobs"]]
    bad = [job for job in jobs if job["conclusion"] in _FAILED]
    run_url = failed_run["html_url"]
    failures: list[Exception] = []
    trusted_logs = (
        failed_run["path"].partition("@")[0] == _WORKFLOWS[workflow]
        and (failed_run["head_repository"] or {}).get("full_name")
        == os.environ["GITHUB_REPOSITORY"]
    )
    superseded = False
    if trusted_logs and any(job["conclusion"] == "cancelled" for job in bad):
        try:
            superseded = _superseded(failed_run, gh)
        except Exception as exc:
            failures.append(exc)
    if not superseded or any(job["conclusion"] != "cancelled" for job in bad):
        try:
            file_attention_issue(
                f"Harness compatibility workflow: {run_id}",
                f"Workflow ended **{failed_run['conclusion']}**. Human attention is required.\n\n"
                f"Run: {run_url}\n\n"
                + "\n".join(f"- {job['name']}: {job['conclusion']}" for job in bad)
                + "\n\nInspect the failing step for setup, cleanup or publication errors.\n",
                gh=gh,
            )
        except Exception as exc:
            failures.append(exc)
    trusted_sweep = failed_run["event"] in SWEEP_EVENTS and failed_run["head_branch"] == "main"
    for job in bad:
        if (
            not trusted_logs
            or job["name"] not in _RECOVERY_JOBS
            or failed_run["event"] not in SWEEP_EVENTS | {"pull_request"}
        ):
            continue
        harness, publication_required = _RECOVERY_JOBS[job["name"]]
        try:
            # Job logs carry the runner's ANSI colour codes, which gh refuses to print otherwise.
            log = gh(
                "api",
                f"repos/{{owner}}/{{repo}}/actions/jobs/{job['id']}/logs",
                "--allow-escape-sequences",
            )
            results = recover_failures(
                log,
                harness,
                run_url=run_url,
                publication_required=publication_required and trusted_sweep,
                include_unfinished=not (superseded and job["conclusion"] == "cancelled"),
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
