"""MVP policy and subprocess checks for harness qualification."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import ClassVar

import pytest

from scripts.harness_compat import runner
from scripts.harness_compat.artifacts import CandidateArtifacts
from scripts.harness_compat.contract import (
    Check,
    RecordedRequest,
    Result,
    Sandbox,
    VerifiedSet,
    evidence_file,
)
from scripts.harness_compat.evidence import adapter_digest, reusable_evidence
from scripts.harness_compat.framework import check_bump, run_pytest
from scripts.harness_compat.publication import prepare_publication
from scripts.harness_compat.runner import classify_routes
from scripts.harness_compat.sandbox import make_sandbox
from scripts.harness_compat.select import newest_in_range, plan_bump, select_candidates


class FakeAdapter:
    name = "vibe"
    expected_routes = frozenset({("POST", "/v1/chat/completions")})
    ignorable_routes = frozenset({("POST", "/telemetry")})

    def discover(self) -> list[str]:
        return ["1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0"]


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, capture_output=True, check=True)


def test_range_policy_evidence_reuse_routes_and_watch(tmp_path: Path) -> None:
    source = tmp_path / "scripts/harness_compat"
    adapters = source / "adapters"
    adapters.mkdir(parents=True)
    (source / "contract.py").write_text("contract\n")
    (adapters / "vibe.py").write_text("adapter\n")
    digest = adapter_digest(tmp_path, "vibe")
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "compat@example.invalid")
    _git(tmp_path, "config", "user.name", "Compat")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "source")
    _git(tmp_path, "checkout", "-b", "rolling")
    evidence = tmp_path / "tests/fixtures/harness_compat/vibe/1.2.0.json"
    evidence.parent.mkdir(parents=True)
    document = {
        "harness": "vibe",
        "version": "1.2.0",
        "adapter_digest": digest,
        "verdict": "green",
        "checks": [],
    }
    evidence.write_text(json.dumps(document))
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "evidence")
    _git(tmp_path, "checkout", "main")
    assert reusable_evidence(tmp_path, "vibe", digest, branch="rolling") == {"1.2.0": document}
    gate = VerifiedSet("range", floor="1.0.0", ceiling="1.2.0", known_bad=frozenset({"1.4.0"}))
    adapter = FakeAdapter()
    assert select_candidates(adapter, gate, {"1.2.0"}) == ["1.3.0", "1.5.0"]
    assert newest_in_range(adapter, gate) == "1.1.0"
    results = [
        Result("vibe", "1.2.0", "green"),
        Result("vibe", "1.3.0", "green"),
        Result("vibe", "1.5.0", "red"),
    ]
    assert plan_bump(gate, results) == ["1.2.0", "1.3.0"]
    route = RecordedRequest("POST", "/unknown", False, ("messages", "model"), True)
    classified = classify_routes(adapter, results[-1], (route,))
    assert classified.verdict == "tooling"
    assert "POST /unknown body keys=messages,model" in classified.checks[-1].detail
    assert classify_routes(adapter, results[-1], ()).verdict == "red"


class ArtifactAdapter:
    name = "vibe"
    probe = "unused:probe"
    stub_help = ""
    green_tests = ("proof",)
    consistency_tests: tuple[str, ...] = ()
    expected_skips: ClassVar[dict[str, str]] = {}
    expected_routes: frozenset[tuple[str, str]] = frozenset()
    ignorable_routes: frozenset[tuple[str, str]] = frozenset()

    def discover(self) -> list[str]:
        return ["1.2.0"]

    def verified(self, root: Path) -> VerifiedSet:
        admitted = (
            frozenset({"1.2.0"}) if (root / "gate.txt").read_text() == "1.2.0\n" else frozenset()
        )
        return VerifiedSet("exact", versions=admitted)

    def install(self, sb: Sandbox, version: str) -> Path:
        return sb.bin_dir / version

    def version_line(self, version: str) -> str:
        return version

    def apply_bump(self, root: Path, greens: list[str]) -> list[Path]:
        (root / "gate.txt").write_text("1.2.0\n")
        changed = [Path("gate.txt")]
        staged = root / "candidates/1.2.0.json"
        if staged.exists():
            target = root / "fixtures/1.2.0.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(staged, target)
            changed.append(Path("fixtures/1.2.0.json"))
        return changed

    def qualify(self, root: Path, version: str, sb: Sandbox, binary: Path) -> Result:
        staged = root / "candidates/1.2.0.json"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_text('{"proved": true}\n')
        return Result(self.name, version, "green", (Check("proof", "pass"),))


class EvidenceOnlyAdapter:
    name = "claude"
    probe = "unavailable:probe"
    stub_help = ""
    green_tests = ("proof",)
    consistency_tests: tuple[str, ...] = ()
    expected_skips: ClassVar[dict[str, str]] = {}
    expected_routes: frozenset[tuple[str, str]] = frozenset()
    ignorable_routes: frozenset[tuple[str, str]] = frozenset()

    def discover(self) -> list[str]:
        return ["3.0.0"]

    def verified(self, root: Path) -> VerifiedSet:
        return VerifiedSet("range", floor="2.0.0", ceiling="3.0.0")

    def install(self, sb: Sandbox, version: str) -> Path:
        return sb.bin_dir / version

    def version_line(self, version: str) -> str:
        return version

    def apply_bump(self, root: Path, greens: list[str]) -> list[Path]:
        changed = []
        for version in greens:
            path = evidence_file(root, self.name, version)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"placeholder": true}\n')
            changed.append(path.relative_to(root))
        return changed

    def qualify(self, root: Path, version: str, sb: Sandbox, binary: Path) -> Result:
        return Result(self.name, version, "green", (Check("proof", "pass"),))


def _evidence_only_repo(root: Path) -> None:
    source = root / "scripts/harness_compat"
    adapters = source / "adapters"
    adapters.mkdir(parents=True)
    (source / "contract.py").write_text("contract\n")
    (adapters / "claude.py").write_text("adapter\n")
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "compat@example.invalid")
    _git(root, "config", "user.name", "Compat")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "source")


def test_evidence_only_bump_skips_probe_and_rejects_other_changes(tmp_path: Path) -> None:
    _evidence_only_repo(tmp_path)
    adapter = EvidenceOnlyAdapter()
    assert adapter.apply_bump(tmp_path, ["3.0.0"]) == [
        Path("tests/fixtures/harness_compat/claude/3.0.0.json")
    ]
    assert not adapter.verified(tmp_path).admits("3.0.0")
    assert check_bump(adapter, tmp_path, ["3.0.0"]) == []
    (tmp_path / "extra.txt").write_text("unexpected\n")
    assert "evidence-only bump changed unrelated files" in " ".join(
        check_bump(adapter, tmp_path, ["3.0.0"])
    )


def test_framework_replaces_placeholder_evidence_and_reports_ceiling(tmp_path: Path) -> None:
    _evidence_only_repo(tmp_path)
    summary = runner.run_harness(
        EvidenceOnlyAdapter(), tmp_path, publish=True, run_url="https://example.invalid/run/1"
    )
    assert summary["published"] is True
    assert summary["evidence_only"] is True
    assert summary["greens"] == ["3.0.0"]
    document = json.loads(evidence_file(tmp_path, "claude", "3.0.0").read_text())
    result = Result(
        "claude",
        "3.0.0",
        "green",
        (Check("proof", "pass"),),
        adapter_digest=summary["adapter_digest"],
    )
    assert document == {**result.to_evidence(), "run_url": "https://example.invalid/run/1"}
    assert "placeholder" not in document
    assert subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines() == ["tests/fixtures/harness_compat/claude/3.0.0.json"]
    report = prepare_publication(summary, tmp_path / "summary.json")
    assert report.pr_needed is True
    assert "Compatibility ceiling was NOT raised" in report.pr_body


def test_forced_in_range_candidate_reuses_its_watch_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _evidence_only_repo(tmp_path)
    calls: list[str] = []

    class InRangeAdapter(EvidenceOnlyAdapter):
        def verified(self, root: Path) -> VerifiedSet:
            return VerifiedSet("range", floor="2.0.0", ceiling="4.0.0")

        def apply_bump(self, root: Path, greens: list[str]) -> list[Path]:
            return []

        def qualify(self, root: Path, version: str, sb: Sandbox, binary: Path) -> Result:
            calls.append(version)
            return super().qualify(root, version, sb, binary)

    monkeypatch.setattr(runner, "check_bump", lambda adapter, root, bumped: [])
    summary = runner.run_harness(InRangeAdapter(), tmp_path, version="3.0.0")
    assert calls == ["3.0.0"]
    assert summary["watch"] == summary["results"][0]
    assert summary["watch_reused"] is True
    assert summary["watch"]["verdict"] == "green"
    route = {"method": "POST", "path": "/v1/chat/completions"}
    summary["results"][0]["routes"] = [route]
    summary["watch"]["routes"] = [route]
    report = prepare_publication(summary, tmp_path / "summary.json")
    assert "`POST /v1/chat/completions` ×1" in report.pr_body


def test_green_candidate_artifact_is_composed_and_staged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "scripts/harness_compat"
    adapters = source / "adapters"
    adapters.mkdir(parents=True)
    (source / "contract.py").write_text("contract\n")
    (adapters / "vibe.py").write_text("adapter\n")
    (tmp_path / "gate.txt").write_text("none\n")
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "compat@example.invalid")
    _git(tmp_path, "config", "user.name", "Compat")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "source")
    monkeypatch.setattr(runner, "check_bump", lambda adapter, root, bumped: [])

    summary = runner.run_harness(ArtifactAdapter(), tmp_path, publish=True)

    assert summary["published"] is True
    assert (tmp_path / "fixtures/1.2.0.json").read_text() == '{"proved": true}\n'
    assert not (tmp_path / "candidates/1.2.0.json").exists()
    staged = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert staged == [
        "fixtures/1.2.0.json",
        "gate.txt",
        "tests/fixtures/harness_compat/vibe/1.2.0.json",
    ]
    _git(tmp_path, "checkout", "-b", "rolling")
    _git(tmp_path, "commit", "-m", "rolling evidence")
    _git(tmp_path, "checkout", "main")
    reused = runner.run_harness(
        ArtifactAdapter(), tmp_path, publish=True, evidence_branch="rolling"
    )
    assert reused["candidates"] == []
    assert reused["published"] is True
    assert (tmp_path / "fixtures/1.2.0.json").read_text() == '{"proved": true}\n'
    assert not (tmp_path / "candidates/1.2.0.json").exists()
    pool = CandidateArtifacts(tmp_path / "collision-pool")
    first, second = tmp_path / "collision-1", tmp_path / "collision-2"
    for tree in (first, second):
        tree.mkdir()
        _git(tree, "init")
        (tree / "same.txt").write_text("proof\n")
    assert pool.capture(first, "1.2.0") == []
    assert pool.capture(second, "1.3.0") == [
        "candidate artifact same.txt conflicts between 1.2.0 and 1.3.0"
    ]


def test_run_pytest_maps_skips_and_failures(tmp_path: Path) -> None:
    test = tmp_path / "test_sample.py"
    test.write_text(
        "import pytest\n"
        "def test_pass(): assert True\n"
        "def test_fail(): assert False\n"
        "def test_expected(): pytest.skip('expected')\n"
        "def test_unexpected(): pytest.skip('unexpected')\n"
    )
    nodes = tuple(
        f"test_sample.py::test_{name}" for name in ("pass", "fail", "expected", "unexpected")
    )
    checks = run_pytest(
        tmp_path,
        nodes,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(tmp_path),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
        },
        expected_skips={nodes[2]: "known"},
        timeout=30,
    )
    assert [check.status for check in checks] == ["pass", "fail", "skip", "error"]
    assert all(isinstance(check, Check) for check in checks)


def test_run_pytest_aggregates_file_directory_class_and_parameter_selectors(tmp_path: Path) -> None:
    proofs = tmp_path / "proofs"
    proofs.mkdir()
    (proofs / "test_selectors.py").write_text(
        "import pytest\n"
        "def test_top(): assert True\n"
        "class TestBundleIndexAndAllowlist:\n"
        "    def test_first(self): assert True\n"
        "    def test_second(self): assert True\n"
        "@pytest.mark.parametrize('value', [1, 2])\n"
        "def test_param(value): assert value > 0\n"
    )
    (tmp_path / "test_skip.py").write_text(
        "import pytest\n"
        "@pytest.mark.parametrize('value', [1, 2])\n"
        "def test_skip(value): pytest.skip('unavailable')\n"
    )
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    file = "proofs/test_selectors.py"
    selectors = (
        "proofs",
        file,
        f"{file}::TestBundleIndexAndAllowlist",
        f"{file}::test_param",
        f"{file}::test_param[1]",
    )
    checks = run_pytest(tmp_path, selectors, env=env, expected_skips={}, timeout=30)
    assert [check.status for check in checks] == ["pass"] * len(selectors)
    assert [check.detail for check in checks] == [
        "5 passed",
        "5 passed",
        "2 passed",
        "2 passed",
        "1 passed",
    ]
    unknown = run_pytest(
        tmp_path, (f"{file}::test_missing",), env=env, expected_skips={}, timeout=30
    )
    assert unknown[0].status == "error"
    unexpected = run_pytest(tmp_path, ("test_skip.py",), env=env, expected_skips={}, timeout=30)
    assert unexpected[0].status == "error"
    assert "2 skipped" in unexpected[0].detail
    expected = run_pytest(
        tmp_path,
        ("test_skip.py::test_skip",),
        env=env,
        expected_skips={"test_skip.py::test_skip": "known"},
        timeout=30,
    )
    assert expected[0].status == "skip"
    assert expected[0].detail == "2 skipped"


def test_sandbox_keeps_unix_socket_paths_short(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with make_sandbox("codex") as sb:
        if Path("/tmp").is_dir():
            assert sb.root.is_relative_to(Path("/tmp").resolve())
        assert sb.root == sb.root.resolve()
        assert sb.env["TMPDIR"] == str(sb.root / "t")
        socket = (
            sb.evidence_dir
            / "0.162.100"
            / sb.home.name
            / ".codex/sockets"
            / f"app-server-{'0' * 32}.sock"
        )
        assert len(os.fsencode(socket)) < 100
        assert len(os.fsencode(sb.tmux_socket)) < 100
        for env in (sb.env, sb.offline_env):
            assert "ANTHROPIC_API_KEY" not in env
            assert "OPENAI_API_KEY" not in env

    long_root = tmp_path / ("x" * 120)

    def long_temp(*args: object, **kwargs: object) -> str:
        long_root.mkdir()
        return str(long_root)

    monkeypatch.setattr("scripts.harness_compat.sandbox.tempfile.mkdtemp", long_temp)
    with (
        pytest.raises(RuntimeError, match="tmux AF_UNIX socket path is too long"),
        make_sandbox("codex"),
    ):
        pass
    assert not long_root.exists()
