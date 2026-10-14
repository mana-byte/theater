"""Verify edited compatibility gates with the plugin's real probe in a child process."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from .artifacts import tracked_changes, untracked_files
from .contract import EVIDENCE_ROOT, Adapter, VerifiedSet, is_stable, version_key


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, timeout=30, check=False
    )


def _probe(adapter: Adapter, root: Path, version: str, stub: Path) -> tuple[bool | None, str]:
    line = shlex.quote(adapter.version_line(version))
    help_text = shlex.quote(adapter.stub_help)
    stub.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--help" ] || [ "$2" = "--help" ]; then '
        f"printf '%s\\n' {help_text}; else printf '%s\\n' {line}; fi\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    module, sep, function = adapter.probe.partition(":")
    if not sep or not module or not function:
        return None, f"invalid probe path: {adapter.probe}"
    code = (
        "import importlib,json,sys; "
        "from theater.harness.contracts.runtime import RuntimeProbeContext; "
        "probe=getattr(importlib.import_module(sys.argv[1]),sys.argv[2]); "
        "result=probe(RuntimeProbeContext(binary=sys.argv[3])); "
        "print(json.dumps({'supported':result.supported,'reason':result.reason}))"
    )
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(root), "HOME": str(stub.parent)}
    try:
        result = subprocess.run(
            [sys.executable, "-c", code, module, function, str(stub)],
            cwd=root,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if result.returncode:
        return None, (result.stderr or result.stdout)[-500:]
    try:
        data = json.loads(result.stdout.strip().splitlines()[-1])
        return bool(data["supported"]), str(data.get("reason") or "")
    except (ValueError, IndexError, KeyError, TypeError) as exc:
        return None, f"invalid probe result: {exc}"


def _next_release(adapter: Adapter, bumped: Sequence[str], gate: VerifiedSet) -> str:
    newest = max([*bumped, *gate.versions], key=version_key)
    if gate.ceiling and version_key(gate.ceiling) > version_key(newest):
        return gate.ceiling
    future = [
        version
        for version in adapter.discover()
        if is_stable(version) and version_key(version) > version_key(newest)
    ]
    if future:
        return min(future, key=version_key)
    major, minor, patch = version_key(newest)
    return f"{major}.{minor}.{patch + 1}"


def _limit_tokens(gate: VerifiedSet) -> set[str]:
    if gate.kind != "range" or gate.ceiling is None:
        return set()
    major, minor, patch = version_key(gate.ceiling)
    tokens = {gate.ceiling}
    if patch > 0:
        tokens.add(f"{major}.{minor}.{patch - 1}")
    return tokens


def _version_in_text(version: str, value: str) -> bool:
    return re.search(rf"(?<![\d.]){re.escape(version)}(?![\d.])", value) is not None


def _old_limit_problems(adapter: Adapter, root: Path, new_gate: VerifiedSet) -> list[str]:
    with tempfile.TemporaryDirectory(prefix="compat-old-gate-") as temporary:
        old_root = Path(temporary) / "tree"
        added = _git(root, "worktree", "add", "--detach", str(old_root), "HEAD")
        if added.returncode:
            return [f"cannot read old gate: {added.stderr.strip()}"]
        try:
            old = adapter.verified(old_root)
            stale = _limit_tokens(old) - _limit_tokens(new_gate)
            if not stale:
                return []
            paths = _git(root, "diff", "--name-only", "HEAD", "--").stdout.splitlines()
            paths.extend(
                _git(root, "ls-files", "--others", "--exclude-standard").stdout.splitlines()
            )
            return [
                f"old limit {token} remains in {name}"
                for name in sorted(set(paths))
                if (root / name).is_file()
                for token in sorted(stale, key=version_key)
                if _version_in_text(
                    token, (root / name).read_text(encoding="utf-8", errors="replace")
                )
            ]
        finally:
            _git(root, "worktree", "remove", "--force", str(old_root))


def evidence_only_bump(
    adapter: Adapter,
    root: Path,
    bumped: Sequence[str],
    changed: Sequence[Path] | None = None,
) -> tuple[bool, list[str]]:
    """Recognize evidence-only admissions while rejecting any unrelated edit."""
    expected = {EVIDENCE_ROOT / adapter.name / f"{version}.json" for version in bumped}
    if not expected:
        return False, []
    actual = tracked_changes(root) | untracked_files(root)
    returned = set(changed or ())
    reported_only_evidence = bool(returned) and all(
        path.is_relative_to(EVIDENCE_ROOT) for path in returned
    )
    if changed is not None and returned and not reported_only_evidence:
        return False, []
    if not reported_only_evidence and actual != expected:
        gate = adapter.verified(root)
        if (
            changed is not None
            or not expected <= actual
            or any(gate.admits(version) for version in bumped)
        ):
            return False, []
    if extra := actual - expected:
        return False, [f"evidence-only bump changed unrelated files: {sorted(extra)}"]
    if missing := expected - actual:
        return False, [f"evidence-only bump missing evidence changes: {sorted(missing)}"]
    if not all((root / path).is_file() for path in expected):
        return False, ["evidence-only bump did not write every evidence file"]
    return True, []


def check_bump_problems(adapter: Adapter, root: Path, bumped: Sequence[str]) -> list[str]:
    if not bumped:
        return ["no bumped versions"]
    problems: list[str] = []
    try:
        evidence_only, problems = evidence_only_bump(adapter, root, bumped)
        if evidence_only or problems:
            return problems
        gate = adapter.verified(root)
        next_version = _next_release(adapter, bumped, gate)
        with tempfile.TemporaryDirectory(prefix="compat-probe-") as temporary:
            stub = Path(temporary) / "compat-stub"
            for version in [*bumped, next_version]:
                supported, detail = _probe(adapter, root, version, stub)
                expected = version != next_version
                if supported is None:
                    problems.append(f"probe {version} errored: {detail}")
                elif supported != expected:
                    problems.append(
                        f"probe {version}: supported={supported}, expected={expected}: {detail}"
                    )
        problems.extend(_old_limit_problems(adapter, root, gate))
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        problems.append(f"bump check errored: {exc}")
    return problems
