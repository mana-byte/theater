"""The version probe runs the CLI from a throwaway home, never the real one."""

from __future__ import annotations

import os
import stat
from pathlib import Path

from scripts.harness_compat.suite_runner import installed_version


def _cli(tmp_path: Path, body: str) -> Path:
    script = tmp_path / "acme"
    script.write_text(f"#!/bin/sh\n{body}\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_the_version_is_read_from_the_clis_own_output(tmp_path: Path) -> None:
    assert installed_version(_cli(tmp_path, 'echo "acme-cli v1.22.3 (build 7)"')) == "1.22.3"


def test_the_cli_never_sees_the_real_home_or_config(tmp_path: Path) -> None:
    probe = _cli(
        tmp_path,
        f'echo "$HOME|$XDG_CONFIG_HOME" > "{tmp_path}/seen"; echo 4.5.6',
    )

    installed_version(probe)

    home, config = (tmp_path / "seen").read_text().strip().split("|")
    assert home != os.environ.get("HOME") and config.startswith(home)
    assert not Path(home).exists(), "the throwaway home is removed afterwards"
