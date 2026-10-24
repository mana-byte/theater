"""The live suite's isolation guards: nothing it starts can reach the real home or network."""

from __future__ import annotations

import os
import pwd
from pathlib import Path

import pytest

from scripts.harness_compat.process import SANDBOX_ENV
from scripts.harness_compat.sandbox import make_sandbox
from tests.support.live_rig import (
    ENV_BINARY,
    ENV_EVIDENCE_DIR,
    ENV_HARNESS,
    ENV_SANDBOX_ROOT,
    ENV_VERSION,
    LiveSession,
    assert_isolated,
    hermetic_environment,
)

_PIPELINE = {
    SANDBOX_ENV: "token",
    ENV_HARNESS: "acme",
    ENV_BINARY: "/opt/acme/bin/acme",
    ENV_VERSION: "1.2.3",
    ENV_SANDBOX_ROOT: "/tmp/sandbox",
    ENV_EVIDENCE_DIR: "/tmp/sandbox/e",
}


def test_a_session_needs_every_pipeline_variable() -> None:
    for name in _PIPELINE:
        partial = {key: value for key, value in _PIPELINE.items() if key != name}
        with pytest.raises(RuntimeError, match=name):
            LiveSession.from_environment(partial)


def test_a_session_reads_the_pipeline_contract() -> None:
    session = LiveSession.from_environment(_PIPELINE)

    assert (session.harness, session.version, session.token) == ("acme", "1.2.3", "token")
    assert session.binary == Path("/opt/acme/bin/acme")


def test_the_hermetic_environment_keeps_every_home_inside_the_sandbox(tmp_path: Path) -> None:
    env = hermetic_environment(tmp_path, token="t", binary_dir=tmp_path / "bin", extra={})

    for name in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "TMPDIR", "THEATER_HOME"):
        assert Path(env[name]).is_relative_to(tmp_path), name
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:9" == env["ALL_PROXY"]
    assert env[SANDBOX_ENV] == "t"


def test_the_rig_and_the_pipeline_sandbox_share_one_environment_policy(tmp_path: Path) -> None:
    with make_sandbox("acme", root=tmp_path / "sandbox") as sb:
        rig = hermetic_environment(tmp_path / "rig", token="t", binary_dir=tmp_path, extra={})

    assert set(sb.offline_env) <= set(rig), "the rig drops nothing the pipeline's policy sets"
    for name in ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "ALL_PROXY"):
        assert rig[name] == "http://127.0.0.1:9", name
    assert rig["GIT_CONFIG_GLOBAL"] == os.devnull and rig["PIP_CONFIG_FILE"] == os.devnull
    assert Path(rig["NPM_CONFIG_CACHE"]).is_relative_to(tmp_path / "rig")


def test_the_hermetic_environment_drops_every_host_variable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "leak")
    monkeypatch.setenv("OPENAI_BASE_URL", "http://example.invalid")

    env = hermetic_environment(tmp_path, token="t", binary_dir=tmp_path, extra={"EXTRA": "1"})

    assert "ANTHROPIC_API_KEY" not in env and "OPENAI_BASE_URL" not in env
    assert env["EXTRA"] == "1"
    assert env["PATH"].split(os.pathsep)[0] == str(tmp_path)


def test_a_path_outside_the_sandbox_aborts_the_session(tmp_path: Path) -> None:
    root = tmp_path / "sandbox"
    root.mkdir()

    assert_isolated(root, root / "inside")
    with pytest.raises(pytest.exit.Exception):
        assert_isolated(root, tmp_path / "elsewhere")


def test_a_sandbox_overlapping_the_live_theater_home_aborts_the_session() -> None:
    live = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".theater"

    with pytest.raises(pytest.exit.Exception):
        assert_isolated(live, live / "inside")
    with pytest.raises(pytest.exit.Exception):
        assert_isolated(live.parent, live)
