"""Pinned renderings prove the JavaScript extraction preserves executable output."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from theater.harness.builtin.plugins.opencode import (
    frontend,
    frontend_inputs,
    native_plugin,
    native_plugin_v2,
)

_PROOF = "0123456789abcdef0123456789abcdef"
# Rendered by the pre-extraction code at 1ad730d6 with the fixed inputs below.
_DIGESTS = {
    "native.mjs": "075f03f8bc3f6beb74ed1a2498f4397f423cec1d98a435087f4391a43e699c15",
    "server.js": "d28b78e919644e40ca14a428c1a98f645b19b4d67a3c97aa348685dc3b0256cc",
    "tui.js": "94fdd72502bc52f5068ecb65b656364f7286f91fd49694c8eb474d5998c2ba8d",
    "frontend.mjs": "a03c06edd0cd4c9006288c83a062ac12e44299cffea257fa0470796754ba76eb",
    "inputs.mjs": "707edfa8b36d9f0b32eb559f2012151ee16ce83fe8bb5a147f51efedef23fed0",
}


@pytest.mark.requires_node
def test_rendered_assets_match_pre_extraction_bytes_and_parse(monkeypatch, tmp_path: Path) -> None:
    special = 'p " ' + chr(92) + " ☃" + chr(10)
    token = Path('/x/tok " ' + chr(92) + " ☃" + chr(10))
    monkeypatch.setattr(native_plugin.secrets, "token_hex", lambda _size: _PROOF)
    monkeypatch.setattr(native_plugin, "theater_binary", lambda: "/x/theater")
    monkeypatch.setattr(native_plugin, "catalog_path", lambda _id: Path("/x/catalog"))
    monkeypatch.setattr(native_plugin, "load_receipt_path", lambda _id: Path("/x/receipt"))
    monkeypatch.setattr(native_plugin_v2, "theater_binary", lambda: "/x/theater")
    monkeypatch.setattr(native_plugin_v2, "catalog_path", lambda _id: Path("/x/catalog"))

    v2 = native_plugin_v2.render_native_plugin_v2(special, Path("/x/config"), token, "manual")
    rendered = {
        "native.mjs": native_plugin.render_native_plugin(
            special, token, ({"permission": "edit", "pattern": special, "action": "ask"},)
        ),
        "server.js": next(text for path, text in v2.items() if path.name == "server.js"),
        "tui.js": next(text for path, text in v2.items() if path.name == "tui.js"),
        "frontend.mjs": frontend.render_opencode_tui_plugin(special, token),
        "inputs.mjs": frontend_inputs.TUI_INPUTS,
    }
    assert f'const loadProof = "{_PROOF}"' in rendered["native.mjs"]
    (tmp_path / "package.json").write_text('{"type":"module"}')
    for name, source in rendered.items():
        assert hashlib.sha256(source.encode()).hexdigest() == _DIGESTS[name]
        target = tmp_path / name
        target.write_text(source)
        result = subprocess.run(
            ["node", "--check", str(target)], capture_output=True, text=True, check=False
        )
        assert result.returncode == 0, f"{name}: {result.stderr}"


def test_template_tokens_inside_settings_are_not_expanded(monkeypatch) -> None:
    monkeypatch.setattr(native_plugin.secrets, "token_hex", lambda _size: _PROOF)
    source = native_plugin.render_native_plugin("__THEATER_TOKEN__", Path("/x/__THEATER_RULES__"))
    assert 'const participantID = "__THEATER_TOKEN__"' in source
    assert 'const tokenPath = "/x/__THEATER_RULES__"' in source
    frontend_source = frontend.render_opencode_tui_plugin(
        "__THEATER_FRONTEND_TOKEN__", Path("/x/token")
    )
    assert 'const socketPath = "__THEATER_FRONTEND_TOKEN__"' in frontend_source
