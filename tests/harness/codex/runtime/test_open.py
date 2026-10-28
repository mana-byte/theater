"""Codex native runtime open paths: new, fork and reconnect handshakes bind the exact thread."""

from __future__ import annotations

import pytest

from tests.support.codex_server import (
    ENDPOINT,
    ScriptedCodexIO,
    ScriptedCodexServer,
    make_runtime,
    open_new,
    thread_started,
)
from theater.harness.builtin.plugins.codex import runtime_constants as codex_runtime_constants
from theater.harness.builtin.plugins.codex.runtime_plan import (
    VERSION_POLICY,
    plan_codex_frontend,
)
from theater.harness.contracts.runtime import (
    RuntimeConnectionError,
    RuntimeRequestError,
    SessionOpenMode,
)
from theater.harness.contracts.version_policy import version_key


async def test_open_new_ignores_foreign_cwd_and_ephemeral_threads() -> None:
    server = ScriptedCodexServer()
    server.push(thread_started("other-thread", cwd="/elsewhere"))
    server.push(thread_started("title-thread", ephemeral=True))
    _runtime, binding = await open_new(server)
    assert binding.native_session_id == "ui-thread-1"


async def test_open_new_matches_canonicalized_broadcast_cwd(tmp_path) -> None:
    # Codex reports the canonicalized cwd in thread/started broadcasts: a cwd
    # reached through a symlinked component (``/tmp`` -> ``/private/tmp`` on
    # macOS) must still bind instead of expiring the startup deadline.
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    server = ScriptedCodexServer()
    runtime = make_runtime(server, cwd=str(link))
    server.push(thread_started("ui-thread-1", cwd=str(real)))
    binding = await runtime.open_session(mode=SessionOpenMode.NEW)
    assert binding.native_session_id == "ui-thread-1"
    await runtime.aclose()


async def test_open_new_waits_for_late_broadcast() -> None:
    server = ScriptedCodexServer()
    server.push_later(thread_started("late-thread"), delay=0.05)
    runtime = make_runtime(server)
    binding = await runtime.open_session(mode=SessionOpenMode.NEW)
    assert binding.native_session_id == "late-thread"


async def test_open_new_fails_closed_without_broadcast(monkeypatch) -> None:
    monkeypatch.setattr(codex_runtime_constants, "CODEX_RUNTIME_STARTUP_TIMEOUT_SECONDS", 0.05)
    server = ScriptedCodexServer()
    runtime = make_runtime(server)
    with pytest.raises(RuntimeConnectionError, match="no thread/started broadcast"):
        await runtime.open_session(mode=SessionOpenMode.NEW)
    await runtime.aclose()


async def test_open_new_fails_closed_on_ambiguous_broadcasts() -> None:
    server = ScriptedCodexServer()
    server.push(thread_started("thread-a"))
    server.push(thread_started("thread-b"))
    runtime = make_runtime(server)
    with pytest.raises(RuntimeConnectionError, match="refusing to guess"):
        await runtime.open_session(mode=SessionOpenMode.NEW)
    await runtime.aclose()


async def test_open_new_rejects_fabricated_session_argument() -> None:
    server = ScriptedCodexServer()
    runtime = make_runtime(server)
    with pytest.raises(ValueError, match="native_session_id=None"):
        await runtime.open_session(mode=SessionOpenMode.NEW, native_session_id="th-1")
    await runtime.aclose()


async def test_frontend_plan_establishes_observer_before_ui_launch() -> None:
    server = ScriptedCodexServer()
    runtime = make_runtime(server)
    plan = await runtime.frontend_plan(native_session_id=None)
    assert plan == plan_codex_frontend(ENDPOINT, native_session_id=None, approval="manual")
    assert [name for name, _params in server.requests] == ["initialize"]
    assert server.notifications_sent == [("initialized", {})]
    assert server.connect_count == 1
    # Only now does the daemon launch the UI and its thread get created; the
    # already-initialized observer receives the exact broadcast.
    server.push(thread_started())
    binding = await runtime.open_session(mode=SessionOpenMode.NEW)
    assert binding.native_session_id == "ui-thread-1"
    # Replanning is idempotent: no second connection or handshake.
    await runtime.frontend_plan(native_session_id="ui-thread-1")
    assert server.connect_count == 1
    assert [name for name, _params in server.requests].count("initialize") == 1
    assert server.notifications_sent == [("initialized", {})]
    await runtime.aclose()


async def test_handshake_failure_closes_freshly_opened_connection() -> None:
    server = ScriptedCodexServer()
    server.fail("initialize", RuntimeRequestError(-32000, "backend refused handshake"))
    runtime = make_runtime(server)
    with pytest.raises(RuntimeRequestError):
        await runtime.open_session(mode=SessionOpenMode.NEW)
    # The freshly opened connection is closed, never leaked.
    assert server.close_count == 1
    io = runtime.context.io
    assert isinstance(io, ScriptedCodexIO)
    assert io.connection is not None
    assert io.connection.closed is True
    await runtime.aclose()
    assert server.close_count == 1


async def test_handshake_rejects_unverified_backend_version() -> None:
    server = ScriptedCodexServer()
    # A binary changed between the probe and backend start must not bind an
    # unverified server under the verified policy.
    maximum = version_key(VERSION_POLICY.maximum)
    unsupported = ".".join(map(str, (*maximum[:2], maximum[2] + 1)))
    server.respond("initialize", {"userAgent": f"codex-cli/{unsupported} (macos; arm64)"})
    runtime = make_runtime(server)
    with pytest.raises(RuntimeConnectionError, match="unverified native version"):
        await runtime.open_session(mode=SessionOpenMode.NEW)
    # Fail closed: the connection is closed and the handshake never completes
    # with an unverified server.
    assert server.close_count == 1
    assert server.notifications_sent == []
    await runtime.aclose()
    assert server.close_count == 1


async def test_handshake_rejects_missing_or_malformed_user_agent() -> None:
    server = ScriptedCodexServer()
    server.respond("initialize", {"codexHome": "/isolated/codex-home"})
    runtime = make_runtime(server)
    with pytest.raises(RuntimeConnectionError, match="unverified native version"):
        await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="th-1")
    assert server.close_count == 1
    assert server.notifications_sent == []
    await runtime.aclose()
    assert server.close_count == 1


async def test_open_fork_uses_native_thread_fork_with_exact_parent() -> None:
    server = ScriptedCodexServer()
    server.respond("thread/fork", {"thread": {"id": "fork-9", "status": {"type": "idle"}}})
    server.respond("thread/resume", {"thread": {"id": "fork-9", "status": {"type": "idle"}}})
    runtime = make_runtime(
        server,
        approval="yolo",
        model="gpt-5.2",
        reasoning_effort="high",
    )
    binding = await runtime.open_session(mode=SessionOpenMode.FORK, native_session_id="parent-1")
    assert binding.native_session_id == "fork-9"
    assert server.requested("thread/fork") == [
        {
            "threadId": "parent-1",
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
            "model": "gpt-5.2",
            "config": {"model_reasoning_effort": "high"},
        }
    ]
    await runtime.aclose()


async def test_open_reconnect_fails_closed_on_identity_mismatch() -> None:
    server = ScriptedCodexServer()
    server.respond(
        "thread/resume", {"thread": {"id": "different-thread", "status": {"type": "idle"}}}
    )
    runtime = make_runtime(server)
    with pytest.raises(RuntimeConnectionError, match="refusing to bind"):
        await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="th-1")
    await runtime.aclose()
