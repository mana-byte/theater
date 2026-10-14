"""The Codex exact gate and collector write path."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from scripts.harness_compat.adapters import codex
from scripts.harness_compat.contract import Check, Result
from scripts.harness_compat.sandbox import make_sandbox
from tests.native import codex_qualify_runtime as collector


def _tree(root: Path) -> Path:
    source = Path(__file__).resolve().parents[1]
    plan = root / codex._PLAN
    plan.parent.mkdir(parents=True)
    shutil.copyfile(source / codex._PLAN, plan)
    newest = max(codex._versions(plan.read_text()), key=codex.version_key)
    for relative in codex._DOCS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"verified through {newest}\n")
    index = root / codex._INDEX
    index.parent.mkdir(parents=True, exist_ok=True)
    index.write_text('{"bundles": {}}\n')
    return root


def _bundle(path: Path) -> None:
    (path / "protocol_schema").mkdir(parents=True)
    for name in collector.BEHAVIOR_FILES:
        (path / name).write_text("{}\n")
    for name in collector.SCHEMA_FILES:
        (path / "protocol_schema" / name).write_text("{}\n")


def _next_patches(version: str, count: int = 2) -> tuple[str, ...]:
    major, minor, patch = codex.version_key(version)
    return tuple(f"{major}.{minor}.{patch + offset}" for offset in range(1, count + 1))


def test_allowlist_bump_stages_complete_bundles_once(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    adapter = codex.ADAPTER
    old_top = max(adapter.verified(root).versions, key=codex.version_key)
    first_version, second_version = _next_patches(old_top)
    first = adapter.apply_bump(root, [second_version, first_version])
    assert codex._PLAN in first
    assert adapter.verified(root).admits(second_version)
    assert second_version not in json.loads((root / codex._INDEX).read_text())["bundles"]

    for version in (second_version, first_version):
        _bundle(root / codex._FIXTURES / "candidates" / version)
    second = adapter.apply_bump(root, [second_version, first_version])
    index = json.loads((root / codex._INDEX).read_text())["bundles"]
    assert list(index) == [first_version, second_version]
    assert all(entry["status"] == "qualified" for entry in index.values())
    assert codex._INDEX in second
    assert all(second_version in (root / doc).read_text() for doc in codex._DOCS)
    assert adapter.apply_bump(root, [first_version, second_version]) == []


def test_discovery_falls_back_to_public_tags_when_api_is_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def limited(*args, **kwargs):
        raise urllib.error.HTTPError("https://api.github.com", 403, "rate limit", None, None)

    def tags(command, **kwargs):
        assert command[:3] == ["git", "-c", "credential.helper="]
        assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == os.devnull
        return subprocess.CompletedProcess(
            command,
            0,
            "a\trefs/tags/rust-v0.162.1\n"
            "b\trefs/tags/rust-v0.160.0\n"
            "c\trefs/tags/rust-v0.163.0-alpha.1\n",
            "",
        )

    monkeypatch.setattr(codex.github, "github_release_versions", limited)
    monkeypatch.setattr(codex.subprocess, "run", tags)
    assert codex.ADAPTER.discover() == ["0.160.0", "0.162.1"]


def test_collector_picks_newest_and_masks_run_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    for version in ("0.9.9", "0.10.0"):
        _bundle(fixtures / version)
    (fixtures / "index.json").write_text(
        json.dumps(
            {
                "bundles": {
                    version: {"directory": version, "status": "qualified"}
                    for version in ("0.9.9", "0.10.0")
                }
            }
        )
    )
    monkeypatch.setattr(collector, "FIXTURE_ROOT", fixtures)
    assert collector.default_baseline(newest=True) == fixtures / "0.10.0"
    redacted = collector.redact(
        {
            "sec-websocket-accept": "random-base64",
            "error_text": "sessions/2026/10/13/rollout-2026-10-13T11-12-13-id.jsonl",
        },
        {},
    )
    assert redacted == {
        "sec-websocket-accept": "<websocket-accept>",
        "error_text": "sessions/<date>/rollout-<time>-id.jsonl",
    }
    (fixtures / "0.9.9" / "protocol_schema" / "Unused.json").write_text("{}")
    assert collector.diff_bundle(fixtures / "0.9.9", fixtures / "0.10.0", committed_only=True) == []
    session = cast(
        collector.CaptureSession,
        SimpleNamespace(
            mock=SimpleNamespace(
                provider=SimpleNamespace(requests=[SimpleNamespace(path="/v1/new")])
            )
        ),
    )
    with pytest.raises(collector.QualificationError, match="unexpected routes"):
        collector._audit_mock_routes(session)


@pytest.mark.parametrize(
    ("verdict", "route", "expected", "classified"),
    [
        ("red", ("PUT", "/v1/unknown"), "tooling", True),
        ("green", ("PUT", "/v1/unknown"), "green", False),
        ("red", ("POST", "/v1/analytics/codex/turn-costs"), "red", False),
    ],
)
def test_private_mock_route_classification(
    tmp_path: Path,
    verdict: str,
    route: tuple[str, str],
    expected: str,
    classified: bool,
) -> None:
    log = tmp_path / "mock.jsonl"
    log.write_text(json.dumps({"method": route[0], "path": route[1], "scripted": False}) + "\n")
    result = Result("codex", "0.0.0", verdict, (Check("proof", "fail"),))
    actual = codex._classify_mock_routes(result, log)
    assert actual.verdict == expected
    assert (actual.checks[-1].name == "mock_routes") is classified
    if classified:
        assert actual.checks[-1].status == "fail"
        assert actual.checks[-1].detail == "unexpected private mock routes: ['PUT /v1/unknown']"


@pytest.mark.parametrize(
    ("expected_skip", "verdict"),
    [(False, "inconclusive"), (True, "green")],
)
def test_qualify_accepts_only_the_named_optional_control_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expected_skip: bool, verdict: str
) -> None:
    root = _tree(tmp_path / "tree")
    adapter = codex.ADAPTER
    old_top = max(adapter.verified(root).versions, key=codex.version_key)
    (candidate,) = _next_patches(old_top, count=1)
    adapter.apply_bump(root, [candidate])

    def collect(command, **kwargs):
        assert kwargs["env"]["THEATER_COMPAT_MOCK_LOG"].endswith("codex_mock_requests.jsonl")
        out = Path(command[command.index("--out") + 1])
        _bundle(out)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(codex.subprocess, "run", collect)
    calls: list[tuple[tuple[str, ...], bool, bool]] = []
    skipped = codex._UI_CONTROL if expected_skip else adapter.green_tests[0]

    def run_pytest(root, nodeids, *, env, expected_skips, timeout=900):
        calls.append((tuple(nodeids), "HTTPS_PROXY" in env, "THEATER_COMPAT_MOCK_LOG" in env))
        if nodeids == adapter.green_tests:
            assert expected_skips == adapter.expected_skips
            return tuple(
                Check(nodeid, "skip" if nodeid == skipped else "pass") for nodeid in nodeids
            )
        return tuple(Check(nodeid, "pass") for nodeid in nodeids)

    monkeypatch.setattr(codex.framework, "run_pytest", run_pytest)
    with make_sandbox("codex", root=tmp_path / "sandbox") as sb:
        result = adapter.qualify(root, candidate, sb, sb.bin_dir / "codex")
        assert result.evidence_dir == sb.evidence_dir
        assert (sb.evidence_dir / candidate / "approval.json").is_file()
    assert calls == [
        (adapter.consistency_tests, True, True),
        (adapter.green_tests, False, True),
    ]
    assert result.verdict == verdict
