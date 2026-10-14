"""Pi harness-compat adapter: gate parsing, coherent bumps, and qualification."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from scripts.harness_compat.adapters import pi, pi_bump
from scripts.harness_compat.contract import Check, InstallFailed, version_key
from scripts.harness_compat.probe import _probe
from scripts.harness_compat.sandbox import make_sandbox


def _copy_coupled(root: Path, destination: Path) -> None:
    for relative in pi_bump._COUPLED:
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


def test_verified_parses_exclusive_ceiling_and_known_bad(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    _copy_coupled(root, tmp_path)
    expected = pi.ADAPTER.verified(tmp_path)
    assert expected.ceiling is not None
    upper = _previous_patch(expected.ceiling)
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

    probe = tmp_path / pi_bump._PROBE
    source = probe.read_text(encoding="utf-8")
    assert gate.floor is not None
    drifted_floor = pi_bump._next_patch(gate.floor)
    drifted_bad = pi_bump._next_patch(pi_bump._next_patch(bad))
    resume = pi_bump._next_patch(bad)
    drifted_resume = pi_bump._next_patch(drifted_bad)
    source = source.replace(
        f"in_080 = ({pi_bump._tuple(gate.floor)}) <= parsed < ({pi_bump._tuple(bad)})",
        f"in_080 = ({pi_bump._tuple(drifted_floor)}) <= parsed < ({pi_bump._tuple(drifted_bad)})",
    ).replace(
        f"in_qualified = ({pi_bump._tuple(resume)}) <= parsed <= ({pi_bump._tuple(upper)})",
        f"in_qualified = ({pi_bump._tuple(drifted_resume)}) <= parsed <= ({pi_bump._tuple(upper)})",
    )
    probe.write_text(source, encoding="utf-8")
    drifted = pi.ADAPTER.verified(tmp_path)
    assert drifted.floor == drifted_floor
    assert drifted.known_bad == frozenset({drifted_bad})


def test_apply_bump_updates_every_coupled_gate_and_is_idempotent(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    shutil.copytree(root / "theater", tmp_path / "theater")
    _copy_coupled(root, tmp_path)
    initial = pi.ADAPTER.verified(tmp_path)
    assert initial.ceiling is not None and initial.floor is not None
    previous = _previous_patch(initial.ceiling)
    bad = next(iter(initial.known_bad))
    resume = pi_bump._next_patch(bad)
    first = pi_bump._next_patch(previous)
    second = pi_bump._next_patch(first)
    greens = (first, second, _next_major(previous))
    for version in greens:
        before = {path: (tmp_path / path).read_bytes() for path in pi_bump._COUPLED}
        changed = pi.ADAPTER.apply_bump(tmp_path, [version])
        assert changed == list(pi_bump._COUPLED)
        assert all((tmp_path / path).read_bytes() != before[path] for path in changed)
        gate = pi.ADAPTER.verified(tmp_path)
        assert gate.ceiling == pi_bump._next_patch(version)
        assert gate.admits(version) and not gate.admits(gate.ceiling)
        probe = (tmp_path / pi_bump._PROBE).read_text(encoding="utf-8")
        old_gate = (
            f"in_qualified = ({pi_bump._tuple(resume)}) <= parsed <= ({pi_bump._tuple(previous)})"
        )
        assert old_gate not in probe
        old_range = f">={initial.floor},<{bad} || >={resume},<={previous}"
        for path in (pi_bump._PROBE, pi_bump._MANIFEST, pi_bump._DOC):
            assert old_range not in (tmp_path / path).read_text(encoding="utf-8")
        for fixture in (tmp_path / "tests/fixtures/pi_native_control").glob("*.mts"):
            assert pi_bump._major_guard(previous) not in fixture.read_text(encoding="utf-8")
        for cache in (tmp_path / "theater").rglob("__pycache__"):
            shutil.rmtree(cache)
        supported, reason = _probe(pi.ADAPTER, tmp_path, version, tmp_path / "pi-stub")
        assert supported is True, reason
        supported, reason = _probe(pi.ADAPTER, tmp_path, gate.ceiling, tmp_path / "pi-stub")
        assert supported is False, reason
        snapshots = {path: (tmp_path / path).read_bytes() for path in changed}
        assert pi.ADAPTER.apply_bump(tmp_path, [version]) == []
        assert snapshots == {path: (tmp_path / path).read_bytes() for path in changed}
        previous = version

    bridge_test = (tmp_path / "tests/test_pi_native_bridge.py").read_text(encoding="utf-8")
    refused = pi_bump._REFUSED_RE.search(bridge_test)
    assert refused is not None
    assert f'"{greens[-1]}"' not in refused.group("versions")
    assert f'"{pi_bump._next_patch(greens[-1])}"' in refused.group("versions")
    assert "stable 0.x.y" not in bridge_test
    assert all(f'    "{version}",' in bridge_test for version in greens)
    for fixture in (tmp_path / "tests/fixtures/pi_native_control").glob("*.mts"):
        source = fixture.read_text(encoding="utf-8")
        assert "const major = Number(versionMatch[1]);" in source
        bad_major, bad_minor, bad_patch = version_key(bad)
        assert f"major === {bad_major} && minor === {bad_minor} && patch === {bad_patch}" in source
        assert pi_bump._major_guard(greens[-1]) in source
        if "function versionAtLeast(" in source:
            assert "const major = Number(match[1]);" in source
            assert "const actualMinor = Number(match[2]);" in source
            assert "const actualPatch = Number(match[3]);" in source
            assert "return major > 0 || actualMinor > minor" in source


def test_qualify_uses_offline_environment_and_maps_errors_to_inconclusive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = Path(__file__).parents[1]
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
        pi.ADAPTER.install(sandbox, _current_upper(Path(__file__).parents[1]))
