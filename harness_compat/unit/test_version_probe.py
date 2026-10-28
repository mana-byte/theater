"""The version probe runs the CLI from a throwaway home, never the real one."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from harness_compat.suite_runner import installed_version


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
        'echo "$HOME|$XDG_CONFIG_HOME|$TMPDIR|$PYTHONDONTWRITEBYTECODE" '
        f'> "{tmp_path}/seen"; echo 4.5.6',
    )

    installed_version(probe)

    home, config, temp, no_bytecode = (tmp_path / "seen").read_text().strip().split("|")
    assert home != os.environ.get("HOME")
    assert Path(config).parent == Path(temp).parent == Path(home).parent
    assert no_bytecode == "1"
    assert not Path(home).parent.exists(), "the whole sandbox is removed afterwards"


def test_stderr_is_read_only_when_stdout_names_no_version(tmp_path: Path) -> None:
    assert installed_version(_cli(tmp_path, 'echo "opencode v2.1.4" >&2')) == "2.1.4"
    assert installed_version(_cli(tmp_path, 'echo "7.8.9"; echo "node 22.1.0" >&2')) == "7.8.9"


def test_a_cli_that_names_no_version_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot read a version"):
        installed_version(_cli(tmp_path, 'echo "usage: acme"'))


def test_the_cli_starts_in_the_throwaway_home_offline_never_in_the_checkout(
    tmp_path: Path,
) -> None:
    probe = _cli(
        tmp_path,
        f'echo "$(pwd -P)|$HOME|$HTTPS_PROXY|$http_proxy" > "{tmp_path}/seen"; echo 4.5.6',
    )

    installed_version(probe)

    cwd, home, proxy, lower = (tmp_path / "seen").read_text().strip().split("|")
    assert Path(cwd).resolve() == Path(home).resolve()
    assert Path(cwd).resolve() != Path.cwd().resolve()
    assert proxy == lower == "http://127.0.0.1:9"


def test_a_failed_exit_is_refused_even_when_it_printed_a_version(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exited 3"):
        installed_version(_cli(tmp_path, "echo 1.2.3; exit 3"))


@pytest.mark.parametrize(
    "printed", ["1.2.3-beta.1", "1.2.3+build5", "1.2.3.4", "01.2.3", "v1.02.3", "1.2.3rc1"]
)
def test_a_release_that_is_not_canonical_stable_is_refused(tmp_path: Path, printed: str) -> None:
    with pytest.raises(ValueError, match="not a canonical stable release"):
        installed_version(_cli(tmp_path, f'echo "acme {printed}"'))


def test_two_different_releases_are_ambiguous(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="ambiguous"):
        installed_version(_cli(tmp_path, 'echo "acme 1.2.3 (update to 1.2.4)"'))


def test_a_pre_release_on_stdout_is_not_masked_by_a_clean_stderr(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a canonical stable release"):
        installed_version(_cli(tmp_path, 'echo "1.2.3-rc.1"; echo "1.2.3" >&2'))


@pytest.mark.parametrize(
    "printed",
    [
        "1.2.3",
        "v1.2.3",
        "(1.2.3)",
        "acme 1.2.3, build 9",
        "1.2.3 1.2.3",
        "claude 1.2.3 (Claude Code)",
    ],
)
def test_the_same_release_named_any_number_of_times_is_one_release(
    tmp_path: Path, printed: str
) -> None:
    assert installed_version(_cli(tmp_path, f'echo "{printed}"')) == "1.2.3"
