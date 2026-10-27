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

from . import git
from .contract import Result
from .policy import (
    PolicyChangeError,
    check_change,
    derive,
    format_policy,
    load_policy,
    merge_pending,
    policy_relative,
)
from .run import Run
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


def _known_issues(gh: Gh) -> dict[str, int]:
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
            title = item["title"]
            if item.get("pull_request"):
                continue
            if title not in known or (
                item.get("state") == "open" and known[title].get("state") != "open"
            ):
                known[title] = item
    return {title: item["number"] for title, item in known.items()}


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
    checks = failing_checks(result, detail_limit=1000)
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
        known = _known_issues(gh)
    created: list[str] = []
    existing: list[str] = []
    failures: list[Exception] = []
    for result in bad:
        title = issue_title(run.harness, result.version)
        try:
            existed = title in known
            known[title] = _write_issue(
                gh, title, issue_body(result, policy=run.policy, run_url=run_url), known.get(title)
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
    proposal: VersionPolicy,
    *,
    run_url: str,
    max_range: int,
    issues: Mapping[str, int],
    results: tuple[Result, ...],
) -> str:
    exclusions = [
        f"- `{version}`: "
        + (
            f"#{number}"
            if (number := issues.get(issue_title(harness, version)))
            else "issue pending"
        )
        for version in proposal.excluded
    ]
    related = [
        f"- `{result.version}`: #{number} ({result.verdict})"
        for result in results
        if result.version not in proposal.excluded
        and (number := issues.get(issue_title(harness, result.version))) is not None
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


def _prior_failures(
    run: Run, current: VersionPolicy, issues: Mapping[str, int]
) -> tuple[Result, ...]:
    """Failure-only issues fence later admissions until that exact version is requalified."""
    observed = {result.version for result in run.results}
    prior: list[Result] = []
    for title in issues:
        prefix, _, version = title.rpartition(" ")
        if prefix != f"Harness compatibility: {run.harness}" or version in observed:
            continue
        try:
            newer = version_key(version) > version_key(current.maximum)
        except ValueError:
            continue
        if newer:
            prior.append(Result(run.harness, version, "inconclusive"))
    return tuple(prior)


def propose_policy(
    run: Run, *, run_url: str, gh: Gh, max_range: int, issues: Mapping[str, int]
) -> Published:
    """Preserve the pending PR's delta and propose one remote JSON line."""
    branch = f"chore/compat-{run.harness}"
    main = api(gh, "git/ref/heads/main")["object"]["sha"]
    current = _policy_at(gh, run.harness, main)
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
        policy_file = policy_relative(run.harness).as_posix()
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
            original = _policy_at(gh, run.harness, merge_base)
            candidate = _policy_at(gh, run.harness, head)
            check_change(format_policy(original), format_policy(candidate))
            pending = merge_pending(current, original, candidate)
    results = (*_prior_failures(run, current, issues), *run.results)
    if any(result.version == pending.maximum and result.verdict != "green" for result in results):
        # Only the unmerged ceiling is withdrawn; committed admission never shrinks here.
        pending = VersionPolicy(pending.minimum, current.maximum, pending.excluded)
    proposal = derive(pending, results, discovered=run.discovered, max_range=max_range)
    check_change(format_policy(current), format_policy(proposal))
    number = prs[0]["number"] if prs else None
    if version_key(proposal.maximum) <= version_key(current.maximum) or not proposal.admits(
        proposal.maximum
    ):
        if number is not None:
            api(gh, f"pulls/{number}", method="PATCH", body={"state": "closed"})
        return Published(proposal, None)
    if proposal != pending or head is None or main != merge_base:
        _commit_policy(gh, run.harness, branch, main, head, current, proposal)
    body = {
        "title": pr_title(run.harness, current, proposal),
        "body": pr_body(
            run.harness,
            current,
            proposal,
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
                "head": branch,
            },
        )["number"]
    else:
        api(gh, f"pulls/{number}", method="PATCH", body=body)
    return Published(proposal, number)


def publish(
    root: Path,
    run: Run,
    *,
    run_url: str,
    gh: Gh = run_gh,
    environ: Mapping[str, str] = os.environ,
    max_range: int = 10,
) -> Published:
    """Attempt both issues and the PR, even if either publication operation fails."""
    check_publication_context(root, environ)
    if load_policy(root, run.harness) != run.policy:
        raise PolicyChangeError("the policy changed during qualification")
    created: list[str] = []
    existing: list[str] = []
    failures: list[Exception] = []
    outcome: Published | None = None
    known: dict[str, int] = {}
    try:
        known = _known_issues(gh)
        created, existing = file_issues(run, run_url=run_url, gh=gh, known=known)
    except Exception as exc:
        failures.append(exc)
    try:
        outcome = propose_policy(run, run_url=run_url, gh=gh, max_range=max_range, issues=known)
    except Exception as exc:
        failures.append(exc)
    if outcome is not None and outcome.pr is not None:
        related = set(outcome.proposal.excluded) | {result.version for result in run.results}
        for version in sorted(related, key=version_key):
            title = issue_title(run.harness, version)
            if (number := known.get(title)) is None:
                if version in outcome.proposal.excluded:
                    failures.append(
                        RuntimeError(f"Missing version issue for {run.harness} {version}")
                    )
                continue
            try:
                _write_issue(gh, title, f"Ceiling PR: #{outcome.pr}", number, section="pr")
            except Exception as exc:
                failures.append(exc)
    if failures:
        raise ExceptionGroup("compatibility publication failed", failures)
    assert outcome is not None
    return Published(outcome.proposal, outcome.pr, tuple(created), tuple(existing))
