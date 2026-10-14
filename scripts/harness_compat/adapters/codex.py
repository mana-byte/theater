"""Exact-release qualification for the Codex native runtime."""

from __future__ import annotations

import ast
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
    load_evidenced,
    version_key,
)

_PLAN = Path("theater/harness/builtin/plugins/codex/runtime_plan.py")
_FIXTURES = Path("tests/fixtures/codex_native_runtime")
_INDEX = _FIXTURES / "index.json"
_DOCS = (Path("docs/harness-plugins.md"), Path("docs/native-interaction/codex.md"))
_POLICY = "codex-appserver-verified"
_MOCK_LOG = "codex_mock_requests.jsonl"
# These endpoints are telemetry side channels; their existing responses remain unchanged.
_BENIGN_MOCK_ROUTES = frozenset({("POST", "/v1/analytics/codex/turn-costs")})
_CONSISTENCY = (
    "tests/test_harness_compat_codex.py",
    "tests/test_codex_native_runtime_proof.py::TestOfflineConformanceBundle",
    "tests/test_codex_native_runtime_proof.py::TestBundleIndexAndAllowlist",
    "tests/test_codex_native_runtime_proof.py::TestIndexValidation",
)
_UI_CONTROL = (
    "tests/test_codex_native_ui_bootstrap_proof.py::"
    "test_codex_native_ui_starts_a_thread_unattended[update-available-control-False]"
)
_GREEN = (
    "tests/test_codex_native_runtime_proof.py::TestNativeSmokeHandshakeAndFraming",
    "tests/test_codex_native_runtime_proof.py::TestNativeSmokeSharedUiSession",
    "tests/test_codex_native_runtime_proof.py::TestNativeSmokeTurnControls",
    "tests/test_codex_native_runtime_proof.py::TestNativeSmokeForkReconnectSurvival",
    "tests/test_codex_native_runtime_proof.py::TestNativeSmokeRemoteNecessity",
    "tests/test_codex_native_ui_bootstrap_proof.py::test_codex_native_ui_bootstrap_proof",
    "tests/test_codex_native_ui_bootstrap_proof.py::"
    "test_codex_native_ui_starts_a_thread_unattended[update-available-True]",
    _UI_CONTROL,
    "tests/test_codex_native_ui_bootstrap_proof.py::"
    "test_codex_native_ui_starts_a_thread_unattended[worktree-of-trusted-repo-True]",
)


def _allowlist_node(source: str) -> ast.Call:
    tree = ast.parse(source)
    for statement in tree.body:
        if not isinstance(statement, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "CODEX_RUNTIME_VERIFIED_VERSIONS"
            for target in statement.targets
        ):
            continue
        value = statement.value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "frozenset"
            and len(value.args) == 1
        ):
            return value
        raise ValueError("Codex allowlist is not a literal frozenset")
    raise ValueError("Codex allowlist assignment is missing")


def _versions(source: str) -> frozenset[str]:
    literal = ast.literal_eval(_allowlist_node(source).args[0])
    if not isinstance(literal, set) or not all(isinstance(item, str) for item in literal):
        raise ValueError("Codex allowlist is not a literal string set")
    return frozenset(literal)


def _write_if_changed(root: Path, relative: Path, content: str) -> list[Path]:
    path = root / relative
    if path.read_text() == content:
        return []
    path.write_text(content)
    return [relative]


def _complete_bundle(path: Path) -> bool:
    expected = set(BEHAVIOR_FILES) | {f"protocol_schema/{name}" for name in SCHEMA_FILES}
    actual = {item.relative_to(path).as_posix() for item in path.rglob("*") if item.is_file()}
    return actual == expected


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
    kind = "exact"
    known_bad: frozenset[str] = frozenset()
    probe = "theater.harness.builtin.plugins.codex.runtime_plan:probe_codex_compatibility"
    stub_help = ""
    green_tests = _GREEN
    consistency_tests = _CONSISTENCY
    expected_skips: ClassVar[dict[str, str]] = {
        _UI_CONTROL: "downloaded binaries may not show Codex's update dialog"
    }
    # The shared mock sees no Codex traffic; the collector audits its private mock routes.
    # Both proof mocks log calls; bootstrap asserts the exact scripted request count.
    expected_routes: frozenset[tuple[str, str]] = frozenset()
    ignorable_routes: frozenset[tuple[str, str]] = frozenset()

    def discover(self) -> list[str]:
        try:
            return github.github_release_versions("openai/codex", tag_prefix="rust-v")
        except urllib.error.URLError:
            return _public_git_tags()

    def verified(self, root: Path) -> VerifiedSet:
        return VerifiedSet(
            kind="exact",
            versions=_versions((root / _PLAN).read_text()),
            known_bad=self.known_bad,
            evidenced=load_evidenced(root, self.name),
        )

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
        source = (root / _PLAN).read_text()
        old = _versions(source)
        wanted = frozenset(old | set(greens))
        ordered = sorted(wanted, key=version_key)
        node = _allowlist_node(source)
        lines = source.splitlines(keepends=True)
        replacement = (
            "CODEX_RUNTIME_VERIFIED_VERSIONS = frozenset(\n    {\n"
            + "".join(f'        "{v}",\n' for v in ordered)
            + "    }\n)"
        )
        start, end = node.lineno - 1, node.end_lineno
        lines[start:end] = [replacement + "\n"]
        changed = _write_if_changed(root, _PLAN, "".join(lines))

        if old and wanted:
            old_top = max(old, key=version_key)
            new_top = max(wanted, key=version_key)
            if old_top != new_top:
                for doc in _DOCS:
                    text = (root / doc).read_text()
                    changed.extend(_write_if_changed(root, doc, text.replace(old_top, new_top)))

        index = json.loads((root / _INDEX).read_text())
        for version in sorted(set(greens), key=version_key):
            target = root / _FIXTURES / version
            staged = root / _FIXTURES / "candidates" / version
            if not target.exists() and staged.is_dir() and _complete_bundle(staged):
                shutil.copytree(staged, target)
                changed.extend(
                    path.relative_to(root) for path in sorted(target.rglob("*")) if path.is_file()
                )
                shutil.rmtree(staged)
            if target.is_dir() and _complete_bundle(target):
                index["bundles"][version] = {
                    "directory": version,
                    "status": "qualified",
                    "compatibility_policy": _POLICY,
                }
        index["bundles"] = {
            version: index["bundles"][version]
            for version in sorted(index["bundles"], key=version_key)
        }
        changed.extend(_write_if_changed(root, _INDEX, json.dumps(index, indent=2) + "\n"))
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
            return _classify_mock_routes(
                Result(
                    self.name,
                    version,
                    "tooling",
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
        staged = root / _FIXTURES / "candidates" / version
        staged.parent.mkdir(parents=True, exist_ok=True)
        if staged.exists():
            shutil.rmtree(staged)
        shutil.copytree(bundle, staged)
        self.apply_bump(root, [version])

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
        if not green or any(check.status == "error" for check in green):
            verdict: Verdict = "inconclusive"
        elif any(check.status == "fail" for check in green):
            verdict = "red"
        elif any(
            check.status == "skip" and check.name not in self.expected_skips for check in green
        ) or not any(check.status == "pass" for check in green):
            verdict = "inconclusive"
        else:
            verdict = "green"
        result = Result(self.name, version, verdict, (*consistency, *green), sb.evidence_dir)
        return _classify_mock_routes(result, mock_log)


ADAPTER = CodexAdapter()
