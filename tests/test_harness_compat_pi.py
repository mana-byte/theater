"""Pi harness-compat adapter: gate parsing, coherent bumps, and qualification."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scripts.harness_compat.adapters import pi, pi_bump
from scripts.harness_compat.contract import Check, InstallFailed
from scripts.harness_compat.probe import _probe
from scripts.harness_compat.sandbox import make_sandbox


def _copy_coupled(root: Path, destination: Path) -> None:
    for relative in pi_bump._COUPLED:
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / relative, target)


def test_verified_parses_exclusive_ceiling_and_known_bad(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    _copy_coupled(root, tmp_path)
    evidence = tmp_path / "tests/fixtures/harness_compat/pi/0.99.1.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text("{}", encoding="utf-8")

    gate = pi.ADAPTER.verified(tmp_path)

    assert gate.floor == "0.80.8"
    assert gate.ceiling == "0.99.2"
    assert gate.known_bad == frozenset({"0.85.0"})
    assert gate.evidenced == frozenset({"0.99.1"})
    assert gate.admits("0.99.1") and not gate.admits("0.85.0")

    probe = tmp_path / pi_bump._PROBE
    source = probe.read_text(encoding="utf-8")
    source = source.replace(
        "in_080 = (0, 80, 8) <= parsed < (0, 85, 0)",
        "in_080 = (0, 80, 9) <= parsed < (0, 85, 2)",
    ).replace(
        "in_qualified = (0, 85, 1) <= parsed <= (0, 99, 1)",
        "in_qualified = (0, 85, 3) <= parsed <= (0, 99, 1)",
    )
    probe.write_text(source, encoding="utf-8")
    drifted = pi.ADAPTER.verified(tmp_path)
    assert drifted.floor == "0.80.9"
    assert drifted.known_bad == frozenset({"0.85.2"})


def test_apply_bump_updates_every_coupled_gate_and_is_idempotent(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    shutil.copytree(root / "theater", tmp_path / "theater")
    _copy_coupled(root, tmp_path)
    stub = tmp_path / "pi-stub"
    previous = "0.99.1"
    greens = ("0.99.2", "0.99.3", "0.99.4", "1.0.0")
    for version in greens:
        before = {path: (tmp_path / path).read_bytes() for path in pi_bump._COUPLED}
        changed = pi.ADAPTER.apply_bump(tmp_path, [version])
        assert changed == list(pi_bump._COUPLED)
        assert all((tmp_path / path).read_bytes() != before[path] for path in changed)
        gate = pi.ADAPTER.verified(tmp_path)
        assert gate.ceiling == pi_bump._next_patch(version)
        assert gate.admits(version) and not gate.admits(gate.ceiling)
        probe = (tmp_path / pi_bump._PROBE).read_text(encoding="utf-8")
        old_gate = f"in_qualified = (0, 85, 1) <= parsed <= ({pi_bump._tuple(previous)})"
        assert old_gate not in probe
        old_range = f">=0.80.8,<0.85.0 || >=0.85.1,<={previous}"
        for path in (pi_bump._PROBE, pi_bump._MANIFEST, pi_bump._DOC):
            assert old_range not in (tmp_path / path).read_text(encoding="utf-8")
        for fixture in (tmp_path / "tests/fixtures/pi_native_control").glob("*.mts"):
            assert pi_bump._major_guard(previous) not in fixture.read_text(encoding="utf-8")
        for cache in (tmp_path / "theater").rglob("__pycache__"):
            shutil.rmtree(cache)
        supported, reason = _probe(pi.ADAPTER, tmp_path, version, stub)
        assert supported is True, reason
        supported, reason = _probe(pi.ADAPTER, tmp_path, gate.ceiling, stub)
        assert supported is False, reason
        snapshots = {path: (tmp_path / path).read_bytes() for path in changed}
        assert pi.ADAPTER.apply_bump(tmp_path, [version]) == []
        assert snapshots == {path: (tmp_path / path).read_bytes() for path in changed}
        previous = version

    bridge_test = (tmp_path / "tests/test_pi_native_bridge.py").read_text(encoding="utf-8")
    refused = pi_bump._REFUSED_RE.search(bridge_test)
    assert refused is not None
    assert '"1.0.0"' not in refused.group("versions")
    assert '"1.0.1"' in refused.group("versions")
    assert "stable 0.x.y" not in bridge_test
    assert all(f'    "{version}",' in bridge_test for version in greens)
    for fixture in (tmp_path / "tests/fixtures/pi_native_control").glob("*.mts"):
        source = fixture.read_text(encoding="utf-8")
        assert "const major = Number(versionMatch[1]);" in source
        assert "major === 0 && minor === 85 && patch === 0" in source
        assert "major === 1 && minor === 0 && patch > 0" in source


def test_qualify_uses_offline_environment_and_maps_errors_to_inconclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
        result = pi.ADAPTER.qualify(tmp_path, "0.99.1", sandbox, binary)

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
        pi.ADAPTER.install(sandbox, "0.99.1")
