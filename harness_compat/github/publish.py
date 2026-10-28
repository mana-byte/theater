"""Scripts own failure issues and the single accumulated JSON-only PR per harness."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from theater.version_policy import VersionPolicy, version_key

from .. import git
from ..contract import Result
from ..diagnostics import FAILURE_DETAIL_CHARS
from ..policy import (
    PolicyChangeError,
    PublicationPlan,
    check_change,
    format_policy,
    load_policy,
    merge_pending,
    plan_publication,
    policy_relative,
)
from ..qualification.run import Run
from .summary import failing_checks

Gh = Callable[..., str]
SWEEP_EVENTS = frozenset({"schedule", "workflow_dispatch"})
_MEANINGS = {
    "red": "A live contract assertion failed. Investigation must determine whether the "
    "new binary, Theater's wiring or the test caused it.",
    "inconclusive": "The run could not establish compatibility (for example installation, "
    "infrastructure or timeout). This is not proof of a binary defect.",
    "tooling": "Qualification or publication failed to complete reliably. "
    "This is not proof of a binary defect.",
}


@dataclass(frozen=True, slots=True)
class Published:
    proposal: VersionPolicy
    pr: int | None
    created: tuple[str, ...] = ()
    existing: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _PolicyBranch:
    name: str
    main: str
    head: str | None
    merge_base: str
    current: VersionPolicy
    pending: VersionPolicy
    pr: int | None


def check_publication_context(root: Path, environ: Mapping[str, str]) -> None:
    """Publication uses trusted, pinned main code in GitHub Actions."""
    if environ.get("GITHUB_ACTIONS") != "true":
        raise ValueError("publication is reserved for GitHub Actions")
    if environ.get("GITHUB_REF") != "refs/heads/main" or environ.get(
        "GITHUB_EVENT_NAME"
    ) not in SWEEP_EVENTS | {"workflow_run"}:
        raise ValueError("publishing requires trusted main workflow code")
    if git.git(root, "rev-parse", "HEAD").strip() != environ.get("GITHUB_SHA"):
        raise ValueError("publication checkout differs from the workflow's pinned commit")
    if changed := git.changed_paths(root):
        raise PolicyChangeError(f"publication checkout is dirty: {sorted(changed)}")


def run_gh(*args: str) -> str:
    """Argument lists and temporary request/body files keep shell interpolation out of gh."""
    done = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120, check=False)
    if done.returncode:
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {done.stderr.strip()}")
    return done.stdout


def api(gh: Gh, path: str, *, method: str = "GET", body: dict[str, Any] | None = None) -> Any:
    args = ["api", f"repos/{{owner}}/{{repo}}/{path}", "--method", method]
    if body is None:
        return json.loads(gh(*args))
    with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8") as request:
        json.dump(body, request)
        request.flush()
        return json.loads(gh(*args, "--input", request.name))


def _known_issues(harness: str, gh: Gh) -> dict[str, int]:
    """Index canonical versions for this harness, preferring existing open issues."""
    listing = gh(
        "api",
        "--paginate",
        "--slurp",
        "repos/{owner}/{repo}/issues",
        "--method",
        "GET",
        "-f",
        "state=all",
        "-f",
        "per_page=100",
    )
    known: dict[str, dict[str, Any]] = {}
    for page in json.loads(listing):
        for item in page:
            if item.get("pull_request"):
                continue
            prefix, _, version = item["title"].rpartition(" ")
            if prefix != f"Harness compatibility: {harness}":
                continue
            try:
                version_key(version)
            except ValueError:
                continue
            if version not in known or (
                item.get("state") == "open" and known[version].get("state") != "open"
            ):
                known[version] = item
    return {version: item["number"] for version, item in known.items()}


def _managed_body(previous: str, text: str, section: str) -> str:
    """Only replace our marked section; legacy descriptions and human notes survive."""
    start = f"<!-- theater-compat:{section}:start -->"
    end = f"<!-- theater-compat:{section}:end -->"
    block = f"{start}\n{text.rstrip()}\n{end}"
    before, marker, remaining = previous.partition(start)
    _, closing, after = remaining.partition(end)
    if (
        previous.count(start) != previous.count(end)
        or previous.count(start) > 1
        or (marker and not closing)
    ):
        raise PolicyChangeError(
            "the issue has an ambiguous managed section; its body was preserved"
        )
    if marker and closing:
        return before + block + after
    return f"{previous.rstrip()}\n\n{block}\n" if previous else block + "\n"


def _write_issue(
    gh: Gh, title: str, text: str, number: int | None, *, section: str = "report"
) -> int:
    previous = api(gh, f"issues/{number}") if number is not None else {}
    body: dict[str, Any] = {
        "body": _managed_body(previous.get("body") or "", text, section),
    }
    if number is None:
        body["title"] = title
    elif section == "report" and previous["state"] == "closed":
        body["state"] = "open"
    result = api(
        gh,
        "issues" if number is None else f"issues/{number}",
        method="POST" if number is None else "PATCH",
        body=body,
    )
    return int(result["number"])


def issue_title(harness: str, version: str) -> str:
    return f"Harness compatibility: {harness} {version}"


def issue_body(result: Result, *, policy: VersionPolicy, run_url: str) -> str:
    checks = failing_checks(result, detail_limit=FAILURE_DETAIL_CHARS)
    return (
        "\n".join(
            [
                f"{result.harness} {result.version} ended **{result.verdict}**.",
                "",
                _MEANINGS[result.verdict],
                "",
                "This version is not qualified for admission. A later ceiling increase "
                "must exclude it unless requalification passes.",
                f"Policy: {policy.range_text}",
                f"Run: {run_url}",
                "",
                *[f"- {check}" for check in checks[:12]],
            ]
        )
        + "\n"
    )


def file_issues(
    run: Run, *, run_url: str, gh: Gh = run_gh, known: dict[str, int] | None = None
) -> tuple[list[str], list[str]]:
    bad = [result for result in run.results if result.verdict != "green"]
    if not bad:
        return [], []
    if known is None:
        known = _known_issues(run.harness, gh)
    created: list[str] = []
    existing: list[str] = []
    failures: list[Exception] = []
    for result in bad:
        title = issue_title(run.harness, result.version)
        try:
            existed = result.version in known
            known[result.version] = _write_issue(
                gh,
                title,
                issue_body(result, policy=run.policy, run_url=run_url),
                known.get(result.version),
            )
            (existing if existed else created).append(title)
        except Exception as exc:
            failures.append(exc)
    if failures:
        raise ExceptionGroup("failure issue publication", failures)
    return created, existing


def pr_title(harness: str, old: VersionPolicy, proposal: VersionPolicy) -> str:
    return f"chore(compat): {harness} {old.maximum} → {proposal.maximum}"


def pr_body(
    harness: str,
    old: VersionPolicy,
    plan: PublicationPlan,
    *,
    run_url: str,
    max_range: int,
    issues: Mapping[str, int],
    results: tuple[Result, ...],
) -> str:
    proposal = plan.proposal
    outcomes = {result.version: f" ({result.verdict})" for result in results}
    exclusions = [
        f"- `{version}`: " + (f"#{number}" if (number := issues.get(version)) else "issue pending")
        for version in proposal.excluded
    ]
    related = [
        f"- `{version}`: #{number}{outcomes.get(version, '')}"
        for version in plan.related_versions
        if version not in proposal.excluded and (number := issues.get(version)) is not None
    ]
    return (
        f"{harness} policy: `{old.range_text}` to `{proposal.range_text}`\n\n"
        f"Floor: newest {max_range} releases in range (max-range={max_range}); the Nix full "
        "profile may ship older CLIs until flake.lock is bumped.\n\n"
        f"Run: {run_url}\n"
        + ("\nExcluded versions:\n" + "\n".join(exclusions) + "\n" if exclusions else "")
        + ("\nRelated version issues:\n" + "\n".join(related) + "\n" if related else "")
    )


def _policy_at(gh: Gh, name: str, revision: str) -> VersionPolicy:
    item = api(gh, f"contents/{policy_relative(name).as_posix()}?ref={revision}")
    if item["encoding"] != "base64":
        raise PolicyChangeError("GitHub returned an unsupported policy encoding")
    text = base64.b64decode(item["content"]).decode("utf-8")
    policy = VersionPolicy.from_text(text)
    if text != format_policy(policy):
        raise PolicyChangeError("the remote policy is not one canonical compact line")
    return policy


def _commit_policy(
    gh: Gh,
    name: str,
    branch: str,
    main: str,
    head: str | None,
    current: VersionPolicy,
    proposal: VersionPolicy,
) -> None:
    """Build on current main and append to the branch without overwriting concurrent commits."""
    commit = api(gh, f"git/commits/{main}")
    tree = api(
        gh,
        "git/trees",
        method="POST",
        body={
            "base_tree": commit["tree"]["sha"],
            "tree": [
                {
                    "path": policy_relative(name).as_posix(),
                    "mode": "100644",
                    "type": "blob",
                    "content": format_policy(proposal),
                }
            ],
        },
    )
    new = api(
        gh,
        "git/commits",
        method="POST",
        body={
            "message": pr_title(name, current, proposal),
            "tree": tree["sha"],
            "parents": list(dict.fromkeys([main, *([head] if head is not None else [])])),
        },
    )
    if head is None:
        api(gh, "git/refs", method="POST", body={"ref": f"refs/heads/{branch}", "sha": new["sha"]})
    else:
        api(
            gh, f"git/refs/heads/{branch}", method="PATCH", body={"sha": new["sha"], "force": False}
        )


def _policy_branch(harness: str, gh: Gh) -> _PolicyBranch:
    """Read and validate the pending branch without changing GitHub."""
    branch = f"chore/compat-{harness}"
    main = api(gh, "git/ref/heads/main")["object"]["sha"]
    current = _policy_at(gh, harness, main)
    refs = api(gh, f"git/matching-refs/heads/{branch}")
    head = next(
        (ref["object"]["sha"] for ref in refs if ref["ref"] == f"refs/heads/{branch}"), None
    )
    prs = json.loads(
        gh(
            "pr",
            "list",
            "--head",
            branch,
            "--base",
            "main",
            "--state",
            "open",
            "--json",
            "number",
        )
    )
    if len(prs) > 1 or (prs and head is None):
        raise PolicyChangeError("the shared policy PR has an ambiguous or missing branch")
    pending = current
    merge_base = main
    if head is not None:
        compared = api(gh, f"compare/{main}...{head}")
        merge_base = compared["merge_base_commit"]["sha"]
        files = compared["files"]
        policy_file = policy_relative(harness).as_posix()
        if files and not (
            len(files) == 1
            and files[0]["filename"] == policy_file
            and files[0]["additions"] == files[0]["deletions"] == 1
        ):
            changed = ", ".join(
                f"{f['filename']} (+{f['additions']}/-{f['deletions']})" for f in files
            )
            raise PolicyChangeError(
                f"the pending branch must change exactly one line of {policy_file}; "
                f"it changes {changed}"
            )
        if files:
            original = _policy_at(gh, harness, merge_base)
            candidate = _policy_at(gh, harness, head)
            check_change(format_policy(original), format_policy(candidate))
            pending = merge_pending(current, original, candidate)
    return _PolicyBranch(
        branch, main, head, merge_base, current, pending, prs[0]["number"] if prs else None
    )


def _publish_policy(
    run: Run,
    branch: _PolicyBranch,
    plan: PublicationPlan,
    *,
    run_url: str,
    gh: Gh,
    max_range: int,
    issues: Mapping[str, int],
) -> int | None:
    """Apply the planned ceiling PR after its required state has been read."""
    number = branch.pr
    if not plan.raises_ceiling:
        if number is not None:
            api(gh, f"pulls/{number}", method="PATCH", body={"state": "closed"})
        return None
    if plan.proposal != branch.pending or branch.head is None or branch.main != branch.merge_base:
        _commit_policy(
            gh, run.harness, branch.name, branch.main, branch.head, branch.current, plan.proposal
        )
    body = {
        "title": pr_title(run.harness, branch.current, plan.proposal),
        "body": pr_body(
            run.harness,
            branch.current,
            plan,
            run_url=run_url,
            max_range=max_range,
            issues=issues,
            results=run.results,
        ),
    }
    if number is None:
        number = api(
            gh,
            "pulls",
            method="POST",
            body={
                **body,
                "base": "main",
                "head": branch.name,
            },
        )["number"]
    else:
        api(gh, f"pulls/{number}", method="PATCH", body=body)
    return number


def publish(
    root: Path,
    run: Run,
    *,
    run_url: str,
    gh: Gh = run_gh,
    environ: Mapping[str, str] = os.environ,
    max_range: int = 10,
) -> Published:
    """Require issue history, plan once, then attempt issue and policy publication."""
    check_publication_context(root, environ)
    if load_policy(root, run.harness) != run.policy:
        raise PolicyChangeError("the policy changed during qualification")
    known = _known_issues(run.harness, gh)
    created: list[str] = []
    existing: list[str] = []
    failures: list[Exception] = []
    publication: tuple[_PolicyBranch, PublicationPlan] | None = None
    try:
        branch = _policy_branch(run.harness, gh)
        plan = plan_publication(
            branch.current,
            branch.pending,
            run.results,
            unqualified_versions=known,
            discovered=run.discovered,
            max_range=max_range,
        )
        publication = branch, plan
    except Exception as exc:
        failures.append(exc)
    try:
        created, existing = file_issues(run, run_url=run_url, gh=gh, known=known)
    except Exception as exc:
        failures.append(exc)
    if publication is None:
        raise ExceptionGroup("compatibility publication failed", failures)
    branch, plan = publication
    pr: int | None = None
    try:
        pr = _publish_policy(
            run, branch, plan, run_url=run_url, gh=gh, max_range=max_range, issues=known
        )
    except Exception as exc:
        failures.append(exc)
    if pr is not None:
        for version in plan.related_versions:
            title = issue_title(run.harness, version)
            if (number := known.get(version)) is None:
                if version in plan.proposal.excluded:
                    failures.append(
                        RuntimeError(f"Missing version issue for {run.harness} {version}")
                    )
                continue
            try:
                _write_issue(gh, title, f"Ceiling PR: #{pr}", number, section="pr")
            except Exception as exc:
                failures.append(exc)
    if failures:
        raise ExceptionGroup("compatibility publication failed", failures)
    return Published(plan.proposal, pr, tuple(created), tuple(existing))
