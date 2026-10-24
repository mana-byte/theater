"""Range qualification for the Codex native runtime."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import ClassVar

from tests.native.codex_qualify_runtime import BEHAVIOR_FILES, SCHEMA_FILES

from .. import framework, github
from ..contract import (
    Check,
    InstallFailed,
    Result,
    Sandbox,
    Verdict,
    VerifiedSet,
    is_stable,
    version_key,
)
from . import codex_bump

_FIXTURES = Path("tests/fixtures/codex_native_runtime")
_CANDIDATES = _FIXTURES / "candidates"
# The retained bundle a bump refreshes; installed_release.json names the binary it came from.
_LATEST = _FIXTURES / "latest"
_IDENTITY = "installed_release.json"
_MOCK_LOG = "codex_mock_requests.jsonl"
# These endpoints are telemetry side channels; their existing responses remain unchanged.
_BENIGN_MOCK_ROUTES = frozenset({("POST", "/v1/analytics/codex/turn-costs")})
_CONSISTENCY = (
    "tests/compat/test_codex.py",
    "tests/harness/codex/test_native_conformance.py::TestOfflineConformanceBundle",
    "tests/harness/codex/test_native_conformance.py::TestBundleIndex",
)
_UI_CONTROL = (
    "tests/integration/codex/test_native_ui_bootstrap.py::"
    "test_codex_native_ui_starts_a_thread_unattended[update-available-control-False]"
)
_GREEN = (
    "tests/integration/codex/test_native_smoke.py::TestNativeSmokeHandshakeAndFraming",
    "tests/integration/codex/test_native_smoke.py::TestNativeSmokeSharedUiSession",
    "tests/integration/codex/test_native_smoke.py::TestNativeSmokeTurnControls",
    "tests/integration/codex/test_native_smoke.py::TestNativeSmokeForkReconnectSurvival",
    "tests/integration/codex/test_native_smoke.py::TestNativeSmokeRemoteNecessity",
    "tests/integration/codex/test_native_ui_bootstrap.py::test_codex_native_ui_bootstrap_proof",
    "tests/integration/codex/test_native_ui_bootstrap.py::"
    "test_codex_native_ui_starts_a_thread_unattended[update-available-True]",
    _UI_CONTROL,
    "tests/integration/codex/test_native_ui_bootstrap.py::"
    "test_codex_native_ui_starts_a_thread_unattended[worktree-of-trusted-repo-True]",
)


def _complete_bundle(path: Path) -> bool:
    expected = set(BEHAVIOR_FILES) | {f"protocol_schema/{name}" for name in SCHEMA_FILES}
    actual = {item.relative_to(path).as_posix() for item in path.rglob("*") if item.is_file()}
    return actual == expected


def _comparable(bundle: Path) -> dict[str, bytes]:
    """The bundle's evidence bytes; the capture identity differs on every release by design."""
    return {
        path.relative_to(bundle).as_posix(): path.read_bytes()
        for path in sorted(bundle.rglob("*"))
        if path.is_file() and path.name != _IDENTITY
    }


def _refresh_latest(root: Path, version: str) -> list[Path]:
    """Copy a captured bundle over ``latest`` only when its evidence really differs."""
    staged, latest = root / _CANDIDATES / version, root / _LATEST
    if not staged.is_dir() or not _complete_bundle(staged):
        return []
    if latest.is_dir() and _comparable(staged) == _comparable(latest):
        return []
    changed: list[Path] = []
    for path in sorted(item for item in staged.rglob("*") if item.is_file()):
        target = latest / path.relative_to(staged)
        if not target.is_file() or target.read_bytes() != path.read_bytes():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            changed.append(target.relative_to(root))
    return changed


def _consume(root: Path, version: str) -> None:
    """Drop a composed candidate; it is untracked scratch, never part of the staged change."""
    shutil.rmtree(root / _CANDIDATES / version, ignore_errors=True)
    with contextlib.suppress(OSError):
        (root / _CANDIDATES).rmdir()


def _classify_mock_routes(result: Result, log: Path) -> Result:
    if result.verdict == "green" or not log.is_file():
        return result
    try:
        entries = [json.loads(line) for line in log.read_text().splitlines()]
        unscripted = {
            (entry["method"], entry["path"]) for entry in entries if entry.get("scripted") is False
        }
    except (OSError, ValueError, KeyError, TypeError) as error:
        check = Check("mock_routes", "fail", f"invalid private mock log: {error}")
        return replace(result, verdict="tooling", checks=(*result.checks, check))
    unexpected = sorted(unscripted - _BENIGN_MOCK_ROUTES)
    if not unexpected:
        return result
    routes = [f"{method} {path}" for method, path in unexpected]
    check = Check("mock_routes", "fail", f"unexpected private mock routes: {routes}")
    return replace(result, verdict="tooling", checks=(*result.checks, check))


