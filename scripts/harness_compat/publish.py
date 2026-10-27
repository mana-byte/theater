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

from theater.version_policy import VersionPolicy

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


def _open_issues(gh: Gh) -> dict[str, int]:
    listing = gh(
        "api",
        "--paginate",
        "--slurp",
        "repos/{owner}/{repo}/issues",
        "--method",
        "GET",
        "-f",
        "state=open",
        "-f",
        "per_page=100",
    )
    return {
        item["title"]: item["number"]
        for page in json.loads(listing)
        for item in page
        if not item.get("pull_request")
    }


def _write_issue(gh: Gh, title: str, text: str, number: int | None) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".md", encoding="utf-8") as body:
        body.write(text)
        body.flush()
        args = (
            ["issue", "create", "--title", title]
            if number is None
            else [
                "issue",
                "edit",
                str(number),
            ]
        )
        gh(*args, "--body-file", body.name)


def file_attention_issue(title: str, body: str, *, gh: Gh = run_gh) -> None:
    """Dedupe workflow issues, preserving the original failure when reporting fails later."""
    number = _open_issues(gh).get(title)
    if number is None:
        _write_issue(gh, title, body, None)
    else:
        api(gh, f"issues/{number}/comments", method="POST", body={"body": body})


def issue_title(harness: str, version: str) -> str:
    return f"Harness compatibility: {harness} {version}"


def issue_body(result: Result, *, policy: VersionPolicy, run_url: str) -> str:
    checks = failing_checks(result)
    return (
        "\n".join(
            [
                f"{result.harness} {result.version} ended **{result.verdict}**.",
                "",
                _MEANINGS[result.verdict],
                "",
                "This run proposes an exclusion pending human attention; "
                "failures do not move the range.",
                f"Policy: {policy.range_text}",
                f"Run: {run_url}",
                "",
                *[f"- {check}" for check in checks[:12]],
            ]
        )
        + "\n"
    )


def file_issues(run: Run, *, run_url: str, gh: Gh = run_gh) -> tuple[list[str], list[str]]:
    bad = [result for result in run.results if result.verdict != "green"]
    if not bad:
        return [], []
    known = _open_issues(gh)
    created: list[str] = []
    existing: list[str] = []
    failures: list[Exception] = []
    for result in bad:
        title = issue_title(run.harness, result.version)
        try:
            _write_issue(
                gh, title, issue_body(result, policy=run.policy, run_url=run_url), known.get(title)
            )
            (existing if title in known else created).append(title)
        except Exception as exc:
            failures.append(exc)
    if failures:
        raise ExceptionGroup("failure issue publication", failures)
    return created, existing


def pr_title(harness: str) -> str:
    return f"chore(compat): update {harness} version policy"


def pr_body(
    harness: str,
    old: VersionPolicy,
    proposal: VersionPolicy,
    *,
    run_url: str,
    max_range: int,
) -> str:
    return (
        f"{harness} policy: `{old.range_text}` to `{proposal.range_text}`\n\n"
        f"Floor: newest {max_range} releases in range (max-range={max_range}); the Nix full "
        "profile may ship older CLIs until flake.lock is bumped.\n\n"
        f"Run: {run_url}\n"
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
    gh: Gh, name: str, branch: str, main: str, head: str | None, proposal: VersionPolicy
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
            "message": pr_title(name),
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


def propose_policy(run: Run, *, run_url: str, gh: Gh, max_range: int) -> Published:
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
    proposal = derive(pending, run.results, discovered=run.discovered, max_range=max_range)
    check_change(format_policy(current), format_policy(proposal))
    number = prs[0]["number"] if prs else None
    if proposal != current or pending != current:
        if proposal != pending or head is None or main != merge_base:
            _commit_policy(gh, run.harness, branch, main, head, proposal)
        body = {
            "title": pr_title(run.harness),
            "body": pr_body(run.harness, current, proposal, run_url=run_url, max_range=max_range),
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
    try:
        created, existing = file_issues(run, run_url=run_url, gh=gh)
    except Exception as exc:
        failures.append(exc)
    try:
        outcome = propose_policy(run, run_url=run_url, gh=gh, max_range=max_range)
    except Exception as exc:
        failures.append(exc)
    if failures:
        raise ExceptionGroup("compatibility publication failed", failures)
    assert outcome is not None
    return Published(outcome.proposal, outcome.pr, tuple(created), tuple(existing))
