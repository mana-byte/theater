"""The OpenCode harness-compat adapter: parser, bump and verdicts."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.harness_compat import contract
from scripts.harness_compat.adapters import (
    opencode,
    opencode_bump,
    opencode_pi_process,
    opencode_sandbox,
)
from scripts.harness_compat.probe import _probe
from tests.support.paths import REPO_ROOT

_REPO_ROOT = REPO_ROOT


@pytest.fixture()
def coupled_tree(tmp_path: Path) -> Path:
    """A temp root carrying fresh copies of every file the bump touches."""
    for relative in opencode_bump.COUPLED_FILES:
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((_REPO_ROOT / relative).read_bytes())
    return tmp_path


def _gate(tree: Path) -> tuple[str, str]:
    return opencode_bump.read_gate(tree)


def _candidate() -> str:
    return _gate(_REPO_ROOT)[1]


def test_verified_reads_an_exclusive_range_gate(coupled_tree: Path) -> None:
    floor, ceiling = _gate(coupled_tree)
    gate = opencode.ADAPTER.verified(coupled_tree)
    assert gate.kind == "range"
    assert gate.floor == floor and gate.ceiling == ceiling
    assert gate.known_bad == frozenset()
    assert gate.evidenced == frozenset()
    assert gate.admits(floor) and gate.admits(opencode_bump.previous_patch(ceiling))
    assert not gate.admits(ceiling)
    assert not gate.admits(opencode_bump.previous_patch(floor))


def test_apply_bump_changes_only_the_source_ceiling(coupled_tree: Path) -> None:
    floor, ceiling = _gate(coupled_tree)
    last_green = ceiling  # the oldest unadmitted candidate
    changed = opencode.ADAPTER.apply_bump(coupled_tree, [last_green])
    assert changed == [opencode_bump.RUNTIME_PLAN]
    new_ceiling = opencode_bump.next_patch(last_green)
    assert _gate(coupled_tree) == (floor, new_ceiling)
    assert opencode.ADAPTER.verified(coupled_tree).admits(last_green)
    assert not opencode.ADAPTER.verified(coupled_tree).admits(new_ceiling)

    major, minor, patch = contract.version_key(new_ceiling)
    runtime_plan = (coupled_tree / opencode_bump.RUNTIME_PLAN).read_text(encoding="utf-8")
    assert f"OPENCODE_TUI_MAX_VERSION = ({major}, {minor}, {patch})" in runtime_plan
    assert "OPENCODE_SERVER_MAX_VERSION = OPENCODE_TUI_MAX_VERSION" in runtime_plan


def test_apply_bump_is_idempotent_and_deterministic(coupled_tree: Path, tmp_path: Path) -> None:
    shutil.copytree(_REPO_ROOT / "theater", coupled_tree / "theater", dirs_exist_ok=True)
    initial_ceiling = _gate(coupled_tree)[1]
    ladder = (
        initial_ceiling,
        opencode_bump.next_patch(initial_ceiling),
        opencode_bump.next_minor(initial_ceiling),
    )
    stub = coupled_tree / "opencode-stub"
    for version in ladder:
        floor, _ = _gate(coupled_tree)
        changed = opencode.ADAPTER.apply_bump(coupled_tree, [version])
        assert changed == [opencode_bump.RUNTIME_PLAN]
        assert _gate(coupled_tree) == (floor, opencode_bump.next_patch(version))
        for cache in (coupled_tree / "theater").rglob("__pycache__"):
            shutil.rmtree(cache)
        supported, reason = _probe(opencode.ADAPTER, coupled_tree, version, stub)
        assert supported is True, reason
        supported, reason = _probe(
            opencode.ADAPTER, coupled_tree, opencode_bump.next_patch(version), stub
        )
        assert supported is False, reason
        assert opencode.ADAPTER.apply_bump(coupled_tree, [version]) == []

    snapshot = {
        relative: (coupled_tree / relative).read_bytes() for relative in opencode_bump.COUPLED_FILES
    }
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    for relative in opencode_bump.COUPLED_FILES:
        destination = fresh / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((_REPO_ROOT / relative).read_bytes())
    for version in ladder:
        opencode.ADAPTER.apply_bump(fresh, [version])
    assert {
        relative: (fresh / relative).read_bytes() for relative in opencode_bump.COUPLED_FILES
    } == snapshot


def test_apply_bump_refuses_unordered_or_v2_releases(coupled_tree: Path) -> None:
    floor, ceiling = _gate(coupled_tree)
    beyond = opencode_bump.next_patch(ceiling)
    with pytest.raises(ValueError, match="unique and ascending"):
        opencode.ADAPTER.apply_bump(coupled_tree, [beyond, ceiling])
    with pytest.raises(ValueError, match=r"only admits 1\.x releases"):
        opencode.ADAPTER.apply_bump(coupled_tree, ["2.0.6"])
    assert _gate(coupled_tree) == (floor, ceiling)


def test_apply_bump_fails_closed_when_ceiling_anchor_is_missing(coupled_tree: Path) -> None:
    path = coupled_tree / opencode_bump.RUNTIME_PLAN
    source = path.read_text(encoding="utf-8")
    path.write_text(source.replace("OPENCODE_TUI_MAX_VERSION =", "MISSING_MAX_VERSION ="))
    with pytest.raises(ValueError, match="unreadable"):
        opencode.ADAPTER.apply_bump(coupled_tree, [_candidate()])


def test_discover_keeps_only_the_1x_line(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        opencode.github,
        "npm_versions",
        lambda package: ["1.0.0", "1.16.0", "2.0.6", "2.1.0"],
    )
    assert opencode.ADAPTER.discover() == ["1.0.0", "1.16.0"]


def test_version_line_is_the_bare_release() -> None:
    assert opencode.ADAPTER.version_line("1.16.0") == "1.16.0"


def test_expected_skips_cover_only_the_disabled_stock_tui_proof() -> None:
    assert all("stock_ui" in nodeid for nodeid in opencode.ADAPTER.expected_skips)
    assert not any("server_proof" in nodeid for nodeid in opencode.ADAPTER.expected_skips)
    assert not set(opencode.ADAPTER.consistency_tests) & set(opencode.ADAPTER.green_tests)


class _FakeMock:
    def __init__(self, requests: tuple[contract.RecordedRequest, ...] = ()) -> None:
        self._requests = requests

    @property
    def requests(self) -> tuple[contract.RecordedRequest, ...]:
        return self._requests

    @property
    def unhandled(self) -> tuple[contract.RecordedRequest, ...]:
        return tuple(request for request in self._requests if not request.handled)


def _sandbox(
    tmp_path: Path, requests: tuple[contract.RecordedRequest, ...] = ()
) -> contract.Sandbox:
    fixture = tmp_path / "tests/fixtures/opencode_probe_plugin.mjs"
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture.write_text("export const Probe = async () => ({})", encoding="utf-8")
    return contract.Sandbox(
        root=tmp_path,
        home=tmp_path / "home",
        config_dir=tmp_path / "config",
        bin_dir=tmp_path / "bin",
        evidence_dir=tmp_path / "evidence",
        tmux_socket=tmp_path / "tmux.sock",
        mock=_FakeMock(requests),
        mock_base_urls={"anthropic": "http://127.0.0.1:1", "openai": "http://127.0.0.1:1/v1"},
        mock_requests=tmp_path / "mock_requests.jsonl",
        env={"HOME": str(tmp_path / "home"), "PATH": "/usr/bin"},
        offline_env={"HOME": str(tmp_path / "home"), "PATH": "/usr/bin"},
    )


def _fake_run_pytest(statuses: dict[str, str]) -> Any:
    seen_env: dict[str, str] = {}

    def run(root, nodeids, *, env, expected_skips, timeout=900.0):
        del root, expected_skips, timeout
        seen_env.update(env)
        return tuple(
            contract.Check(name=nodeid, status=statuses.get(nodeid, "pass")) for nodeid in nodeids
        )

    return run, seen_env


def _statuses(**overrides: str) -> dict[str, str]:
    statuses = dict.fromkeys(opencode.ADAPTER.green_tests, "skip")
    statuses.update(dict.fromkeys(opencode.ADAPTER.consistency_tests, "pass"))
    statuses.update(overrides)
    return statuses


def _server_nodeids() -> list[str]:
    return [nodeid for nodeid in opencode.ADAPTER.green_tests if "server_proof" in nodeid]


def test_qualify_is_green_when_the_server_proofs_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(opencode_sandbox, "prewarm_catalog", lambda *args, **kwargs: True)
    run, seen_env = _fake_run_pytest(_statuses(**dict.fromkeys(_server_nodeids(), "pass")))
    monkeypatch.setattr(opencode.framework, "run_pytest", run)
    sb = _sandbox(tmp_path)
    candidate = _candidate()
    result = opencode.ADAPTER.qualify(tmp_path, candidate, sb, sb.bin_dir / "opencode")
    assert result.verdict == "green"
    assert result.harness == "opencode" and result.version == candidate
    assert result.evidence_dir == sb.evidence_dir
    assert seen_env["THEATER_OPENCODE_SERVER_CONFORMANCE"] == "1"
    assert seen_env["THEATER_OPENCODE_STOCK_CONFORMANCE"] == ""
    override = (sb.config_dir / "opencode" / "opencode.json").read_text(encoding="utf-8")
    assert sb.mock_base_urls["openai"] in override
    skips = [check for check in result.checks if check.name in opencode.ADAPTER.expected_skips]
    assert len(skips) == 2
    assert all("stock-TUI proof NOT run" in check.detail for check in skips)
    assert all("human review required" in check.detail for check in skips)
    regie = next(check for check in result.checks if "dashboard/test_content.py" in check.name)
    assert regie.status == "skip" and "tested separately" in regie.detail


@pytest.mark.parametrize("announce", [True, False])
def test_prewarm_stops_forked_serve_children(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, announce: bool
) -> None:
    sb = _sandbox(tmp_path)
    sb.bin_dir.mkdir()
    stub = sb.bin_dir / "opencode"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import os, time\n"
        "child = os.fork()\n"
        "if child == 0:\n"
        "    while True: time.sleep(10)\n"
        "open('serve.pids', 'w').write(f'{os.getpid()} {child}')\n"
        f"if {announce!r}:\n"
        "    print('opencode server listening on http://127.0.0.1:12345', flush=True)\n"
        "while True: time.sleep(10)\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    sb.env["PATH"] = f"{sb.bin_dir}:{sb.env['PATH']}"
    monkeypatch.setattr(opencode_sandbox, "_post", lambda *args, **kwargs: 200)
    monkeypatch.setattr(opencode_sandbox, "_await_catalog", lambda *args, **kwargs: True)
    assert opencode_sandbox.prewarm_catalog(sb, timeout=1) is announce
    pids = (sb.root / "opencode-prewarm" / "serve.pids").read_text().split()
    for pid in pids:
        result = subprocess.run(
            ["ps", "-o", "stat=", "-p", pid], capture_output=True, text=True, check=False
        )
        assert not result.stdout.strip() or result.stdout.strip().startswith("Z")


def test_qualify_is_tooling_when_the_catalog_pre_warm_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(opencode_sandbox, "prewarm_catalog", lambda *args, **kwargs: False)

    def _unexpected(*args: object, **kwargs: object) -> object:
        raise AssertionError("run_pytest must not run after a failed pre-warm")

    monkeypatch.setattr(opencode.framework, "run_pytest", _unexpected)
    result = opencode.ADAPTER.qualify(tmp_path, _candidate(), _sandbox(tmp_path), tmp_path / "bin")
    assert result.verdict == "tooling"
    assert result.checks[0].name == "opencode prewarm"


def test_qualify_is_inconclusive_when_nothing_proves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(opencode_sandbox, "prewarm_catalog", lambda *args, **kwargs: True)
    run, _ = _fake_run_pytest(_statuses())
    monkeypatch.setattr(opencode.framework, "run_pytest", run)
    result = opencode.ADAPTER.qualify(tmp_path, _candidate(), _sandbox(tmp_path), tmp_path / "bin")
    assert result.verdict == "inconclusive"


def test_qualify_is_tooling_on_consistency_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(opencode_sandbox, "prewarm_catalog", lambda *args, **kwargs: True)
    broken = _statuses(**dict.fromkeys(_server_nodeids(), "pass"))
    broken[opencode.ADAPTER.consistency_tests[0]] = "fail"
    run, _ = _fake_run_pytest(broken)

    def parametrized_run(*args: Any, **kwargs: Any) -> tuple[contract.Check, ...]:
        checks = run(*args, **kwargs)
        return tuple(
            contract.Check(f"{check.name}[{_candidate()}]", check.status)
            if check.name == opencode.ADAPTER.consistency_tests[0]
            else check
            for check in checks
        )

    monkeypatch.setattr(opencode.framework, "run_pytest", parametrized_run)
    result = opencode.ADAPTER.qualify(tmp_path, _candidate(), _sandbox(tmp_path), tmp_path / "bin")
    assert result.verdict == "tooling"


def test_qualify_is_red_on_a_failed_server_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(opencode_sandbox, "prewarm_catalog", lambda *args, **kwargs: True)
    server = _server_nodeids()
    run, _ = _fake_run_pytest(_statuses(**{server[0]: "fail", **dict.fromkeys(server[1:], "pass")}))
    monkeypatch.setattr(opencode.framework, "run_pytest", run)
    result = opencode.ADAPTER.qualify(tmp_path, _candidate(), _sandbox(tmp_path), tmp_path / "bin")
    assert result.verdict == "red"


@pytest.mark.parametrize("handled", [False, True])
def test_qualify_is_tooling_when_stray_routes_hit_the_mock_on_a_non_green_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, handled: bool
) -> None:
    monkeypatch.setattr(opencode_sandbox, "prewarm_catalog", lambda *args, **kwargs: True)
    stray = (
        contract.RecordedRequest(
            method="POST", path="/v1/foo", handled=handled, body_keys=(), authorized=False
        ),
    )
    server = _server_nodeids()
    statuses = _statuses(**{server[0]: "fail", **dict.fromkeys(server[1:], "pass")})
    run, _ = _fake_run_pytest(statuses)
    monkeypatch.setattr(opencode.framework, "run_pytest", run)
    result = opencode.ADAPTER.qualify(
        tmp_path, _candidate(), _sandbox(tmp_path, stray), tmp_path / "bin"
    )
    assert result.verdict == "tooling"


def test_install_failure_raises_install_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Result:
        returncode = 1
        stdout = ""
        stderr = "npm ERR! network unreachable"

    monkeypatch.setattr(opencode_pi_process, "run_group", lambda *args, **kwargs: _Result())
    with pytest.raises(contract.InstallFailed, match="exited 1"):
        opencode.ADAPTER.install(_sandbox(tmp_path), _candidate())
