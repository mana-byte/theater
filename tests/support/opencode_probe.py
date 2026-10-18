"""A patched `opencode --version` / `serve --help` probe for server-plan tests."""

from __future__ import annotations

import threading

import pytest

from theater.harness.builtin.plugins.opencode import server_plan


class ProbeResult:
    def __init__(self, output: str, returncode: int = 0) -> None:
        self.returncode = returncode
        self.stdout = output
        self.stderr = ""


def patch_probe(
    monkeypatch: pytest.MonkeyPatch,
    version: str,
    serve_help: str,
    returncodes: tuple[int, int] = (0, 0),
) -> None:
    started = threading.Barrier(2, timeout=5)

    def run(argv: list[str], **kwargs: object) -> ProbeResult:
        assert kwargs == {
            "capture_output": True,
            "text": True,
            "timeout": server_plan.MODELS_TIMEOUT,
            "check": False,
        }
        started.wait()
        return (
            ProbeResult(version, returncodes[0])
            if argv[-1] == "--version"
            else ProbeResult(serve_help, returncodes[1])
        )

    monkeypatch.setattr(server_plan.subprocess, "run", run)
