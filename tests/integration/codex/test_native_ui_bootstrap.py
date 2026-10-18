"""Opt-in real-codex promptless TUI bootstrap on a shared app-server."""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

from tests.support.codex_bootstrap_proof import (
    FINAL_ASSISTANT_MESSAGE,
    FIRST_TURN_PROMPT,
    MARKER_CONTENT,
    MARKER_FILENAME,
    PROOF_COMMAND,
    client,
    codex_env,
    discover_ui_thread,
    mock_responses,
    ui_ready_evidence,
)
from theater.harness.builtin.plugins.codex.runtime_plan import (
    codex_version_refusal,
    qualified_versions_range_text,
)

pytestmark = pytest.mark.integration


#: The approval overlay title the stock TUI renders for an escalated command
#: (codex-rs `tui/src/bottom_pane/approval_overlay.rs`, Exec branch).
APPROVAL_OVERLAY_TITLE = "Would you like to run the following command?"


#: Default keybinding for ApprovalKeymap::approve in the stock TUI
#: (codex-rs `tui/src/keymap.rs`).
APPROVE_KEY = "y"


#: Exact stock error a zero-turn `thread/resume` answers with, on the
#: historical unmodified 0.154.0 app-server (the rollout is only materialised once the
#: first turn starts).
NO_ROLLOUT_ERROR_PREFIX = "no rollout found for thread id "


SMOKE_ENV_VAR = "THEATER_CODEX_NATIVE_SMOKE"


def _require_allowed_bootstrap_version(version_line: str) -> None:
    """A release inside the qualified range, or the one THEATER_CODEX_QUALIFY_VERSION candidate."""
    version = version_line.removeprefix("codex-cli").strip()
    refusal = codex_version_refusal(version)
    assert refusal is None or version == os.environ.get("THEATER_CODEX_QUALIFY_VERSION"), (
        f"{refusal}; qualified range {qualified_versions_range_text()}"
    )


def _skip_unless_smoke_enabled() -> None:
    if os.environ.get(SMOKE_ENV_VAR) != "1":
        pytest.skip(
            f"set {SMOKE_ENV_VAR}=1 to run the real codex native smoke; "
            "it launches the stock codex app-server and TUI"
        )
    if codex_env.codex_binary() is None:
        pytest.skip("codex is not on PATH")
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not on PATH")


def _tmux(tmux_socket: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["tmux", "-S", tmux_socket, *args], capture_output=True, text=True, check=check
    )


def _capture_pane(tmux_socket: str) -> str:
    result = _tmux(tmux_socket, "capture-pane", "-p", "-t", "cxb:0.0", check=False)
    return result.stdout


def _pane_alive(tmux_socket: str) -> bool:
    result = _tmux(tmux_socket, "list-panes", "-t", "cxb:0.0", "-F", "#{pane_pid}", check=False)
    if result.returncode != 0:
        return False
    pid = result.stdout.strip()
    if not pid.isdigit():
        return False
    probe = subprocess.run(
        ["ps", "-p", pid, "-o", "pid="], capture_output=True, text=True, check=False
    )
    return probe.returncode == 0 and probe.stdout.strip() != ""


def _spawn_backend(
    repo: Path, socket_path: Path, backend_log, backend_env: dict
) -> subprocess.Popen:
    """Launch the isolated stock app-server on a private Unix socket."""
    return subprocess.Popen(
        [
            codex_env.codex_binary() or "codex",
            "app-server",
            "--listen",
            f"unix://{socket_path}",
        ],
        cwd=repo,
        env=backend_env,
        stdout=backend_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )


def _launch_tui(
    tmux_socket: Path, codex_home: Path, repo: Path, socket_path: Path, command: str
) -> None:
    """Launch the real native TUI, promptless, on this test's private tmux."""
    _tmux(
        str(tmux_socket),
        "new-session",
        "-d",
        "-x",
        "220",
        "-y",
        "50",
        "-s",
        "cxb",
        "-e",
        f"CODEX_HOME={codex_home}",
        "-c",
        str(repo),
        command,
    )