def _public_git_tags() -> list[str]:
    with tempfile.TemporaryDirectory(prefix="codex-release-tags-") as home:
        env = {
            "HOME": home,
            "PATH": os.environ.get("PATH", ""),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_ASKPASS": os.devnull,
        }
        done = subprocess.run(
            [
                "git",
                "-c",
                "credential.helper=",
                "ls-remote",
                "--tags",
                "https://github.com/openai/codex.git",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    if done.returncode:
        raise RuntimeError(f"Codex public tags unavailable: {done.stderr[-400:]}")
    prefix = "refs/tags/rust-v"
    versions: set[str] = set()
    for line in done.stdout.splitlines():
        _, separator, ref = line.partition("\t")
        if separator and ref.startswith(prefix):
            version = ref.removeprefix(prefix)
            if is_stable(version):
                versions.add(version)
    if not versions:
        raise RuntimeError("Codex public tags response has no stable rust-v releases")
    return sorted(versions, key=version_key)


class CodexAdapter:
    name = "codex"
    probe = "theater.harness.builtin.plugins.codex.runtime_plan:probe_codex_compatibility"
    stub_help = ""
    green_tests = _GREEN
    consistency_tests = _CONSISTENCY
    #: Where a qualification leaves the bundle it captured; the runner ships nothing else.
    artifact_roots = (_CANDIDATES,)
    expected_skips: ClassVar[dict[str, str]] = {
        _UI_CONTROL: "downloaded binaries may not show Codex's update dialog"
    }
    # The shared mock sees no Codex traffic; the collector audits its private mock routes.
    # Both proof mocks log calls; bootstrap asserts the exact scripted request count.
    expected_routes: frozenset[tuple[str, str]] = frozenset()
    ignorable_routes: frozenset[tuple[str, str]] = frozenset()
    shadow_suite = False

    def discover(self) -> list[str]:
        try:
            return github.github_release_versions("openai/codex", tag_prefix="rust-v")
        except urllib.error.URLError:
            return _public_git_tags()

    def verified(self, root: Path) -> VerifiedSet:
        return codex_bump.verified(root)

    def install(self, sb: Sandbox, version: str) -> Path:
        command = [
            sys.executable,
            "-m",
            "tests.native.codex_fetch_release",
            "--version",
            version,
            "--dest",
            str(sb.bin_dir),
        ]
        try:
            done = subprocess.run(
                command,
                cwd=Path(__file__).resolve().parents[3],
                env=dict(sb.env),
                capture_output=True,
                text=True,
                timeout=900,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise InstallFailed(f"Codex {version} download failed: {error}") from error
        binary = sb.bin_dir / "codex"
        if done.returncode or not binary.is_file():
            raise InstallFailed(f"Codex {version} download failed: {done.stderr[-400:]}")
        return binary

    def version_line(self, version: str) -> str:
        return f"codex-cli {version}"

    def apply_bump(self, root: Path, greens: Sequence[str]) -> list[Path]:
        changed = codex_bump.apply_bump(root, greens)
        if greens:
            # Only the newest green may refresh ``latest``; the rest are just discarded.
            changed.extend(_refresh_latest(root, max(greens, key=version_key)))
        for version in greens:
            _consume(root, version)
        return sorted(set(changed))

    def qualify(self, root: Path, version: str, sb: Sandbox, binary: Path) -> Result:
        bundle = sb.evidence_dir / version
        mock_log = sb.evidence_dir / _MOCK_LOG
        command = [
            sys.executable,
            "-m",
            "tests.native.codex_qualify_runtime",
            "--out",
            str(bundle),
            "--codex",
            str(binary),
            "--capture-behavior",
            "--provider",
            "mock",
        ]
        common_env = {
            "CODEX_HOME": str(sb.home / ".codex"),
            "THEATER_CODEX_QUALIFY_VERSION": version,
            "THEATER_CODEX_NATIVE_PROOF": "1",
            "THEATER_CODEX_NATIVE_PROVIDER": "mock",
            "THEATER_CODEX_NATIVE_BIN": str(binary),
            "THEATER_CODEX_NATIVE_SMOKE": "1",
            "THEATER_COMPAT_MOCK_LOG": str(mock_log),
            **{k: v for k, v in os.environ.items() if k == "THEATER_COMPAT_DIAGNOSTICS"},
        }
        capture_env = {**sb.offline_env, **common_env}
        # The UI's update check may need network; model calls stay on local mocks.
        proof_env = {**sb.env, **common_env}
        try:
            done = subprocess.run(
                command,
                cwd=root,
                env=capture_env,
                capture_output=True,
                text=True,
                timeout=900,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            # A slow collector is infrastructure noise worth a retry; a missing one is ours.
            slow: Verdict = (
                "inconclusive" if isinstance(error, subprocess.TimeoutExpired) else "tooling"
            )
            return _classify_mock_routes(
                Result(
                    self.name,
                    version,
                    slow,
                    (Check("collect_bundle", "error", str(error)),),
                ),
                mock_log,
            )
        if done.returncode or not _complete_bundle(bundle):
            detail = (done.stderr or done.stdout)[-400:]
            return _classify_mock_routes(
                Result(self.name, version, "tooling", (Check("collect_bundle", "error", detail),)),
                mock_log,
            )
        staged = root / _CANDIDATES / version
        staged.parent.mkdir(parents=True, exist_ok=True)
        if staged.exists():
            shutil.rmtree(staged)
        shutil.copytree(bundle, staged)
        # Kept untracked: the runner captures and caches it; a tracked edit would be lost.
        _refresh_latest(root, version)

        consistency = framework.run_pytest(
            root, self.consistency_tests, env=capture_env, expected_skips={}, timeout=900
        )
        if not consistency or any(check.status != "pass" for check in consistency):
            return _classify_mock_routes(
                Result(self.name, version, "tooling", consistency, sb.evidence_dir), mock_log
            )
        green = framework.run_pytest(
            root, self.green_tests, env=proof_env, expected_skips=self.expected_skips, timeout=900
        )
        if any(check.status == "fail" for check in green):
            verdict: Verdict = "red"  # an explicit failure outranks a setup error beside it
        elif (
            not green
            or any(check.status == "error" for check in green)
            or any(
                check.status == "skip" and check.name not in self.expected_skips for check in green
            )
            or not any(check.status == "pass" for check in green)
        ):
            verdict = "inconclusive"
        else:
            verdict = "green"
        result = Result(self.name, version, verdict, (*consistency, *green), sb.evidence_dir)
        return _classify_mock_routes(result, mock_log)


ADAPTER = CodexAdapter()
