"""Verify edited compatibility gates with the plugin's real probe in a child process."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from .contract import Adapter, VerifiedSet, is_stable, version_key
from .process import run_group
from .stale import stale_references


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
        result = run_group(
            [sys.executable, "-c", code, module, function, str(stub)],
            cwd=root,
            env=env,
            timeout=30,
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
    newest = max(bumped, key=version_key)
    if version_key(gate.ceiling) > version_key(newest):
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


def check_bump_problems(adapter: Adapter, root: Path, bumped: Sequence[str]) -> list[str]:
    if not bumped:
        return ["no bumped versions"]
    problems: list[str] = []
    try:
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
        problems.extend(stale_references(adapter, root)[0])
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        problems.append(f"bump check errored: {exc}")
    return problems
