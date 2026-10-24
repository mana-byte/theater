"""The rig blames the stock CLI only when every suite component is proven alive."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from scripts.harness_compat.mock import MockServer
from tests.support import live_rig
from tests.support.live_rig import LiveRig, LiveSession


class _Client:
    """Just what the health check reads: a connection flag and the provider registry."""

    def __init__(self, *healths: str, connected: bool = True, unreadable: bool = False) -> None:
        self.connected = connected
        self._healths = healths
        self._unreadable = unreadable
        self.providers = self

    async def list(self) -> Any:
        if self._unreadable:
            raise OSError("registry unreadable")
        items = tuple(SimpleNamespace(health=health) for health in self._healths)
        return SimpleNamespace(value=SimpleNamespace(items=items))


def _rig(
    tmp_path: Path, client: _Client, *, daemon: bool = True, mock_serving: bool = True
) -> tuple[LiveRig, MockServer]:
    session = LiveSession("acme", Path("acme"), "1", tmp_path, tmp_path / "e", "token")
    mock = MockServer(tmp_path / "requests.jsonl")
    if mock_serving:
        mock.start()
    run = tmp_path / "th" / "var" / "run"
    run.mkdir(parents=True)
    if daemon:
        (run / "daemon.pid").write_text(str(os.getpid()))
    rig = LiveRig(
        session,
        cast(Any, None),
        tmp_path,
        {"TMUX_TMPDIR": str(tmp_path / "t")},
        cast(Any, None),
        mock,
        cast(Any, client),
        tmp_path / "w",
        tmp_path / "m",
        "scenario",
    )
    return rig, mock


@pytest.mark.parametrize(
    ("setup", "tmux", "broken"),
    [
        pytest.param({"daemon": False}, True, "the daemon", id="daemon-gone"),
        pytest.param(
            {"client": _Client("online", connected=False)},
            True,
            "daemon connection",
            id="disconnected",
        ),
        pytest.param({"mock_serving": False}, True, "the mock model server", id="mock-stopped"),
        pytest.param({}, False, "the private tmux server", id="tmux-gone"),
        pytest.param({"client": _Client()}, True, "the terminal provider", id="no-provider"),
        pytest.param({"client": _Client("offline")}, True, "the terminal provider", id="offline"),
        pytest.param(
            {"client": _Client(unreadable=True)}, True, "provider registry", id="unreadable"
        ),
        pytest.param({}, True, None, id="all-alive"),
    ],
)
async def test_a_component_not_proven_alive_is_named_and_never_blamed_on_the_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setup: dict[str, Any],
    tmux: bool,
    broken: str | None,
) -> None:
    monkeypatch.setattr(live_rig, "_tmux_running", lambda _env: tmux)
    rig, mock = _rig(
        tmp_path,
        setup.get("client", _Client("online")),
        daemon=setup.get("daemon", True),
        mock_serving=setup.get("mock_serving", True),
    )
    try:
        found = await rig.broken_component()
    finally:
        mock.stop()

    assert (found is None) if broken is None else (found is not None and broken in found)


async def test_an_exit_with_a_broken_component_is_inconclusive_not_a_cli_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(live_rig, "_tmux_running", lambda _env: True)
    rig, mock = _rig(tmp_path, _Client("online"), mock_serving=False)

    with pytest.raises(pytest.skip.Exception, match="the mock model server stopped"):
        await rig._classify_exit("p1")
    mock.start()
    try:
        with pytest.raises(pytest.fail.Exception, match="the stock CLI exited before readiness"):
            await rig._classify_exit("p1")
    finally:
        mock.stop()


def _write_logs(theater_home: Path) -> None:
    logs = theater_home / "regie" / "var" / "logs"
    logs.mkdir(parents=True)
    (logs / "bridge.log").write_text("bridge said this", encoding="utf-8")


def test_a_stack_that_never_started_leaves_no_evidence_directory(tmp_path: Path) -> None:
    live_rig._copy_logs(tmp_path / "th", tmp_path / "evidence" / "boot")

    assert not (tmp_path / "evidence").exists()


def test_a_boot_that_is_retried_keeps_the_logs_of_the_attempt_it_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, evidence = tmp_path / "th", tmp_path / "evidence"
    _write_logs(home)
    outcomes = iter(["the Regie bridge did not start: slow", None])
    stopped: list[Path] = []
    monkeypatch.setattr(live_rig, "_boot", lambda _env, _socket: next(outcomes))
    monkeypatch.setattr(live_rig, "_stop", lambda _env, root: stopped.append(root))

    assert live_rig._boot_with_retry({}, home, evidence) is None

    assert (evidence / "boot-1" / "bridge.log").read_text(encoding="utf-8") == "bridge said this"
    assert stopped == [home]


def test_two_missed_boots_report_the_last_reason_and_leave_cleanup_to_the_caller(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, evidence = tmp_path / "th", tmp_path / "evidence"
    _write_logs(home)
    reasons = iter(["first miss", "second miss"])
    stopped: list[Path] = []
    monkeypatch.setattr(live_rig, "_boot", lambda _env, _socket: next(reasons))
    monkeypatch.setattr(live_rig, "_stop", lambda _env, root: stopped.append(root))

    assert live_rig._boot_with_retry({}, home, evidence) == "second miss"

    assert [path.name for path in evidence.iterdir()] == ["boot-1"]
    assert stopped == [home]
