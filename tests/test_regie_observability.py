from __future__ import annotations

import os

from textual.app import App

from theater import paths
from theater.regie import app as app_mod


def test_regie_log_path_uses_tmux_pane(theater_home, monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%17")
    assert paths.regie_log_path() == theater_home / "logs" / "regie" / "pane-17.log"


def test_regie_log_path_falls_back_to_pid(theater_home, monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "not-a-pane")
    monkeypatch.setattr(os, "getpid", lambda: 42)
    assert paths.regie_log_path() == theater_home / "logs" / "regie" / "pid-42.log"


def test_ensure_home_creates_log_directories(theater_home):
    assert paths.logs_dir().is_dir()
    assert paths.regie_logs_dir().is_dir()


def test_textual_internal_exception_is_logged_before_default_handling(monkeypatch):
    calls: list[tuple[str, object]] = []
    error = RuntimeError("render failed")
    monkeypatch.setattr(
        app_mod,
        "log_exception",
        lambda _logger, message, caught: calls.append((message, caught)),
    )
    monkeypatch.setattr(
        App,
        "_handle_exception",
        lambda _self, caught: calls.append(("base", caught)),
    )

    app_mod.RegieApp()._handle_exception(error)

    assert calls == [("régie crashed", error), ("base", error)]
