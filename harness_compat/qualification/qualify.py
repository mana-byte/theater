"""Qualify one supplied release: install it, then let the live suite alone decide."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

from theater.version_policy import version_key

from ..contract import Adapter, Check, InstallFailed, Result, Sandbox
from ..diagnostics import clip_diagnostic, exception_detail
from ..policy import admit_only, load_policy
from .cleanup import CleanupFailed
from .codecopy import code_copy
from .sandbox import make_sandbox
from .suite_runner import installed_version, run_suite, verdict


def qualify_binary(
    root: Path,
    sb: Sandbox,
    name: str,
    binary: Path,
    version: str,
    *,
    pytest_args: Sequence[str] = (),
) -> Result:
    """Run the full live suite for ``binary`` in a disposable copy of ``root``.

    A release the policy does not admit would be refused by the plugin's own gate, which says
    nothing about behaviour: the copy admits just this release, and only the copy. Only a whole,
    unfiltered run can be green; suite reconciliation enforces that rule.
    """
    version_key(version)
    binary = binary.resolve()
    checks: tuple[Check, ...] = ()
    try:
        with code_copy(root) as tree:
            if not load_policy(tree, name).admits(version):
                admit_only(tree, name, version)
            report = run_suite(
                tree,
                sb,
                harness=name,
                binary=binary,
                version=version,
                pytest_args=pytest_args,
            )
            checks = report.checks
    except CleanupFailed as exc:
        exc.checks = checks or exc.checks
        raise
    except Exception as exc:
        detail = clip_diagnostic(exception_detail(exc), 400, tail=True)
        return Result(name, version, "tooling", (Check("qualification", "error", detail),))
    return Result(name, version, verdict(report), report.checks)


def install_verified(adapter: Adapter, sb: Sandbox, version: str) -> Path:
    """Install exactly ``version`` and prove the binary reports that very release."""
    binary = adapter.install(sb, version)
    try:
        reported = installed_version(binary)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise InstallFailed(f"{adapter.name} {version} cannot report its version: {exc}") from exc
    if reported != version:
        raise InstallFailed(f"{adapter.name} {version} reports {reported} for --version")
    return binary


def qualify_release(
    root: Path, adapter: Adapter, version: str, *, pytest_args: Sequence[str] = ()
) -> Result:
    """Install exactly ``version``, check what the binary reports, and run the live suite on it."""
    version_key(version)
    result: Result | None = None
    try:
        with make_sandbox(adapter.name) as sb:
            try:
                binary = install_verified(adapter, sb, version)
            except (InstallFailed, OSError, subprocess.SubprocessError) as exc:
                detail = clip_diagnostic(exception_detail(exc), 500, tail=True)
                result = Result(
                    adapter.name, version, "inconclusive", (Check("install", "error", detail),)
                )
            else:
                result = qualify_binary(
                    root, sb, adapter.name, binary, version, pytest_args=pytest_args
                )
    except CleanupFailed as exc:
        if result is not None:
            exc.checks = result.checks
        elif isinstance(exc.__context__, CleanupFailed):
            exc.checks = exc.__context__.checks
        raise
    assert result is not None
    return result