async def _wait_for_pane_text(tmux_socket: str, needle: str, *, timeout: float = 30.0) -> None:
    """Poll the pane for rendered text. Used only to drive the UI (find the
    approval overlay, confirm the turn rendered) — never for thread identity,
    which comes from notifications with exact ids."""
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        last = _capture_pane(tmux_socket)
        if needle in last:
            return
        await asyncio.sleep(0.1)
    pytest.fail(f"TUI pane never showed {needle!r}; last capture was:\n{last}")


async def _discover_ui_thread(
    observer: client.AppServerClient, repo: Path, marker_path: Path
) -> tuple[dict, str]:
    """Exact thread discovery from the thread/started broadcast, plus the
    explicit zero-turn resume failure that answers the feasibility question."""
    started = await observer.wait_for(
        "notification",
        method="thread/started",
        predicate=lambda event: event.payload.get("thread", {}).get("cwd") == str(repo),
        timeout=90.0,
    )
    started_thread = started.payload["thread"]
    thread_id = started_thread["id"]
    assert started_thread["cwd"] == str(repo)
    assert discover_ui_thread(observer.received, cwd=str(repo))["id"] == thread_id
    assert not marker_path.exists(), "UI bootstrap must not submit the prompt itself"

    # The stock app-server refuses to resume the zero-turn thread (no rollout
    # exists yet), so a control client cannot pre-subscribe before the first
    # turn. Captured as explicit evidence, not worked around.
    try:
        await observer.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
        raise AssertionError("zero-turn thread/resume unexpectedly succeeded")
    except client.RequestError as err:
        assert str(err) == f"{NO_ROLLOUT_ERROR_PREFIX}{thread_id}", (
            f"unexpected zero-turn resume error: {err}"
        )
    return started_thread, thread_id


async def _submit_exactly_one_turn(
    observer: client.AppServerClient,
    tmux_socket: str,
    thread_id: str,
    marker_path: Path,
) -> str:
    """The single turn/start after readiness; the approval it triggers is
    answered exclusively in the native UI."""
    turn = await observer.request(
        "turn/start",
        {
            "threadId": thread_id,
            "input": [{"type": "text", "text": FIRST_TURN_PROMPT}],
        },
    )
    turn_id = turn["turn"]["id"]
    assert turn["turn"]["status"] == "inProgress"
    assert observer.sent.count_outgoing_requests("turn/start") == 1

    # The model demands an escalated command; the unsubscribed control client
    # sees the thread block on approval via the broadcast thread/status/changed
    # (waitingOnApproval flag), while the approval *request* itself goes only
    # to the subscribed connection: the UI.
    await observer.wait_for(
        "notification",
        method="thread/status/changed",
        predicate=lambda event: (
            event.payload.get("threadId") == thread_id
            and "waitingOnApproval" in (event.payload.get("status", {}).get("activeFlags") or [])
        ),
    )
    assert not any(received.kind == "server_request" for received in observer.received), (
        "turn-scoped requests (approvals) must not reach the unsubscribed control client"
    )
    assert not observer.sent.responses_to_server_requests, (
        "the Theater control client must never answer an approval"
    )

    # Drive the UI like the human it belongs to: wait for the overlay,
    # then press the stock approve key.
    await _wait_for_pane_text(tmux_socket, APPROVAL_OVERLAY_TITLE)
    _tmux(tmux_socket, "send-keys", "-t", "cxb:0.0", APPROVE_KEY)

    # The approval resolved and the command ran: marker written, and the
    # broadcast shows the thread active again with the flag cleared.
    await client.wait_until(marker_path.exists, timeout=30.0)
    assert marker_path.read_text().strip() == MARKER_CONTENT
    await observer.wait_for(
        "notification",
        method="thread/status/changed",
        predicate=lambda event: (
            event.payload.get("threadId") == thread_id
            and event.payload.get("status", {}).get("type") == "active"
            and "waitingOnApproval"
            not in (event.payload.get("status", {}).get("activeFlags") or [])
        ),
    )
    return turn_id


