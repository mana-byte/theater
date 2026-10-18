"""Pi harness-compat adapter: gate parsing, coherent bumps, and qualification."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scripts.harness_compat.adapters import pi, pi_bump, pi_pins
from scripts.harness_compat.contract import Check, InstallFailed, version_key
from scripts.harness_compat.probe import _probe
from scripts.harness_compat.sandbox import make_sandbox
from tests.support.paths import REPO_ROOT


def _copy_gate(root: Path, destination: Path) -> None:
    relative = pi_bump._CONSTANTS
    target = destination / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / relative, target)


def _previous_patch(version: str) -> str:
    major, minor, patch = version_key(version)
    assert patch > 0
    return f"{major}.{minor}.{patch - 1}"


def _next_major(version: str) -> str:
    major, _, _ = version_key(version)
    return f"{major + 1}.0.0"


def _current_upper(root: Path) -> str:
    ceiling = pi.ADAPTER.verified(root).ceiling
    assert ceiling is not None
    return _previous_patch(ceiling)


def test_verified_reads_plugin_literal_and_fails_closed(tmp_path: Path) -> None:
    root = REPO_ROOT
    _copy_gate(root, tmp_path)
    expected = pi.ADAPTER.verified(tmp_path)
    upper = _current_upper(tmp_path)
    evidence = tmp_path / f"tests/fixtures/harness_compat/pi/{upper}.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("{}", encoding="utf-8")

    gate = pi.ADAPTER.verified(tmp_path)
    assert gate.floor == expected.floor
    assert gate.ceiling == expected.ceiling
    assert gate.known_bad == expected.known_bad
    assert gate.evidenced == frozenset({upper})
    bad = next(iter(gate.known_bad))
    assert gate.admits(upper) and not gate.admits(bad)

    source = tmp_path / pi_bump._CONSTANTS
    original = source.read_text(encoding="utf-8")
    source.write_text(
        original.replace("PI_FRONTEND_QUALIFIED_MAX =", "PI_FRONTEND_QUALIFIED_LIMIT =")
    )
    with pytest.raises(ValueError, match="missing Pi gate anchor"):
        pi.ADAPTER.verified(tmp_path)
    with pytest.raises(ValueError, match="missing Pi gate anchor"):
        pi.ADAPTER.apply_bump(tmp_path, [pi_bump._next_patch(upper)])

    source.write_text(
        original.replace("PI_FRONTEND_QUALIFIED_EXCLUDED =", "PI_FRONTEND_QUALIFIED_SKIPPED =")
    )
    with pytest.raises(ValueError, match="missing Pi gate anchor"):
        pi.ADAPTER.verified(tmp_path)


def test_apply_bump_changes_one_value_and_is_idempotent(tmp_path: Path) -> None:
    root = REPO_ROOT
    shutil.copytree(root / "theater", tmp_path / "theater")
    initial = pi.ADAPTER.verified(tmp_path)
    assert initial.ceiling is not None
    previous = _previous_patch(initial.ceiling)
    greens = (
        pi_bump._next_patch(previous),
        pi_bump._next_patch(pi_bump._next_patch(previous)),
        _next_major(previous),
    )
    for version in greens:
        before = (tmp_path / pi_bump._CONSTANTS).read_text(encoding="utf-8")
        changed = pi.ADAPTER.apply_bump(tmp_path, [version])
        assert changed == [pi_bump._CONSTANTS]
        after = (tmp_path / pi_bump._CONSTANTS).read_text(encoding="utf-8")
        assert before != after
        assert pi.ADAPTER.apply_bump(tmp_path, [version]) == []
        assert (tmp_path / pi_bump._CONSTANTS).read_text(encoding="utf-8") == after
        gate = pi.ADAPTER.verified(tmp_path)
        assert gate.ceiling == pi_bump._next_patch(version)
        assert gate.admits(version) and not gate.admits(gate.ceiling)
        for cache in (tmp_path / "theater").rglob("__pycache__"):
            shutil.rmtree(cache)
        supported, reason = _probe(pi.ADAPTER, tmp_path, version, tmp_path / "pi-stub")
        assert supported is True, reason
        supported, reason = _probe(pi.ADAPTER, tmp_path, gate.ceiling, tmp_path / "pi-stub")
        assert supported is False, reason
        previous = version
    assert pi.ADAPTER.apply_bump(tmp_path, [initial.floor]) == []


def test_qualify_uses_offline_environment_and_maps_errors_to_inconclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = REPO_ROOT
    calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def fake_run_pytest(root, nodeids, *, env, expected_skips, timeout=900):
        del root, expected_skips, timeout
        calls.append((tuple(nodeids), dict(env)))
        if tuple(nodeids) == pi.ADAPTER.consistency_tests:
            return tuple(Check(nodeid, "pass") for nodeid in nodeids)
        return (Check(nodeids[0], "error", "fake runner failure"),)

    monkeypatch.setattr(pi.framework, "run_pytest", fake_run_pytest)
    with make_sandbox("pi", root=tmp_path / "sandbox") as sandbox:
        binary = sandbox.bin_dir / "pi"
        binary.touch()
        result = pi.ADAPTER.qualify(tmp_path, _current_upper(root), sandbox, binary)

    assert result.verdict == "inconclusive"
    assert [call[0] for call in calls] == [
        pi.ADAPTER.consistency_tests,
        pi.ADAPTER.green_tests,
    ]
    assert all(call[1]["HTTP_PROXY"] == "http://127.0.0.1:9" for call in calls)
    assert all(call[1]["PATH"].split(":", 1)[0] == str(binary.parent) for call in calls)


def test_install_failure_raises_install_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Failed:
        returncode = 1
        stdout = ""
        stderr = "fake npm failure"

    monkeypatch.setattr(pi.opencode_pi_process, "run_group", lambda *args, **kwargs: Failed())
    with (
        make_sandbox("pi", root=tmp_path / "sandbox") as sandbox,
        pytest.raises(InstallFailed, match="fake npm failure"),
    ):
        pi.ADAPTER.install(sandbox, _current_upper(REPO_ROOT))


def test_pinned_specs_pin_own_scope_simple_ranges_only() -> None:
    dependencies = {
        "@earendil-works/caret": "^2.3.4",
        "@earendil-works/tilde": "~5.6.7",
        "@earendil-works/exact": "9.9.9",
        "@earendil-works/span": ">=1 <2",
        "@earendil-works/any": "*",
        "@earendil-works/local": "workspace:*",
        "@earendil-works/remote": "git+https://example.invalid/x.git",
        "@earendil-works/loose": "^2.3",
        "other-scope": "^1.2.3",
        "@elsewhere/pkg": "~1.2.3",
        "@earendil-works/number": 4,
    }
    assert pi_pins.pinned_specs(dependencies) == [
        "@earendil-works/caret@2.3.4",
        "@earendil-works/tilde@5.6.7",
    ]
    for odd in (None, [], "text", {}):
        assert pi_pins.pinned_specs(odd) == []
