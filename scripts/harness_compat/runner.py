"""Candidate worktrees, qualification, and final composed bump."""

from __future__ import annotations

import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any

from .artifacts import CandidateArtifacts, stage_composed, tracked_patch
from .contract import (
    Adapter,
    Check,
    InstallFailed,
    RecordedRequest,
    Result,
    Verdict,
    VerifiedSet,
    verdict_from_checks,
)
from .diagnostics import clip_diagnostic
from .evidence import (
    adapter_digest,
    cache_artifacts_dir,
    drop_evidence,
    reusable_evidence,
    write_evidence,
)
from .framework import check_bump, run_pytest
from .sandbox import make_sandbox
from .select import newest_in_range, plan_bump, select_candidates
from .stale import stale_warnings

#: Infrastructure noise gets a whole fresh unit again; a red or tooling verdict never does.
MAX_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (30.0, 90.0)
#: Wall-clock budget of one harness run. The first attempt of every unit always runs, so the
#: workflow's job timeout (90 min = budget + reserve) is the hard bound, not this value.
RUN_BUDGET_SECONDS = 70 * 60
#: What one attempt may need (a pytest invocation may take 15 min); nothing new starts without it.
ATTEMPT_RESERVE_SECONDS = 20 * 60


def _timeouts_only(checks: tuple[Check, ...]) -> bool:
    """True when every non-passing check merely timed out: no deterministic failure is hidden."""
    bad = [check for check in checks if check.status in ("error", "fail")]
    return bool(bad) and all(
        check.status == "error" and "timed out" in check.detail.lower() for check in bad
    )


def _cause(result: Result) -> str:
    check = next((item for item in result.checks if item.status in ("error", "fail")), None)
    return clip_diagnostic(f"{check.name}: {check.detail}", 200, tail=True) if check else ""


def _retry[T](
    attempt: Callable[[], tuple[Result, T]], deadline: float
) -> tuple[Result, T, list[dict[str, Any]]]:
    """Run ``attempt`` until it is conclusive, at most ``MAX_ATTEMPTS`` times within budget."""
    log: list[dict[str, Any]] = []
    for number in range(1, MAX_ATTEMPTS + 1):
        result, extra = attempt()
        log.append({"attempt": number, "verdict": result.verdict, "cause": _cause(result)})
        if result.verdict != "inconclusive" or number == MAX_ATTEMPTS:
            break
        if not _backoff(number, deadline):
            log.append({"attempt": number + 1, "verdict": "not run", "cause": "run budget spent"})
            break
    return result, extra, log


_sleep = time.sleep  # a seam: tests patch this, never the process-wide time.sleep


def _backoff(number: int, deadline: float) -> bool:
    """Wait before attempt ``number + 1``; False when the run budget cannot fit another one."""
    delay = RETRY_BACKOFF_SECONDS[number - 1]
    if time.monotonic() + delay + ATTEMPT_RESERVE_SECONDS > deadline:
        return False
    _sleep(delay)
    return True