async def _reconnect_midturn(
    observer2: client.AppServerClient, thread_id: str, turn_id: str, tmux_socket: str
) -> None:
    """A fresh control client re-subscribes to the exact same thread while the
    turn is provably still active (the mock is holding its continuation)."""
    resume = await observer2.request("thread/resume", {"threadId": thread_id, "excludeTurns": True})
    assert resume["thread"]["id"] == thread_id, (
        "a new control client must reconnect to the exact same thread"
    )
    assert resume["thread"]["status"]["type"] == "active", (
        "the reconnected client must see the same live turn still running"
    )
    assert _pane_alive(tmux_socket), "UI died when the control connection was dropped"


async def _witness_completion(
    observer2: client.AppServerClient,
    tmux_socket: str,
    thread_id: str,
    turn_id: str,
) -> None:
    """The reconnected, subscribed client watches the exact same turn finish,
    and the thread history names the same turn with the same items."""
    completed = await observer2.wait_for(
        "notification",
        method="turn/completed",
        predicate=lambda event: event.payload.get("threadId") == thread_id,
        timeout=60.0,
    )
    completed_turn = completed.payload["turn"]
    assert completed_turn["id"] == turn_id, (
        "the reconnected client must see the control client's turn finish"
    )
    assert completed_turn["status"] == "completed"
    assert any(
        item.get("type") == "agentMessage" and item.get("text") == FINAL_ASSISTANT_MESSAGE
        for item in completed_turn["items"]
    ), f"turn/completed items lack the final message: {completed_turn['items']}"
    await observer2.wait_for(
        "notification",
        method="thread/status/changed",
        predicate=lambda event: (
            event.payload.get("threadId") == thread_id
            and event.payload.get("status", {}).get("type") == "idle"
        ),
        timeout=30.0,
    )

    # UI and control client provably shared the same turn: the UI rendered the
    # exact assistant message that ended the Theater-submitted turn, and the
    # post-turn history names the exact same turn id with the same items.
    await _wait_for_pane_text(tmux_socket, FINAL_ASSISTANT_MESSAGE)
    resume = await observer2.request(
        "thread/resume", {"threadId": thread_id, "excludeTurns": False}
    )
    matching = [
        turn_entry for turn_entry in resume["thread"]["turns"] if turn_entry["id"] == turn_id
    ]
    assert len(matching) == 1, f"thread history must contain turn {turn_id} exactly once"
    history_items = matching[0]["items"]
    assert any(item.get("type") == "agentMessage" for item in history_items)
    exec_items = [item for item in history_items if item.get("id") == "call-proof-1"]
    assert len(exec_items) == 1 and exec_items[0]["exitCode"] == 0


async def _teardown(
    observers: list[client.AppServerClient],
    mock: mock_responses.MockResponsesServer,
    backend: subprocess.Popen | None,
    backend_log,
    tmux_socket: str,
    root: Path,
) -> None:
    """Deterministic cleanup: nothing survives the test, nothing is shared."""
    for observer_client in observers:
        await observer_client.aclose()
    mock.release_held_response()
    mock.stop()
    _tmux(tmux_socket, "kill-server", check=False)
    if backend is not None and backend.poll() is None:
        os.killpg(os.getpgid(backend.pid), signal.SIGTERM)
        try:
            backend.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(os.getpgid(backend.pid), signal.SIGKILL)
            backend.wait(timeout=5)
    backend_log.close()
    shutil.rmtree(root, ignore_errors=True)


def _make_mock(marker_path: Path) -> mock_responses.MockResponsesServer:
    """Scripted provider: escalated exec (approval proof) then the final
    assistant message, with TUI-internal structured requests routed to
    filler and the second turn stream gated for the mid-turn abrupt close."""
    mock = mock_responses.MockResponsesServer(
        [
            mock_responses.escalated_exec_call_response(
                PROOF_COMMAND, "call-proof-1", str(marker_path)
            ),
            mock_responses.final_assistant_message_response(FINAL_ASSISTANT_MESSAGE),
        ],
        marker=FIRST_TURN_PROMPT,
    )
    mock.hold_second_response()
    return mock


