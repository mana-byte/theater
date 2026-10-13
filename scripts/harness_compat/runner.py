"""Candidate worktrees, qualification, and final composed bump."""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from .contract import (
    Adapter,
    Check,
    InstallFailed,
    RecordedRequest,
    Result,
    Verdict,
    verdict_from_checks,
)
from .evidence import adapter_digest, reusable_evidence, write_evidence
from .framework import check_bump, run_pytest
from .sandbox import make_sandbox
from .select import newest_in_range, plan_bump, select_candidates


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
        try:
            if bump:
                try:
                    adapter.apply_bump(root, [version])
                    problems = check_bump(adapter, root, [version])
                except Exception as exc:
                    problems = [f"bump failed: {exc}"]
                if problems:
                    return (
                        Result(
                            adapter.name,
                            version,
                            "tooling",
                            tuple(Check("check_bump", "fail", problem) for problem in problems),
                        ),
                        [],
                    )
            binary = adapter.install(sandbox, version)
            observed = adapter.qualify(root, version, sandbox, binary)
            relevant = tuple(
                check for check in observed.checks if check.name in adapter.green_tests
            )
            verdict: Verdict
            if any(
                check.status == "error"
                or (check.status == "skip" and check.name not in adapter.expected_skips)
                for check in relevant
            ):
                verdict = "inconclusive"
            else:
                verdict = verdict_from_checks(relevant)
            if len(relevant) != len(adapter.green_tests):
                verdict = "inconclusive"
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


def _compose(adapter: Adapter, root: Path, greens: list[str]) -> list[str]:
    try:
        with _worktree(root) as tree, make_sandbox(adapter.name) as sandbox:
            adapter.apply_bump(tree, greens)
            problems = check_bump(adapter, tree, greens)
            if problems:
                return problems
            checks = run_pytest(
                tree,
                adapter.consistency_tests,
                env=sandbox.env,
                expected_skips=adapter.expected_skips,
            )
            return [
                f"{check.name}: {check.status}: {check.detail}"
                for check in checks
                if check.status != "pass"
            ]
    except Exception as exc:
        return [f"composed bump failed: {exc}"]


def run_harness(
    adapter: Adapter,
    root: Path,
    *,
    version: str | None = None,
    max_candidates: int = 5,
    publish: bool = False,
    run_url: str = "local",
    evidence_branch: str | None = None,
) -> dict[str, Any]:
    """Qualify one harness; publish only after the composed tree passes."""
    gate = adapter.verified(root)
    digest = adapter_digest(root, adapter.name)
    reused = reusable_evidence(root, adapter.name, digest, branch=evidence_branch)
    candidates = select_candidates(
        adapter, gate, reused, max_candidates=max_candidates, version=version
    )
    rows: list[dict[str, Any]] = []
    results: list[Result] = []
    for candidate in candidates:
        with _worktree(root) as tree:
            result, routes = _qualify(adapter, tree, candidate, bump=True)
        result = replace(result, adapter_digest=digest)
        results.append(result)
        rows.append({**result.to_evidence(), "routes": routes, "run_url": run_url})
        if gate.kind == "range" and result.verdict != "green":
            break
    watch_version = newest_in_range(adapter, gate)
    watch: dict[str, Any] | None = None
    if watch_version is not None:
        with _worktree(root) as tree:
            watched, routes = _qualify(adapter, tree, watch_version, bump=False)
        watch = {**watched.to_evidence(), "routes": routes, "run_url": run_url}
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
        if not gate.admits(item)
    ]
    greens = plan_bump(gate, [*reused_results, *results])
    problems = _compose(adapter, root, greens) if greens else []
    if problems:
        rows.append(
            {
                "harness": adapter.name,
                "version": ",".join(greens),
                "verdict": "tooling",
                "checks": [
                    {"name": "consistency", "status": "fail", "detail": item} for item in problems
                ],
                "routes": [],
                "run_url": run_url,
            }
        )
    inconclusive = any(row["verdict"] == "inconclusive" for row in rows)
    inconclusive |= bool(watch and watch["verdict"] == "inconclusive")
    issues = [row for row in rows if row["verdict"] in ("red", "tooling")]
    if watch and watch["verdict"] in ("red", "tooling"):
        issues.append({**watch, "admitted_regression": watch["verdict"] == "red"})
    can_publish = publish and not inconclusive and not problems
    if can_publish and greens:
        adapter.apply_bump(root, greens)
        for result in [*reused_results, *results]:
            if result.version in greens:
                write_evidence(root, result, run_url)
    return {
        "harness": adapter.name,
        "adapter_digest": digest,
        "candidates": candidates,
        "reused": sorted(reused),
        "results": rows,
        "watch": watch,
        "greens": greens,
        "issues": issues if not inconclusive else [],
        "inconclusive": inconclusive,
        "published": bool(can_publish and greens),
        "run_url": run_url,
    }
