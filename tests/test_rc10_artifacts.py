"""Built-distribution boundaries, without importing from this checkout."""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import zipfile
from collections.abc import Iterable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.0.0rc10"
UV = shutil.which("uv")


@pytest.mark.skipif(UV is None, reason="artifact smoke tests require uv")
def test_built_artifacts_install_as_independent_candidates(tmp_path: Path) -> None:
    """The two distributions must not accidentally rely on this workspace."""
    dist = tmp_path / "dist"
    _run((_uv(), "build", "--all-packages", "--out-dir", str(dist)), cwd=ROOT)
    artifacts = _artifacts(dist)

    assert set(artifacts) == {"regie", "theater"}
    assert all(set(by_kind) == {"sdist", "wheel"} for by_kind in artifacts.values())
    _assert_theater_contents(artifacts["theater"].values())
    _assert_regie_contents(artifacts["regie"].values())

    for kind in ("wheel", "sdist"):
        _smoke_theater(tmp_path / f"theater-{kind}", artifacts["theater"][kind])
        _smoke_regie(
            tmp_path / f"regie-{kind}",
            artifacts["theater"][kind],
            artifacts["regie"][kind],
        )


def _artifacts(dist: Path) -> dict[str, dict[str, Path]]:
    artifacts: dict[str, dict[str, Path]] = {}
    for artifact in dist.iterdir():
        if artifact.name.endswith(".whl"):
            kind = "wheel"
        elif artifact.name.endswith(".tar.gz"):
            kind = "sdist"
        else:
            continue
        name = artifact.name.split("-", maxsplit=1)[0]
        artifacts.setdefault(name, {})[kind] = artifact
    return artifacts


def _assert_theater_contents(artifacts: Iterable[Path]) -> None:
    for artifact in artifacts:
        names, metadata = _contents(artifact)
        assert f"Version: {VERSION}" in metadata
        assert "Requires-Dist: textual" not in metadata
        assert "Requires-Dist: regie" not in metadata
        assert not any("packages/regie/" in name or "/regie/" in name for name in names)
        for required in (
            "theater/frontend/schemas/methods.json",
            "theater/harness/builtin/plugins/pi/theater_mcp_bridge.ts",
            "theater/harness/builtin/plugins/pi/pi_startup_filter.cjs",
            "theater/pricing/model_prices.json",
            "theater/skills/builtin/theater-orchestrate/SKILL.md",
            "theater/daemon/migrations/script.py.mako",
        ):
            assert any(name.endswith(required) for name in names), (artifact.name, required)


def _assert_regie_contents(artifacts: Iterable[Path]) -> None:
    for artifact in artifacts:
        names, metadata = _contents(artifact)
        assert f"Version: {VERSION}" in metadata
        assert f"Requires-Dist: theater=={VERSION}" in metadata
        for required in (
            "regie/app.py",
            "regie/bridge/runtime.py",
            "regie/tmux/presentation.py",
            "regie/trajectory/rich/view.py",
        ):
            assert any(name.endswith(required) for name in names), (artifact.name, required)


def _contents(artifact: Path) -> tuple[list[str], str]:
    if artifact.suffix == ".whl":
        with zipfile.ZipFile(artifact) as archive:
            names = archive.namelist()
            metadata_name = next(name for name in names if name.endswith(".dist-info/METADATA"))
            return names, archive.read(metadata_name).decode("utf-8")
    with tarfile.open(artifact) as archive:
        names = archive.getnames()
        metadata_name = next(name for name in names if name.endswith("PKG-INFO"))
        metadata = archive.extractfile(metadata_name)
        assert metadata is not None
        return names, metadata.read().decode("utf-8")


def _smoke_theater(candidate: Path, artifact: Path) -> None:
    python, environment = _candidate(candidate)
    _run(
        (_uv(), "pip", "install", "--python", str(python), "--no-sources", str(artifact)),
        cwd=candidate,
        environment=environment,
    )
    _run(
        (
            str(python),
            "-c",
            "import importlib.util; import theater.frontend; "
            "assert importlib.util.find_spec('regie') is None; "
            "assert importlib.util.find_spec('textual') is None",
        ),
        cwd=candidate,
        environment=environment,
    )
    _run((str(python.parent / "theater"), "--help"), cwd=candidate, environment=environment)


def _smoke_regie(candidate: Path, theater: Path, regie: Path) -> None:
    python, environment = _candidate(candidate)
    _run(
        (
            _uv(),
            "pip",
            "install",
            "--python",
            str(python),
            "--no-sources",
            str(theater),
            str(regie),
        ),
        cwd=candidate,
        environment=environment,
    )
    _run(
        (
            str(python),
            "-c",
            "import regie.app, regie.bridge.runtime, regie.tmux.presentation; "
            "import theater.frontend",
        ),
        cwd=candidate,
        environment=environment,
    )
    _run((str(python.parent / "regie"), "--help"), cwd=candidate, environment=environment)


def _candidate(root: Path) -> tuple[Path, dict[str, str]]:
    root.mkdir()
    environment = _candidate_environment(root)
    _run((_uv(), "venv", "--no-project", str(root / "venv")), cwd=root, environment=environment)
    return root / "venv" / "bin" / "python", environment


def _candidate_environment(root: Path) -> dict[str, str]:
    environment = os.environ.copy()
    for name in (
        "PYTHONHOME",
        "PYTHONPATH",
        "TMUX",
        "TMUX_PANE",
        "THEATER_ID",
        "THEATER_PROVIDER_CREDENTIAL",
        "THEATER_PROVIDER_GENERATION",
        "THEATER_PROVIDER_ID",
        "VIRTUAL_ENV",
        "UV_PROJECT",
        "UV_PROJECT_ENVIRONMENT",
        "UV_WORKING_DIR",
    ):
        environment.pop(name, None)
    environment["THEATER_HOME"] = str(root / "home")
    environment["TMUX_TMPDIR"] = str(root / "tmux")
    return environment


def _uv() -> str:
    assert UV is not None
    return UV


def _run(
    command: tuple[str, ...],
    *,
    cwd: Path,
    environment: dict[str, str] | None = None,
) -> None:
    completed = subprocess.run(
        command,
        check=False,
        cwd=cwd,
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    assert completed.returncode == 0, f"{' '.join(command)} failed:\n{completed.stdout}"
