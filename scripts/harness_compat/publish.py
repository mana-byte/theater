"""Publish a completed run: one issue per unsuccessful release, and at most one reviewed PR.

Nothing is written before the whole run is done, and nothing is remembered between runs: an
existing issue is the only dedupe, and a proposal is recomputed from fresh evidence every time.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from theater.harness.contracts.version_policy import VersionPolicy

from . import git
from .contract import Result
from .policy import (
    PolicyChangeError,
    Unproposable,
    check_change,
    check_working_tree,
    derive,
    format_policy,
    load_policy,
    policy_relative,
)
from .run import Run
from .summary import change_text, failing_checks

Gh = Callable[..., str]


def check_publication_context(root: Path, environ: Mapping[str, str]) -> None:
    """Only a clean, pinned main checkout in a schedule or dispatch may publish."""
    if environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("--publish is reserved for GitHub Actions")
    if environ.get("GITHUB_REF") != "refs/heads/main" or environ.get("GITHUB_EVENT_NAME") not in {
        "schedule",
        "workflow_dispatch",
    }:
        raise ValueError("publishing requires a schedule or dispatch on main")
    if git.git(root, "rev-parse", "HEAD").strip() != environ.get("GITHUB_SHA"):
        raise ValueError("publication checkout differs from the workflow's pinned commit")
    if changed := git.changed_paths(root):
        raise PolicyChangeError(f"publication checkout is dirty: {sorted(changed)}")


@dataclass(frozen=True, slots=True)
class _Kind:
    label: str
    color: str
    meaning: str


#: How an unsuccessful verdict is labelled and explained: a CLI incompatibility is never confused
#: with a run that proved nothing, or with our own accounting breaking.
_KINDS: Mapping[str, _Kind] = {
    "red": _Kind(
        "incompatible",
        "b60205",
        "The live conformance suite failed: the release breaks a contract Theater relies on.",
    ),
    "inconclusive": _Kind(
        "inconclusive",
        "fbca04",
        "No verdict: the install, the infrastructure or a timeout stopped the suite "
        "before it could judge the release.",
    ),
    "tooling": _Kind(
        "tooling",
        "d93f0b",
        "Theater's own suite accounting broke while the CLI ran cleanly; "
        "this is not a CLI incompatibility.",
    ),
}
_LABEL_COLOR = "5319e7"
_MAX_CHECKS = 12


@dataclass(frozen=True, slots=True)
class Published:
    """What publishing did: the PR step to take (a proposal, retiring the open one, or nothing)."""

    pr_action: Literal["propose", "retire", "keep"]
    proposal: VersionPolicy | None
    created: tuple[str, ...]
    existing: tuple[str, ...]


def run_gh(*args: str) -> str:
    """``gh`` with an argument list (never a shell string); the workflow supplies GH_TOKEN."""
    done = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120, check=False)
    if done.returncode:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {done.stderr.strip()}")
    return done.stdout


def issue_title(harness: str, version: str) -> str:
    return f"Harness compatibility: {harness} {version}"


def issue_body(result: Result, *, policy: VersionPolicy, run_url: str) -> str:
    """Plain prose for people: the release, the verdict and its meaning, the run, the checks."""
    kind = _KINDS[result.verdict]
    admitted = "admitted" if policy.admits(result.version) else "not admitted"
    lines = [
        f"{result.harness} {result.version} ended **{result.verdict}** ({kind.label}).",
        "",
        kind.meaning,
        "",
        f"- Policy at the time of the run: {admitted} ({policy.range_text})",
        f"- CI run: {run_url or 'not available'}",
        "",
        "Failing checks:",
    ]
    checks = failing_checks(result)
    lines.extend(f"- {line}" for line in checks[:_MAX_CHECKS])
    if len(checks) > _MAX_CHECKS:
        lines.append(f"- and {len(checks) - _MAX_CHECKS} more")
    if not checks:
        lines.append("- none recorded")
    return "\n".join(lines) + "\n"


def _issue_labels(result: Result, run: Run) -> list[str]:
    labels = [f"compat:{result.harness}", _KINDS[result.verdict].label]
    if run.policy.admits(result.version):
        labels.append("admitted-regression")
    return labels


def file_issues(run: Run, *, run_url: str, gh: Gh = run_gh) -> tuple[list[str], list[str]]:
    """Open an issue for each unsuccessful release that has none; existence is the only dedupe."""
    bad = [result for result in run.results if result.verdict != "green"]
    if not bad:
        return [], []
    for label in {f"compat:{run.harness}", "admitted-regression"}:
        gh("label", "create", label, "--color", _LABEL_COLOR, "--force")
    for kind in _KINDS.values():
        gh("label", "create", kind.label, "--color", kind.color, "--force")
    listing = gh(
        "api", "--paginate", "repos/{owner}/{repo}/issues", "--method", "GET",
        "-f", "state=open", "-f", f"labels=compat:{run.harness}", "-f", "per_page=100",
        "--jq", ".[] | select(.pull_request == null) | .title",
    )  # fmt: skip
    known = set(listing.splitlines())
    created: list[str] = []
    existing: list[str] = []
    for result in bad:
        title = issue_title(run.harness, result.version)
        if title in known:
            existing.append(title)
            continue
        with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8", delete=False) as body:
            body.write(issue_body(result, policy=run.policy, run_url=run_url))
        try:
            args = ["issue", "create", "--title", title, "--body-file", body.name]
            for label in _issue_labels(result, run):
                args += ["--label", label]
            gh(*args)
        finally:
            Path(body.name).unlink(missing_ok=True)
        created.append(title)
    return created, existing


def pr_title(harness: str) -> str:
    return f"chore(compat): update {harness} version policy"


def pr_body(run: Run, proposal: VersionPolicy, *, run_url: str) -> str:
    """The policy delta and the run that proved it; the run's log holds every release's result."""
    return (
        f"{run.harness} policy: {change_text(run.policy, proposal)}\n\nRun: {run_url or 'local'}\n"
    )


def write_outputs(path: Path, values: Mapping[str, str]) -> None:
    """Append single-line step outputs; a multi-line value could forge another output."""
    for key, value in values.items():
        if "\n" in value or "\r" in value:
            raise ValueError(f"output {key!r} must be one line")
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        for key, value in values.items():
            stream.write(f"{key}={value}\n")


def prepare_proposal(
    root: Path, run: Run, *, run_url: str, environ: Mapping[str, str]
) -> Published:
    """Write the evidence-derived policy into the checkout when it differs, behind the guards."""
    if changed := git.changed_paths(root):
        raise PolicyChangeError(f"proposal checkout is dirty: {sorted(changed)}")
    if load_policy(root, run.harness) != run.policy:
        raise PolicyChangeError("the policy changed while its releases were being qualified")
    try:
        proposal = derive(run.policy, run.results)
    except Unproposable as exc:
        print(f"{run.harness}: no policy proposed: {exc}")
        return Published("keep", None, (), ())
    if proposal == run.policy:
        return Published("retire", proposal, (), ())
    relative = policy_relative(run.harness)
    old_text = (root / relative).read_text(encoding="utf-8")
    new_text = format_policy(proposal)
    check_change(old_text, new_text)
    if not (target := environ.get("GITHUB_OUTPUT")):
        raise ValueError("a policy proposal needs the workflow's GITHUB_OUTPUT destination")
    handle, name = tempfile.mkstemp(
        prefix=f"compat-pr-{run.harness}-",
        suffix=".md",
        dir=environ.get("RUNNER_TEMP") or tempfile.gettempdir(),
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as body:
            body.write(pr_body(run, proposal, run_url=run_url))
        (root / relative).write_text(new_text, encoding="utf-8")
        check_working_tree(root, run.harness)
        # Announce the action last, after every input the PR step needs is ready.
        outputs = {"pr_title": pr_title(run.harness), "pr_body_path": name, "pr_action": "propose"}
        write_outputs(Path(target), outputs)
    except BaseException:
        (root / relative).write_text(old_text, encoding="utf-8")
        Path(name).unlink(missing_ok=True)
        raise
    return Published("propose", proposal, (), ())


def publish(
    root: Path,
    run: Run,
    *,
    run_url: str,
    gh: Gh = run_gh,
    environ: Mapping[str, str] = os.environ,
) -> Published:
    """Issues first (plain facts of this run), then the guarded policy proposal.

    Neither blocks the other: a failure in one is raised only after the other was attempted.
    """
    check_publication_context(root, environ)
    if load_policy(root, run.harness) != run.policy:
        raise PolicyChangeError("the policy changed while its releases were being qualified")
    created: list[str] = []
    existing: list[str] = []
    failure: Exception | None = None
    try:
        created, existing = file_issues(run, run_url=run_url, gh=gh)
    except Exception as exc:
        failure = exc
    outcome = prepare_proposal(root, run, run_url=run_url, environ=environ)
    if outcome.pr_action != "propose" and (target := environ.get("GITHUB_OUTPUT")):
        write_outputs(Path(target), {"pr_action": outcome.pr_action})
    if failure is not None:
        raise failure
    return Published(outcome.pr_action, outcome.proposal, tuple(created), tuple(existing))