@contextmanager
def _worktree(root: Path) -> Iterator[Path]:
    with tempfile.TemporaryDirectory(prefix="compat-tree-") as temporary:
        tree = Path(temporary) / "source"
        added = subprocess.run(
            ["git", "worktree", "add", "--detach", str(tree), "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if added.returncode:
            raise RuntimeError(f"git worktree add failed: {added.stderr.strip()}")
        try:
            yield tree
        finally:
            removed = subprocess.run(
                ["git", "worktree", "remove", "--force", str(tree)],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if removed.returncode:
                raise RuntimeError(f"git worktree remove failed: {removed.stderr.strip()}")


def _routes(adapter: Adapter, requests: tuple[RecordedRequest, ...]) -> list[dict[str, Any]]:
    return [
        {
            "method": item.method,
            "path": item.path,
            "body_keys": list(item.body_keys),
            "handled": item.handled,
            "unexpected": (item.method, item.path) not in adapter.expected_routes,
            "ignorable": (item.method, item.path) in adapter.ignorable_routes,
        }
        for item in requests
    ]


def classify_routes(
    adapter: Adapter, result: Result, requests: tuple[RecordedRequest, ...]
) -> Result:
    """A model route gap explains a failing proof as tooling, never upstream red."""
    if result.verdict == "green":
        return result
    bad = [
        item
        for item in requests
        if (item.method, item.path) not in adapter.ignorable_routes
        and (not item.handled or (item.method, item.path) not in adapter.expected_routes)
    ]
    if not bad:
        return result
    details = "; ".join(
        f"{item.method} {item.path} body keys={','.join(item.body_keys)}" for item in bad
    )
    return replace(
        result, verdict="tooling", checks=(*result.checks, Check("mock routes", "fail", details))
    )


def _qualify(
    adapter: Adapter, root: Path, version: str, *, bump: bool
) -> tuple[Result, list[dict[str, Any]]]:
    with make_sandbox(adapter.name) as sandbox:
        verdict: Verdict
        try:
            if bump:
                try:
                    changed = adapter.apply_bump(root, [version])
                    problems = check_bump(adapter, root, [version]) if changed else []
                except Exception as exc:
                    problems = [f"bump failed: {exc}"]
                if problems:
                    verdict = (
                        "inconclusive"
                        if any("timed out" in problem.lower() for problem in problems)
                        else "tooling"
                    )
                    return (
                        Result(
                            adapter.name,
                            version,
                            verdict,
                            tuple(
                                Check(
                                    "check_bump",
                                    "error" if verdict == "inconclusive" else "fail",
                                    problem,
                                )
                                for problem in problems
                            ),
                        ),
                        [],
                    )
            binary = adapter.install(sandbox, version)
            observed = adapter.qualify(root, version, sandbox, binary)
            relevant = tuple(
                check for check in observed.checks if check.name in adapter.green_tests
            )
            if observed.verdict == "tooling" and _timeouts_only(observed.checks):
                verdict = "inconclusive"  # a hung consistency run says nothing about the bump
            elif observed.verdict in ("tooling", "inconclusive"):
                verdict = observed.verdict
            elif observed.verdict == "red" and any(c.status == "fail" for c in observed.checks):
                verdict = "red"  # an early conclusive failure needs no further proofs
            elif len(relevant) != len(adapter.green_tests) or any(
                check.status == "error"
                or (check.status == "skip" and check.name not in adapter.expected_skips)
                for check in relevant
            ):
                verdict = "inconclusive"
            else:
                verdict = verdict_from_checks(relevant)
            result = replace(observed, verdict=verdict)
        except InstallFailed as exc:
            result = Result(
                adapter.name, version, "inconclusive", (Check("install", "error", str(exc)),)
            )
        except Exception as exc:
            result = Result(
                adapter.name, version, "inconclusive", (Check("qualification", "error", str(exc)),)
            )
        requests = sandbox.mock.requests
        return classify_routes(adapter, result, requests), _routes(adapter, requests)


def _compose(
    adapter: Adapter,
    root: Path,
    greens: list[str],
    artifacts: CandidateArtifacts,
    *,
    publish: bool,
) -> tuple[list[str], bool, bool, list[str]]:
    try:
        with _worktree(root) as tree, make_sandbox(adapter.name) as sandbox:
            artifacts.copy_into(tree, greens)
            changed = adapter.apply_bump(tree, greens)
            if not changed:
                return [], False, False, []
            if stray := artifacts.stray_files(tree, changed):
                return stray, False, False, []
            problems = check_bump(adapter, tree, greens)
            if problems:
                # Noise only when every problem is a timeout; one deterministic problem wins.
                return (
                    problems,
                    all("timed out" in problem.lower() for problem in problems),
                    False,
                    [],
                )
            warnings = stale_warnings(adapter, tree, greens)
            patch_before_tests = tracked_patch(tree)
            checks = run_pytest(
                tree,
                adapter.consistency_tests,
                env=sandbox.env,
                expected_skips=adapter.expected_skips,
            )
            bad = [check for check in checks if check.status not in ("pass", "skip")]
            problems = [f"{check.name}: {check.status}: {check.detail}" for check in bad]
            deterministic: list[str] = []
            if tracked_patch(tree) != patch_before_tests:
                deterministic.append("consistency tests changed tracked files")
            deterministic.extend(artifacts.stray_files(tree, changed))
            problems.extend(deterministic)
            if problems:
                # Noise only when every failing check merely errored: a fail always wins.
                noisy = not deterministic and all(check.status == "error" for check in bad)
                return problems, noisy, False, warnings
            if publish:
                try:
                    stage_composed(tree, root, {*changed, *artifacts.owners})
                except Exception as exc:
                    # Deterministic (dirty checkout, undeclared paths): ours, never noise.
                    return [f"staging failed: {exc}"], False, False, warnings
                return [], False, True, warnings
            return [], False, False, warnings
    except Exception as exc:
        return [f"composed bump failed: {exc}"], False, False, []


def _compose_retrying(
    adapter: Adapter,
    root: Path,
    greens: list[str],
    artifacts: CandidateArtifacts,
    *,
    publish: bool,
    deadline: float,
) -> tuple[tuple[list[str], bool, bool, list[str]], list[dict[str, Any]]]:
    """``_compose`` again while its only failure is infrastructure noise, with its history."""
    log: list[dict[str, Any]] = []
    for number in range(1, MAX_ATTEMPTS + 1):
        outcome = _compose(adapter, root, greens, artifacts, publish=publish)
        problems, noisy = outcome[0], outcome[1]
        verdict = "inconclusive" if noisy else "tooling" if problems else "green"
        log.append(
            {"attempt": number, "verdict": verdict, "cause": problems[0] if problems else ""}
        )
        if not noisy or number == MAX_ATTEMPTS or not _backoff(number, deadline):
            return outcome, log
    raise AssertionError("unreachable")


def _candidate_attempt(
    adapter: Adapter, root: Path, candidate: str, artifacts: CandidateArtifacts
) -> tuple[Result, tuple[list[dict[str, Any]], list[str], list[str]]]:
    """One fresh attempt: qualify in its own tree and capture a green's generated files."""
    warnings: list[str] = []
    conflicts: list[str] = []
    with _worktree(root) as tree:
        result, routes = _qualify(adapter, tree, candidate, bump=True)
        if result.verdict in ("green", "red"):
            warnings = stale_warnings(adapter, tree, [candidate])
        if result.verdict == "green":
            conflicts = artifacts.capture(tree, candidate)
            if conflicts:
                result = replace(
                    result,
                    verdict="tooling",
                    checks=(*result.checks, Check("artifacts", "fail", "; ".join(conflicts))),
                )
    return result, (routes, warnings, conflicts)


def _watch(
    adapter: Adapter,
    root: Path,
    gate: VerifiedSet,
    releases: list[str],
    rows: list[dict[str, Any]],
    run_url: str,
    digest: str,
    deadline: float,
) -> tuple[dict[str, Any] | None, bool]:
    """The daily regression watch: reuse a candidate row for the newest admitted release."""
    version = newest_in_range(adapter, gate, releases)
    if version is None:
        return None, False
    if row := next((dict(item) for item in rows if item["version"] == version), None):
        return row, True

    def attempt() -> tuple[Result, list[dict[str, Any]]]:
        with _worktree(root) as tree:
            return _qualify(adapter, tree, version, bump=False)

    watched, routes, log = _retry(attempt, deadline)
    if watched.verdict == "green":
        write_evidence(root, replace(watched, adapter_digest=digest), run_url)
    else:
        drop_evidence(root, adapter.name, version)
    row = {**watched.to_evidence(), "routes": routes, "run_url": run_url, "attempts": log}
    return row, False


def _walk_candidates(
    adapter: Adapter,
    root: Path,
    gate: VerifiedSet,
    candidates: list[str],
    artifacts: CandidateArtifacts,
    reused: dict[str, dict[str, Any]],
    digest: str,
    run_url: str,
    deadline: float,
) -> tuple[list[Result], list[dict[str, Any]], list[str], list[str], list[str]]:
    """Qualify candidates in ascending order: results, rows, conflicts, warnings, deferred."""
    results: list[Result] = []
    rows: list[dict[str, Any]] = []
    conflicts_seen: list[str] = []
    warnings_seen: list[str] = []
    deferred: list[str] = []
    for index, candidate in enumerate(candidates):
        if index and time.monotonic() + ATTEMPT_RESERVE_SECONDS > deadline:
            deferred = candidates[index:]
            break
        result, (routes, warnings, conflicts), log = _retry(
            partial(_candidate_attempt, adapter, root, candidate, artifacts), deadline
        )
        warnings_seen.extend(warnings)
        conflicts_seen.extend(conflicts)
        result = replace(result, adapter_digest=digest)
        if result.verdict == "green":
            cached = artifacts.cache_version(
                candidate, cache_artifacts_dir(root, adapter.name, candidate)
            )
            write_evidence(root, result, run_url, artifacts=cached)
        else:
            drop_evidence(root, adapter.name, candidate)
            reused.pop(candidate, None)
        results.append(result)
        rows.append({**result.to_evidence(), "routes": routes, "run_url": run_url, "attempts": log})
        # A candidate that was ever inconclusive ends the walk: the backlog waits for tomorrow.
        if gate.kind == "range" and (result.verdict != "green" or len(log) > 1):
            deferred = candidates[index + 1 :]
            break
    return results, rows, conflicts_seen, warnings_seen, deferred


def _run_with_artifacts(
    adapter: Adapter,
    root: Path,
    artifacts: CandidateArtifacts,
    *,
    version: str | None = None,
    max_candidates: int = 5,
    publish: bool = False,
    run_url: str = "local",
    budget_seconds: float = RUN_BUDGET_SECONDS,
) -> dict[str, Any]:
    """Qualify one harness; publish only after the composed tree passes."""
    deadline = time.monotonic() + budget_seconds
    gate = adapter.verified(root)
    digest = adapter_digest(root, adapter.name, adapter)
    reused = reusable_evidence(
        root,
        adapter.name,
        digest,
        required=adapter.green_tests,
        skips=adapter.expected_skips,
    )
    releases = adapter.discover()
    candidates = select_candidates(
        adapter, gate, reused, max_candidates=max_candidates, version=version, releases=releases
    )
    results, rows, artifact_conflicts, candidate_warnings, deferred = _walk_candidates(
        adapter, root, gate, candidates, artifacts, reused, digest, run_url, deadline
    )
    watch, watch_reused = _watch(adapter, root, gate, releases, rows, run_url, digest, deadline)
    reused_results = [
        Result(
            adapter.name,
            item,
            "green",
            tuple(
                Check(check["name"], check["status"], check.get("detail", ""))
                for check in document.get("checks", [])
            ),
            adapter_digest=digest,
        )
        for item, document in reused.items()
        if not gate.admits(item) and item not in candidates
    ]
    greens = plan_bump(gate, [*reused_results, *results], releases)
    for result in reused_results:
        if result.version in greens:
            document = reused[result.version]
            artifacts.add_cached(
                result.version,
                cache_artifacts_dir(root, adapter.name, result.version),
                document.get("artifacts", {}),
            )
    warnings: list[str]
    compose_log: list[dict[str, Any]] = []
    if artifact_conflicts:
        problems, compose_inconclusive, published, warnings = (
            artifact_conflicts,
            False,
            False,
            [],
        )
    elif greens:
        (problems, compose_inconclusive, published, warnings), compose_log = _compose_retrying(
            adapter, root, greens, artifacts, publish=publish, deadline=deadline
        )
    else:
        problems, compose_inconclusive, published, warnings = (
            [],
            False,
            False,
            [],
        )
    if problems:
        rows.append(
            {
                "harness": adapter.name,
                "version": greens[-1] if greens else candidates[-1],
                "verdict": "inconclusive" if compose_inconclusive else "tooling",
                "checks": [
                    {
                        "name": "consistency",
                        "status": "error" if compose_inconclusive else "fail",
                        "detail": item,
                    }
                    for item in problems
                ],
                "routes": [],
                "run_url": run_url,
                "role": "composition",
                "attempts": compose_log,
            }
        )
    warnings = sorted(set(candidate_warnings) | set(warnings))
    inconclusive = any(row["verdict"] == "inconclusive" for row in rows)
    inconclusive |= bool(watch and watch["verdict"] == "inconclusive")
    failing = ("red", "tooling", "inconclusive")
    issues = [row for row in rows if row["verdict"] in failing]
    if watch and not watch_reused and watch["verdict"] in failing:
        issues.append({**watch, "admitted_regression": watch["verdict"] == "red"})
    return {
        "harness": adapter.name,
        "gate_kind": gate.kind,
        "gate": {
            "floor": gate.floor,
            "ceiling": gate.ceiling,
            "known_bad": sorted(gate.known_bad),
        },
        "adapter_digest": digest,
        "candidates": candidates,
        "deferred": deferred,
        # Candidates that ended non-green: an open gate PR that lists one is withdrawn.
        "revoked": sorted(
            {
                row["version"]
                for row in rows
                if row["verdict"] != "green"
                and row.get("role") != "composition"
                and not gate.admits(row["version"])
            }
        ),
        "composition_attempts": compose_log,
        "reused": sorted(reused),
        "reused_results": [
            {
                **result.to_evidence(),
                "routes": [],
                "run_url": reused[result.version].get("run_url", run_url),
            }
            for result in reused_results
        ],
        "results": rows,
        "watch": watch,
        "watch_reused": watch_reused,
        "greens": greens,
        "issues": issues,
        "inconclusive": inconclusive,
        "published": published,
        "stale_warnings": warnings,
        "run_url": run_url,
    }


def run_harness(
    adapter: Adapter,
    root: Path,
    *,
    version: str | None = None,
    max_candidates: int = 5,
    publish: bool = False,
    run_url: str = "local",
) -> dict[str, Any]:
    """Qualify one harness; publish only after the composed tree passes."""
    with tempfile.TemporaryDirectory(prefix="compat-artifacts-") as temporary:
        return _run_with_artifacts(
            adapter,
            root,
            # An adapter that generates files names where; every other path is dropped.
            CandidateArtifacts(Path(temporary), getattr(adapter, "artifact_roots", ())),
            version=version,
            max_candidates=max_candidates,
            publish=publish,
            run_url=run_url,
        )
