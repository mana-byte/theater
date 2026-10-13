"""The Codex exact gate and collector write path."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from scripts.harness_compat.adapters import codex
from scripts.harness_compat.contract import Check
from scripts.harness_compat.sandbox import make_sandbox
from tests.native import codex_qualify_runtime as collector


def _tree(root: Path) -> Path:
    source = Path(__file__).resolve().parents[1]
    plan = root / codex._PLAN
    plan.parent.mkdir(parents=True)
    shutil.copyfile(source / codex._PLAN, plan)
    for relative in codex._DOCS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("verified through 0.159.1\n")
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


def test_allowlist_bump_stages_complete_bundles_once(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    adapter = codex.ADAPTER
    assert "0.159.1" in adapter.verified(root).versions
    first = adapter.apply_bump(root, ["0.162.1", "0.160.0"])
    assert codex._PLAN in first
    assert adapter.verified(root).admits("0.162.1")
    assert "0.162.1" not in json.loads((root / codex._INDEX).read_text())["bundles"]

    for version in ("0.162.1", "0.160.0"):
        _bundle(root / codex._FIXTURES / "candidates" / version)
    second = adapter.apply_bump(root, ["0.162.1", "0.160.0"])
    index = json.loads((root / codex._INDEX).read_text())["bundles"]
    assert list(index) == ["0.160.0", "0.162.1"]
    assert all(entry["status"] == "qualified" for entry in index.values())
    assert codex._INDEX in second
    assert all("0.162.1" in (root / doc).read_text() for doc in codex._DOCS)
    assert adapter.apply_bump(root, ["0.160.0", "0.162.1"]) == []


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


def test_qualify_uses_real_proofs_and_rejects_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _tree(tmp_path / "tree")
    adapter = codex.ADAPTER
    adapter.apply_bump(root, ["0.162.1"])

    def collect(command, **kwargs):
        out = Path(command[command.index("--out") + 1])
        _bundle(out)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(codex.subprocess, "run", collect)
    calls: list[tuple[str, ...]] = []

    def run_pytest(root, nodeids, *, env, expected_skips, timeout=900):
        calls.append(tuple(nodeids))
        status = "skip" if nodeids == adapter.green_tests else "pass"
        return (Check(nodeids[0], status),)

    monkeypatch.setattr(codex.framework, "run_pytest", run_pytest)
    with make_sandbox("codex", root=tmp_path / "sandbox") as sb:
        result = adapter.qualify(root, "0.162.1", sb, sb.bin_dir / "codex")
        assert result.evidence_dir == sb.evidence_dir
        assert (sb.evidence_dir / "0.162.1" / "approval.json").is_file()
    assert calls == [adapter.consistency_tests, adapter.green_tests]
    assert result.verdict == "inconclusive"
