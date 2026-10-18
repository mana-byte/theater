"""Helpers shared across a split test module."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from tests.support.paths import TESTS_DIR

FIXTURES = TESTS_DIR / "fixtures" / "codex_native_ui_bootstrap"


MARKER_FILENAME = "approval_marker.txt"


MARKER_CONTENT = "theater-ui-approval-proof"


PROOF_COMMAND = f"/bin/sh -c 'echo {MARKER_CONTENT} > {MARKER_FILENAME}'"


FIRST_TURN_PROMPT = "Run the proof command now."


FINAL_ASSISTANT_MESSAGE = "Theater bootstrap proof turn completed."


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


client = load_module(
    "codex_ui_bootstrap_client", TESTS_DIR / "native" / "codex_ui_bootstrap_client.py"
)


mock_responses = load_module("codex_native_bootstrap_mock", FIXTURES / "mock_responses.py")


codex_env = load_module("codex_native_bootstrap_env", FIXTURES / "codex_env.py")


def discover_ui_thread(received: list[client.Received], *, cwd: str) -> dict:
    """Find the thread/started broadcast for exactly this cwd.

    Identity comes from the notification payload itself: the app-server
    broadcasts `thread/started` with the full Thread object, and we require an
    exact string match on its `cwd`. No creation-time window, no directory
    listing, no guessing.
    """
    matches = [
        received_event.payload["thread"]
        for received_event in received
        if received_event.kind == "notification"
        and received_event.method == "thread/started"
        and received_event.payload.get("thread", {}).get("cwd") == cwd
    ]
    if len(matches) != 1:
        raise AssertionError(
            f"expected exactly one thread/started broadcast for cwd {cwd}, got {len(matches)}"
        )
    return matches[0]


def ui_ready_evidence(started_thread: dict) -> list[str]:
    """State/event predicates that gate the single turn/start, in order.

    The zero-turn `thread/resume` attempt has already failed with the exact
    stock error (captured separately), so readiness here rests on the
    `thread/started` broadcast itself: the thread must be live (idle) in the
    payload the app-server broadcast to every initialized connection.
    """
    status = started_thread.get("status")
    if not (isinstance(status, dict) and status.get("type") in ("idle", "active")):
        raise AssertionError(f"thread is not in a live state: {status!r}")
    return [
        f"thread/started broadcast delivered the exact id {started_thread['id']}",
        f"thread state is live: {status.get('type')}",
    ]
