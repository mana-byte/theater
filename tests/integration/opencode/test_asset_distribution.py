"""OpenCode JavaScript assets render from the built, installed distribution."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from tests.support.paths import REPO_ROOT

_ASSETS = {
    "native_plugin.js",
    "tui_recovery.js",
}
_PREFIX = "theater/harness/builtin/plugins/opencode/assets/"


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout


@pytest.mark.integration
def test_built_wheel_and_sdist_install_all_executable_assets(tmp_path: Path) -> None:
    env = {**os.environ, "UV_FROZEN": "1"}
    env.pop("PYTHONPATH", None)
    dist = tmp_path / "dist"
    _run(
        ["uv", "build", "--wheel", "--sdist", "--out-dir", str(dist)],
        cwd=REPO_ROOT,
        env=env,
    )
    wheel = next(dist.glob("theater-*.whl"))
    sdist = next(dist.glob("theater-*.tar.gz"))
    with zipfile.ZipFile(wheel) as archive:
        assert {_PREFIX + name for name in _ASSETS} <= set(archive.namelist())
    with tarfile.open(sdist, "r:gz") as archive:
        names = archive.getnames()
        assert all(any(name.endswith(_PREFIX + asset) for name in names) for asset in _ASSETS)

    venv = tmp_path / "venv"
    _run(
        ["uv", "venv", "--python", sys.executable, str(venv)],
        cwd=tmp_path,
        env=env,
    )
    python = venv / "bin" / "python"
    _run(
        ["uv", "pip", "install", "--python", str(python), "--no-sources", str(wheel)],
        cwd=tmp_path,
        env=env,
    )
    proof = """
import json
import sys
from importlib.resources import files
from pathlib import Path
from theater.harness.builtin.plugins import opencode
from theater.harness.builtin.plugins.opencode.native_plugin import render_native_plugin

root = files(opencode).joinpath('assets')
assets = json.loads(sys.argv[1])
assert all(root.joinpath(name).is_file() for name in assets)
files = render_native_plugin('p', Path('/x/config'), Path('/x/token'), 'manual')
assert any(path.name == 'server.js' and 'export default' in text for path, text in files.items())
assert any(path.name == 'tui.js' and 'root.render = render' in text for path, text in files.items())
print(json.dumps({'package': str(Path(opencode.__file__).resolve()), 'assets': sorted(assets)}))
"""
    output = _run(
        [str(python), "-I", "-c", proof, json.dumps(sorted(_ASSETS))],
        cwd=tmp_path,
        env=env,
    )
    observed = json.loads(output)
    assert Path(observed["package"]).is_relative_to(venv)
    assert observed["assets"] == sorted(_ASSETS)