def _assert_sentinels(
    observer: client.AppServerClient,
    observer2: client.AppServerClient,
    mock: mock_responses.MockResponsesServer,
) -> None:
    """Exactly one first-turn prompt, only after readiness; no approval was
    ever answered from a control connection."""
    assert observer.sent.count_outgoing_requests("turn/start") == 1
    assert observer2.sent.count_outgoing_requests("turn/start") == 0
    assert not observer.sent.responses_to_server_requests
    assert not observer2.sent.responses_to_server_requests
    # The mock's dispatch record proves the turn's two scripted model calls
    # were the only ones that matched the script.
    matched_requests = [request for request in mock.requests if request.matched]
    assert len(matched_requests) == 2, (
        f"expected exactly two turn model calls, got {len(matched_requests)}"
    )


@pytest.mark.tmux
async def test_codex_native_ui_bootstrap_proof() -> None:
    """One full chain, one live backend, one UI, one prompt — asserted at
    every seam against the unmodified installed codex release."""
    _skip_unless_smoke_enabled()
    _require_allowed_bootstrap_version(codex_env.codex_version())

    # resolve() up front: the app-server canonicalises paths, and on macOS
    # /tmp is a symlink to /private/tmp — the exact-cwd predicate below must
    # compare canonical with canonical, not symlinked with canonical.
    root = Path(tempfile.mkdtemp(prefix="cxb-", dir="/tmp")).resolve()
    codex_home = root / "codex-home"
    repo = codex_env.make_git_repo(root / "repo")
    socket_path = root / "app.sock"
    tmux_socket = root / "tmux-socket"
    marker_path = repo / MARKER_FILENAME

    backend_log = (root / "backend.log").open("w")
    backend_env = dict(os.environ)
    backend_env.update(CODEX_HOME=str(codex_home), TERM="xterm-256color")
    backend_env.pop("TMUX", None)
    backend_env.pop("TMUX_PANE", None)

    backend: subprocess.Popen | None = None
    mock = _make_mock(marker_path)
    observers: list[client.AppServerClient] = []

    try:
        mock.start()
        codex_env.write_mock_config(codex_home, mock.base_url)

        # 1. isolated backend on a private unix socket
        backend = _spawn_backend(repo, socket_path, backend_log, backend_env)
        await client.wait_until(socket_path.exists, timeout=30.0)
        await client.wait_until(lambda: client.socket_connectable(str(socket_path)), timeout=30.0)

        # 2. Theater-like observer completes initialize/initialized first, so
        #    every later broadcast (thread/started, thread/status/changed)
        #    must be delivered to it.
        observer = client.AppServerClient(str(socket_path), name="theater-observer")
        observers.append(observer)
        await observer.connect()
        await observer.initialize()

        # 3. the real native TUI, promptless, no resume id, on a private tmux
        #    server that belongs to this test alone.
        frontend_command = f"{codex_env.codex_binary() or 'codex'} --remote unix://{socket_path}"
        assert "resume" not in frontend_command and FIRST_TURN_PROMPT not in frontend_command
        _launch_tui(tmux_socket, codex_home, repo, socket_path, frontend_command)

        # 4. exact thread discovery from native evidence; the zero-turn
        #    thread/resume failure is captured as explicit evidence.
        started_thread, thread_id = await _discover_ui_thread(observer, repo, marker_path)

        # 5. event/state readiness — the broadcast itself plus a live UI
        #    process; no sleep anywhere in this chain
        for line in ui_ready_evidence(started_thread):
            assert line
        assert _pane_alive(str(tmux_socket)), "TUI pane exited before readiness"
        await _wait_for_pane_text(str(tmux_socket), "OpenAI Codex")

        # 6. exactly one turn/start from the control client, after readiness;
        #    the approval it triggers is answered exclusively in the UI.
        turn_id = await _submit_exactly_one_turn(observer, str(tmux_socket), thread_id, marker_path)

        # 7. abrupt close of the control connection mid-turn (the held model
        #    continuation proves the turn is still active), then a fresh
        #    control client re-subscribes to the exact same thread.
        await observer.close_abrupt()
        observer2 = client.AppServerClient(str(socket_path), name="theater-observer-2")
        observers.append(observer2)
        await observer2.connect()
        await observer2.initialize()
        await _reconnect_midturn(observer2, thread_id, turn_id, str(tmux_socket))

        mock.release_held_response()
        await _witness_completion(observer2, str(tmux_socket), thread_id, turn_id)

        _assert_sentinels(observer, observer2, mock)
    finally:
        await _teardown(observers, mock, backend, backend_log, str(tmux_socket), root)


