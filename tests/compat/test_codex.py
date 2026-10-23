"""The Codex range gate, its ceiling bump, the retained-bundle refresh and the collector."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from scripts.harness_compat.adapters import codex, codex_bump
from scripts.harness_compat.contract import Check, Result
from scripts.harness_compat.sandbox import make_sandbox
from tests.native import codex_qualify_runtime as collector

# Synthetic gate in the real constants' shape: floor 1.0.0, ceiling 1.5.0, one excluded release.
_PLAN_SOURCE = """\
CODEX_RUNTIME_QUALIFIED_FLOOR = (1, 0, 0)
CODEX_RUNTIME_QUALIFIED_CEILING = (1, 5, 0)
CODEX_RUNTIME_QUALIFIED_EXCLUDED: tuple[tuple[int, int, int], ...] = ((1, 2, 0),)
"""


def _tree(root: Path, plan: str = _PLAN_SOURCE) -> Path:
    (root / codex_bump.PLAN).parent.mkdir(parents=True)
    (root / codex_bump.PLAN).write_text(plan)
    return root


def _bundle(path: Path, *, marker: str = "same", release: str = "1.4.0") -> None:
    (path / "protocol_schema").mkdir(parents=True)
    for name in collector.BEHAVIOR_FILES:
        (path / name).write_text(f'{{"release": "{release}"}}\n' if "installed" in name else "{}\n")
    for name in collector.SCHEMA_FILES:
        (path / "protocol_schema" / name).write_text(f'{{"shape": "{marker}"}}\n')


def test_bump_edits_only_the_ceiling_line_and_fails_closed(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    adapter = codex.ADAPTER
    gate = adapter.verified(root)
    assert (gate.floor, gate.ceiling, gate.known_bad) == ("1.0.0", "1.5.0", frozenset({"1.2.0"}))
    assert [gate.admits(v) for v in ("1.0.0", "1.2.0", "1.4.9", "1.5.0")] == [
        True,
        False,
        True,
        False,
    ]
    assert adapter.apply_bump(root, ["1.4.0"]) == []  # nothing is green above the ceiling
    changed = adapter.apply_bump(root, ["1.5.0", "1.5.1"])
    assert changed == [codex_bump.PLAN]
    new = (root / codex_bump.PLAN).read_text().splitlines()
    old = _PLAN_SOURCE.splitlines()
    assert [i for i, (x, y) in enumerate(zip(old, new, strict=True)) if x != y] == [1]
    assert new[1] == "CODEX_RUNTIME_QUALIFIED_CEILING = (1, 5, 2)"
    assert adapter.apply_bump(root, ["1.5.0", "1.5.1"]) == []
    with pytest.raises(ValueError, match="below the qualified floor"):
        adapter.apply_bump(root, ["0.9.0"])
    for broken, message in (
        (_PLAN_SOURCE.replace("CEILING =", "MISSING ="), "missing Codex gate constant"),
        (_PLAN_SOURCE.replace("(1, 5, 0)", "(1,5,0)"), "changed shape"),
        (_PLAN_SOURCE.replace("(1, 5, 0)", "(1, 5)"), "three-integer literal"),
        (_PLAN_SOURCE + _PLAN_SOURCE.splitlines()[0] + "\n", "duplicate Codex gate constant"),
    ):
        (root / codex_bump.PLAN).write_text(broken)
        with pytest.raises(ValueError, match=message):
            adapter.apply_bump(root, ["1.5.0"])


def test_annotated_ceiling_keeps_its_comment_when_bumped(tmp_path: Path) -> None:
    source = _PLAN_SOURCE.replace(
        "CODEX_RUNTIME_QUALIFIED_CEILING = (1, 5, 0)",
        "CODEX_RUNTIME_QUALIFIED_CEILING: tuple[int, int, int] = (1, 5, 0)  # gate",
    )
    root = _tree(tmp_path, source)
    assert codex.ADAPTER.apply_bump(root, ["1.5.0"]) == [codex_bump.PLAN]
    assert (
        "CODEX_RUNTIME_QUALIFIED_CEILING: tuple[int, int, int] = (1, 5, 1)  # gate"
        in (root / codex_bump.PLAN).read_text()
    )


def test_bump_refreshes_latest_only_when_the_capture_differs(tmp_path: Path) -> None:
    root = _tree(tmp_path)
    latest = root / codex._LATEST
    _bundle(latest)
    candidates = root / codex._CANDIDATES
    # Identity differs on every release; the same evidence must not touch a fixture.
    _bundle(candidates / "1.5.0", release="1.5.0")
    assert codex.ADAPTER.apply_bump(root, ["1.5.0"]) == [codex_bump.PLAN]
    assert not candidates.exists()
    assert json.loads((latest / "installed_release.json").read_text()) == {"release": "1.4.0"}
    # Only the newest green may refresh latest; changed evidence rewrites exactly that file.
    _bundle(candidates / "1.5.1", marker="older", release="1.5.1")
    _bundle(candidates / "1.5.2", marker="newer", release="1.5.2")
    changed = codex.ADAPTER.apply_bump(root, ["1.5.1", "1.5.2"])
    schema = codex._LATEST / "protocol_schema"
    expected = {codex_bump.PLAN, codex._LATEST / "installed_release.json"}
    expected |= {schema / name for name in collector.SCHEMA_FILES}
    assert set(changed) == expected
    assert (latest / "protocol_schema/ClientRequest.json").read_text() == '{"shape": "newer"}\n'
    assert not candidates.exists()


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
            "a\trefs/tags/rust-v1.2.1\n"
            "b\trefs/tags/rust-v1.1.0\n"
            "c\trefs/tags/rust-v1.3.0-alpha.1\n",
            "",
        )

    monkeypatch.setattr(codex.github, "github_release_versions", limited)
    monkeypatch.setattr(codex.subprocess, "run", tags)
    assert codex.ADAPTER.discover() == ["1.1.0", "1.2.1"]


def test_collector_baseline_is_latest_and_masks_run_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    for role in ("floor", "latest"):
        _bundle(fixtures / role)
    (fixtures / "index.json").write_text(
        json.dumps(
            {
                "bundles": {
                    role: {"directory": role, "status": "qualified"} for role in ("floor", "latest")
                }
            }
        )
    )
    monkeypatch.setattr(collector, "FIXTURE_ROOT", fixtures)
    assert collector.default_baseline() == fixtures / "latest"
    redacted = collector.redact(
        {
            "sec-websocket-accept": "random-base64",
            "error_text": "sessions/2026/10/13/rollout-2026-10-13T11-12-13-id.jsonl",
            "tmp": 'helper binaries under temporary dir "/tmp/xq1/t" (x)',
            "cli": "v9.9.9",
        },
        {"9.9.9": "<release>"},
    )
    assert redacted == {
        "sec-websocket-accept": "<websocket-accept>",
        "error_text": "sessions/<date>/rollout-<time>-id.jsonl",
        "tmp": 'helper binaries under temporary dir "<tmp-dir>" (x)',
        "cli": "v<release>",
    }
    # Advisory warnings race with real notifications, so they never enter the evidence.
    messages = [
        {"method": "warning"},
        {"method": "thread/started"},
        {"id": 1},
        {"method": "warning"},
    ]
    assert collector.notification_methods(messages) == ["thread/started"]
    (fixtures / "floor" / "protocol_schema" / "Unused.json").write_text("{}")
    assert collector.diff_bundle(fixtures / "floor", fixtures / "latest", committed_only=True) == []
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
        ("red", ("POST", "/v1/new-required-endpoint"), "tooling", True),
        ("inconclusive", ("POST", "/v1/new-required-endpoint"), "tooling", True),
        ("tooling", ("POST", "/v1/new-required-endpoint"), "tooling", True),
        ("green", ("POST", "/v1/new-required-endpoint"), "green", False),
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
        assert actual.checks[-1].detail == (
            "unexpected private mock routes: ['POST /v1/new-required-endpoint']"
        )


def test_collect_failure_still_classifies_logged_bootstrap_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _tree(tmp_path / "tree")
    adapter = codex.ADAPTER
    candidate = "1.5.0"

    def fail_collect(command, **kwargs):
        log = Path(kwargs["env"]["THEATER_COMPAT_MOCK_LOG"])
        log.write_text(
            json.dumps(
                {
                    "method": "POST",
                    "path": "/v1/new-required-endpoint",
                    "scripted": False,
                }
            )
            + "\n"
        )
        return subprocess.CompletedProcess(command, 1, "", "proof stopped early")

    monkeypatch.setattr(codex.subprocess, "run", fail_collect)
    with make_sandbox("codex", root=tmp_path / "sandbox") as sb:
        result = adapter.qualify(root, candidate, sb, sb.bin_dir / "codex")
    assert result.verdict == "tooling"
    assert [check.name for check in result.checks] == ["collect_bundle", "mock_routes"]
    assert result.checks[-1].status == "fail"


@pytest.mark.parametrize(
    ("expected_skip", "verdict"),
    [(False, "inconclusive"), (True, "green")],
)
def test_qualify_accepts_only_the_named_optional_control_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expected_skip: bool, verdict: str
) -> None:
    root = _tree(tmp_path / "tree")
    adapter = codex.ADAPTER
    candidate = "1.5.0"

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


def test_an_explicit_proof_failure_outranks_a_setup_error_beside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _tree(tmp_path / "tree")
    adapter = codex.ADAPTER

    def collect(command, **kwargs):
        _bundle(Path(command[command.index("--out") + 1]))
        return subprocess.CompletedProcess(command, 0, "", "")

    def run_pytest(root, nodeids, *, env, expected_skips, timeout=900):
        if nodeids != adapter.green_tests:
            return tuple(Check(nodeid, "pass") for nodeid in nodeids)
        return (Check(nodeids[0], "fail"), Check(nodeids[1], "error"), *map(_pass, nodeids[2:]))

    monkeypatch.setattr(codex.subprocess, "run", collect)
    monkeypatch.setattr(codex.framework, "run_pytest", run_pytest)
    with make_sandbox("codex", root=tmp_path / "sandbox") as sb:
        result = adapter.qualify(root, "1.5.0", sb, sb.bin_dir / "codex")
    assert result.verdict == "red"


def _pass(nodeid: str) -> Check:
    return Check(nodeid, "pass")


def test_collector_keeps_failure_diagnostics_only_when_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "server.log"
    log.write_text("boom")
    pane = SimpleNamespace(socket_path=tmp_path / "ui.sock", pane=lambda: "dialog")
    session = SimpleNamespace(app_server=SimpleNamespace(log_path=log), tmux_uis=[pane])
    monkeypatch.delenv("THEATER_COMPAT_DIAGNOSTICS", raising=False)
    collector._keep_diagnostics(cast(collector.CaptureSession, session))
    kept = tmp_path / "kept"
    assert not kept.exists()
    monkeypatch.setenv("THEATER_COMPAT_DIAGNOSTICS", str(kept))
    collector._keep_diagnostics(cast(collector.CaptureSession, session))
    assert (kept / "app-server.log").read_text() == "boom"
    assert (kept / "ui.sock.pane.txt").read_text() == "dialog"
