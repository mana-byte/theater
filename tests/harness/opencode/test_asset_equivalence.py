"""Pinned renderings prove the JavaScript extraction preserves executable output."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.support.paths import REPO_ROOT
from theater.harness.builtin.plugins.opencode import (
    native_plugin,
    tui_recovery,
)

# Rendered by the pre-extraction code at 1ad730d6 with the fixed inputs below.
_DIGESTS = {
    "server.js": "d28b78e919644e40ca14a428c1a98f645b19b4d67a3c97aa348685dc3b0256cc",
    "tui.js": "94fdd72502bc52f5068ecb65b656364f7286f91fd49694c8eb474d5998c2ba8d",
}


@pytest.mark.requires_node
def test_rendered_assets_match_pre_extraction_bytes_and_parse(monkeypatch, tmp_path: Path) -> None:
    special = 'p " ' + chr(92) + " ☃" + chr(10)
    token = Path('/x/tok " ' + chr(92) + " ☃" + chr(10))
    monkeypatch.setattr(native_plugin, "theater_binary", lambda: "/x/theater")
    monkeypatch.setattr(native_plugin, "catalog_path", lambda _id: Path("/x/catalog"))

    v2 = native_plugin.render_native_plugin(special, Path("/x/config"), token, "manual")
    rendered = {
        "server.js": next(text for path, text in v2.items() if path.name == "server.js"),
        "tui.js": next(text for path, text in v2.items() if path.name == "tui.js"),
    }
    (tmp_path / "package.json").write_text('{"type":"module"}')
    for name, source in rendered.items():
        assert hashlib.sha256(source.encode()).hexdigest() == _DIGESTS[name]
        target = tmp_path / name
        target.write_text(source, encoding="utf-8")
        result = subprocess.run(
            ["node", "--check", str(target)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, f"{name}: {result.stderr}"


@pytest.mark.requires_node
def test_fixed_renderings_in_an_ascii_locale() -> None:
    env = {**os.environ, "LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"}
    env.pop("PYTEST_ADDOPTS", None)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            f"{__file__}::test_rendered_assets_match_pre_extraction_bytes_and_parse",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0 and "1 passed" in result.stdout, result.stdout + result.stderr


def test_native_settings_are_not_reexpanded() -> None:
    rendered = native_plugin.render_native_plugin(
        "__THEATER_SETTINGS__", Path("/x/config"), Path("/x/token"), "yolo"
    )
    source = next(text for path, text in rendered.items() if path.name == "server.js")
    assert '"participantID": "__THEATER_SETTINGS__"' in source


def test_recovery_keeps_ordered_replacement() -> None:
    recovery = tui_recovery.render_tui_recovery("__QUALIFIED_VERSIONS__")
    qualified = json.dumps(list(tui_recovery._QUALIFIED_VERSIONS))
    assert f'  id: "{qualified}",' in recovery