_UPDATE_AVAILABLE = '{"latest_version": "99.0.0", "last_checked_at": "2026-10-05T00:00:00Z"}'


_PRODUCTION_STARTUP_DEADLINE = 30.0


async def _ui_thread_within_deadline(observer, cwd: Path) -> bool:
    try:
        await observer.wait_for(
            "notification",
            method="thread/started",
            predicate=lambda event: event.payload.get("thread", {}).get("cwd") == str(cwd),
            timeout=_PRODUCTION_STARTUP_DEADLINE,
        )
    except TimeoutError:
        return False
    return True


@pytest.mark.tmux
@pytest.mark.parametrize(
    ("scenario", "keep_update_override"),
    [
        ("update-available", True),
        ("update-available-control", False),
        ("worktree-of-trusted-repo", True),
    ],
)
async def test_codex_native_ui_starts_a_thread_unattended(
    scenario: str, keep_update_override: bool
) -> None:
    """Theater's own UI plan must reach `thread/started` with nobody to answer a
    startup dialog: an available update, a worktree of a trusted repo. The control
    drops the update override and must block, proving the scenario can bite."""
    import shlex

    from theater.harness.builtin.plugins.codex.runtime_plan import plan_codex_frontend

    _skip_unless_smoke_enabled()
    root = Path(tempfile.mkdtemp(prefix="cxu-", dir="/tmp")).resolve()
    codex_home = root / "codex-home"
    repo = codex_env.make_git_repo(root / "repo")
    cwd = repo
    if scenario == "worktree-of-trusted-repo":
        # Theater's own layout: the worktree lives inside the repo it was cut from.
        cwd = repo / ".theater" / "worktrees" / "child"
        await asyncio.to_thread(
            subprocess.run, ["git", "worktree", "add", "-q", str(cwd)], cwd=repo, check=True
        )
    socket_path = root / "app.sock"
    tmux_socket = root / "tmux-socket"
    backend_log = (root / "backend.log").open("w")
    backend_env = dict(os.environ, CODEX_HOME=str(codex_home), TERM="xterm-256color")
    backend_env.pop("TMUX", None)
    backend_env.pop("TMUX_PANE", None)
    backend: subprocess.Popen | None = None
    mock = _make_mock(repo / MARKER_FILENAME)
    observers: list = []
    try:
        mock.start()
        config = codex_env.write_mock_config(codex_home, mock.base_url)
        # Only the repo root is trusted: a worktree must rely on Codex's own inheritance.
        config.write_text(
            config.read_text(encoding="utf-8")
            + f'\n[projects."{repo}"]\ntrust_level = "trusted"\n',
            encoding="utf-8",
        )
        (codex_home / "version.json").write_text(_UPDATE_AVAILABLE, encoding="utf-8")
        backend = _spawn_backend(cwd, socket_path, backend_log, backend_env)
        await client.wait_until(socket_path.exists, timeout=30.0)
        await client.wait_until(lambda: client.socket_connectable(str(socket_path)), timeout=30.0)
        observer = client.AppServerClient(str(socket_path), name="theater-observer")
        observers.append(observer)
        await observer.connect()
        await observer.initialize()

        argv = plan_codex_frontend(str(socket_path), native_session_id=None, approval="yolo").argv
        if not keep_update_override:
            at = argv.index("check_for_update_on_startup=false")
            del argv[at - 1 : at + 1]  # the `-c` flag and its value
        command = shlex.join([codex_env.codex_binary() or "codex", *argv[1:]])
        _launch_tui(tmux_socket, codex_home, cwd, socket_path, command)

        started = await _ui_thread_within_deadline(observer, cwd)
        screen = _capture_pane(str(tmux_socket))
        if keep_update_override:
            assert started, f"the UI never created a thread unattended; screen:\n{screen}"
        elif started:
            pytest.skip(
                "this codex install cannot show the update dialog, so it cannot be provoked"
            )
        else:
            assert "update" in screen.lower(), f"blocked, but not on the update dialog:\n{screen}"
    finally:
        await _teardown(observers, mock, backend, backend_log, str(tmux_socket), root)
