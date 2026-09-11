"""Wave 2B tests: the Codex native runtime plugin half.

These are Codex-focused tests against a scripted app-server double through
the frozen injected ``RuntimeIO``/``RuntimeConnection`` seams — no real
sockets, no process management (Wave 2A owns the transport; Wave 0 owns the
real-binary proof). Sanitized facts come from the Wave 0 fixtures under
``tests/fixtures/codex_native_runtime``.

Covered:
* compatibility probe against the frozen policy/version fixtures,
* exact backend/frontend commands and backend-scoped configuration,
* UI-first NEW discovery of the exact UI-created thread on the private
  backend/generation,
* fork/reconnect with exact native ids and identity-mismatch fail-closed,
* approval server requests observed, never answered,
* busy returned-turn behavior on send, stale steer refusal, exact interrupt,
* experimental settings gating, idle-only dispatch, readback confirmation,
* native queue absence,
* live Source terminal/item dedupe, preview bounds, status snapshots,
  reconnect/missed-completion recovery, cumulative-usage exclusion,
* correction-round guarantees: frontend_plan establishes the observer
  before the UI launches, item started→completed emits exactly once,
  foreign/missing threadId payloads never leak, terminal evidence is
  loss-free under backpressure (including cancelled saturated inserts),
  stored results match their completeness at the contract bound, native
  error text is bounded before the contract object, handshake failure
  closes the fresh connection and enforces the verified version, fork
  binds identity before subscribing, one live Source per runtime, and
  unconfirmed settings readback returns an explicit UNKNOWN receipt,
* summary-view promotion: a completed turn's itemsView=summary agentMessage
  is the exact final agent message (upstream one-item
  TurnCompletionMetadata.last_agent_message construction) and is recorded
  COMPLETE/NATIVE_EVIDENCE from the live turn/completed notification;
  missing/notLoaded/malformed/unknown views and failed/interrupted
  summaries are never promoted, oversized results stay bounded and PARTIAL,
  and reconnect reconciliation keeps every snapshot-derived result PARTIAL
  (NativeTurnOutcome contract) with exact-once dedupe,
* disconnect-only aclose,
* legacy Codex launch behavior unchanged.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from theater.harness.builtin.plugins.codex import runtime as codex_runtime_module
from theater.harness.builtin.plugins.codex.launch import plan_launch
from theater.harness.builtin.plugins.codex.manifest import MANIFEST, manifest_for_root
from theater.harness.builtin.plugins.codex.observer import CodexObserver
from theater.harness.builtin.plugins.codex.runtime import CodexRuntime, codex_runtime_factory
from theater.harness.builtin.plugins.codex.runtime_plan import (
    CODEX_RUNTIME_COMPATIBILITY_POLICY,
    CODEX_RUNTIME_VERIFIED_VERSIONS,
    codex_backend_config_overrides,
    codex_endpoint_url,
    parse_codex_version,
    plan_codex_frontend,
    plan_codex_runtime_backend,
    probe_codex_compatibility,
)
from theater.harness.channels.hybrid import HybridSource
from theater.harness.contracts.callbacks import LaunchContext
from theater.harness.contracts.channels import ChannelDeclaration, ChannelHealthState, ChannelKind
from theater.harness.contracts.launch import LaunchPlan
from theater.harness.contracts.manifest import HarnessManifest
from theater.harness.contracts.runtime import (
    HARNESS_RUNTIME_ERROR_MAX_CHARS,
    HARNESS_RUNTIME_RESULT_MAX_CHARS,
    CapabilityUnavailableReason,
    ConnectionHealth,
    DeliveryResult,
    LiveChannelDeclaration,
    NativeTurnTerminal,
    ResultCompleteness,
    ResultProvenance,
    RuntimeCompatibility,
    RuntimeConnection,
    RuntimeConnectionError,
    RuntimeContext,
    RuntimeExecutionState,
    RuntimeIO,
    RuntimeManifest,
    RuntimeNotification,
    RuntimePlan,
    RuntimePlanningContext,
    RuntimeProbeContext,
    RuntimeRequestError,
    RuntimeRequestTimeout,
    SessionOpenMode,
)
from theater.harness.contracts.source import Batch, Source
from theater.harness.manifests.validation import validate_manifest
from theater.models import Status
from theater.trajectory.enums import TrajectoryKind, TrajectoryStatus

FIXTURES = Path(__file__).parent / "fixtures" / "codex_native_runtime"

ENDPOINT = "/private/theater/var/runtime/codex-p1.sock"
PARTICIPANT = "codex-p1"
CWD = "/repo"
GENERATION = 7
QUEUE_METHODS = (
    "thread/queue/add",
    "thread/queue/list",
    "thread/queue/update",
    "thread/queue/delete",
    "thread/queue/reorder",
    "thread/queue/start",
)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


# ---------------------------------------------------------------------------
# Scripted app-server double (test-only; never imported by production code)
# ---------------------------------------------------------------------------


class RequestFailure(Exception):
    """Scripted failure for one request method."""


@dataclass
class ScriptedCodexServer:
    """Everything the scripted backend records and serves."""

    requests: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    notifications_sent: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    pending: list[RuntimeNotification] = field(default_factory=list)
    responses: dict[str, object] = field(default_factory=dict)
    failures: dict[str, Exception] = field(default_factory=dict)
    connect_count: int = 0
    close_count: int = 0
    #: The backend outlives every Theater connection by construction.
    backend_alive: bool = True

    def push(self, notification: RuntimeNotification) -> None:
        self.pending.append(notification)

    def push_later(self, notification: RuntimeNotification, delay: float) -> None:
        async def deliver() -> None:
            await asyncio.sleep(delay)
            self.push(notification)

        asyncio.get_running_loop().create_task(deliver())

    def respond(self, method: str, result: object) -> None:
        self.responses[method] = result

    def fail(self, method: str, error: Exception) -> None:
        self.failures[method] = error

    def requested(self, method: str) -> list[dict[str, object]]:
        return [params for name, params in self.requests if name == method]

    def default_response(self, method: str) -> object:
        if method == "initialize":
            return {
                "userAgent": "theater/0.154.0 (macos; arm64) xterm (theater; 1.0)",
                "codexHome": "/isolated/codex-home",
                "platformFamily": "unix",
                "platformOs": "macos",
            }
        if method == "thread/resume":
            return {"thread": {"id": "ui-thread-1", "status": {"type": "idle"}, "turns": []}}
        if method == "thread/settings/update":
            return {}
        if method == "thread/read":
            return {"thread": {"id": "ui-thread-1"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1", "status": "inProgress", "items": []}}
        if method == "turn/steer":
            return {"turnId": "turn-1"}
        if method == "turn/interrupt":
            return {}
        raise RequestFailure(f"unexpected request {method}")


class ScriptedCodexConnection(RuntimeConnection):
    def __init__(self, server: ScriptedCodexServer) -> None:
        self._server = server
        self.closed = False

    async def request(
        self,
        method: str,
        params: Mapping[str, object],
        *,
        timeout: float,
    ) -> Mapping[str, object]:
        del timeout
        if self.closed:
            raise RuntimeConnectionError("scripted connection closed")
        assert not method.startswith("thread/queue"), "native queue must never be used"
        self._server.requests.append((method, dict(params)))
        failure = self._server.failures.get(method)
        if failure is not None:
            raise failure
        result = self._server.responses.get(method)
        if result is None:
            result = self._server.default_response(method)
        if isinstance(result, Mapping):
            return dict(result)
        raise RequestFailure(f"scripted response for {method} is not a mapping")

    async def notify(self, method: str, params: Mapping[str, object]) -> None:
        if self.closed:
            raise RuntimeConnectionError("scripted connection closed")
        self._server.notifications_sent.append((method, dict(params)))

    async def notifications(self) -> AsyncIterator[RuntimeNotification]:
        while not self.closed:
            if self._server.pending:
                yield self._server.pending.pop(0)
                continue
            await asyncio.sleep(0.005)

    async def aclose(self) -> None:
        self.closed = True
        self._server.close_count += 1


class ScriptedCodexIO(RuntimeIO):
    def __init__(self, server: ScriptedCodexServer) -> None:
        self._server = server
        self.connection: ScriptedCodexConnection | None = None

    async def connect(self, endpoint: str, *, timeout: float) -> RuntimeConnection:
        del timeout
        assert endpoint == ENDPOINT
        self._server.connect_count += 1
        self.connection = ScriptedCodexConnection(self._server)
        return self.connection


def make_runtime(
    server: ScriptedCodexServer,
    *,
    native_session_id: str | None = None,
    cwd: str | None = CWD,
    io: RuntimeIO | None = None,
) -> CodexRuntime:
    context = RuntimeContext(
        participant_id=PARTICIPANT,
        cwd=cwd,
        io=io if io is not None else ScriptedCodexIO(server),
        backend_generation=GENERATION,
        endpoint=ENDPOINT,
        approval="manual",
        native_session_id=native_session_id,
    )
    return CodexRuntime(context)


def thread_started(
    thread_id: str = "ui-thread-1",
    *,
    cwd: str | None = CWD,
    ephemeral: bool = False,
    status: str = "idle",
) -> RuntimeNotification:
    return RuntimeNotification(
        method="thread/started",
        params={
            "thread": {
                "id": thread_id,
                "cwd": cwd,
                "ephemeral": ephemeral,
                "cliVersion": "0.154.0",
                "status": {"type": status},
            }
        },
    )


async def open_new(server: ScriptedCodexServer, **kwargs) -> tuple[CodexRuntime, object]:
    runtime = make_runtime(server, **kwargs)
    server.push(thread_started())
    binding = await runtime.open_session(mode=SessionOpenMode.NEW)
    return runtime, binding


# ---------------------------------------------------------------------------
# Compatibility probe (policy/version fixtures)
# ---------------------------------------------------------------------------


class _FakeCompleted:
    def __init__(self, output: str, returncode: int = 0) -> None:
        self.stdout = output
        self.stderr = ""
        self.returncode = returncode


def _patch_version(monkeypatch, output: str | None, *, error: Exception | None = None) -> None:
    def fake_run(argv, **kwargs):
        assert argv[1:2] == ["--version"]
        if error is not None:
            raise error
        return _FakeCompleted(output or "")

    monkeypatch.setattr(
        "theater.harness.builtin.plugins.codex.runtime_plan.subprocess.run", fake_run
    )


def test_installed_release_fixture_version_is_verified_policy() -> None:
    release = load_fixture("installed_release.json")
    version = parse_codex_version(release["installed_version_output"])
    assert version == "0.154.0"
    assert version in CODEX_RUNTIME_VERIFIED_VERSIONS


def test_probe_supports_verified_release(monkeypatch) -> None:
    _patch_version(monkeypatch, "codex-cli 0.154.0\n")
    compatibility = probe_codex_compatibility(RuntimeProbeContext(binary="codex"))
    assert compatibility == RuntimeCompatibility(
        supported=True,
        policy=CODEX_RUNTIME_COMPATIBILITY_POLICY,
        native_version="0.154.0",
        reason=None,
    )


def test_probe_rejects_unknown_version_with_legacy_guidance(monkeypatch) -> None:
    _patch_version(monkeypatch, "codex-cli 0.99.0\n")
    compatibility = probe_codex_compatibility(RuntimeProbeContext(binary="codex"))
    assert compatibility.supported is False
    assert compatibility.native_version == "0.99.0"
    assert compatibility.policy == CODEX_RUNTIME_COMPATIBILITY_POLICY
    assert "wiring=auto selects legacy" in compatibility.reason
    assert "explicit native fails" in compatibility.reason


def test_probe_rejects_unparseable_output(monkeypatch) -> None:
    _patch_version(monkeypatch, "some other tool 1.2.3\n")
    compatibility = probe_codex_compatibility(RuntimeProbeContext())
    assert compatibility.supported is False
    assert compatibility.native_version is None


def test_probe_rejects_missing_binary(monkeypatch) -> None:
    _patch_version(monkeypatch, None, error=FileNotFoundError("codex"))
    compatibility = probe_codex_compatibility(RuntimeProbeContext(binary="/no/such/codex"))
    assert compatibility.supported is False
    assert "could not run" in compatibility.reason


def test_probe_rejects_failed_version_command(monkeypatch) -> None:
    _patch_version(monkeypatch, "boom", error=None)
    compatibility = probe_codex_compatibility(
        RuntimeProbeContext(binary="/no/such/codex"),
    )
    assert compatibility.supported is False


# ---------------------------------------------------------------------------
# Pure plans: exact commands and configuration
# ---------------------------------------------------------------------------


def planning_context(**overrides) -> RuntimePlanningContext:
    values: dict[str, object] = {
        "participant_id": PARTICIPANT,
        "cwd": CWD,
        "endpoint": ENDPOINT,
        "config_path": None,
        "approval": "manual",
        "model": None,
        "reasoning_effort": None,
    }
    values.update(overrides)
    return RuntimePlanningContext(**values)  # type: ignore[arg-type]


def test_backend_plan_is_exact_app_server_listen_with_scoped_config() -> None:
    plan = plan_codex_runtime_backend(
        planning_context(approval="yolo", model="gpt-5.2", reasoning_effort="high")
    )
    assert isinstance(plan, RuntimePlan)
    assert plan.backend.argv == [
        "codex",
        "-c",
        "approval_policy=never",
        "-c",
        "sandbox_mode=danger-full-access",
        "-c",
        "model=gpt-5.2",
        "-c",
        "model_reasoning_effort=high",
        "app-server",
        "--listen",
        f"unix://{ENDPOINT}",
    ]
    assert plan.endpoint == ENDPOINT


def test_backend_plan_approval_modes_match_legacy_mapping() -> None:
    edits = codex_backend_config_overrides(planning_context(approval="edits"))
    manual = codex_backend_config_overrides(planning_context(approval="manual"))
    yolo = codex_backend_config_overrides(planning_context(approval="yolo"))
    assert edits == (("approval_policy", "on-request"), ("sandbox_mode", "workspace-write"))
    assert manual == (("approval_policy", "on-request"), ("sandbox_mode", "read-only"))
    assert yolo == (("approval_policy", "never"), ("sandbox_mode", "danger-full-access"))


def test_backend_plan_rejects_missing_or_unknown_approval() -> None:
    # Approval is explicit per spawn — it has no default anywhere: a missing
    # or unknown mode is rejected, never silently mapped to manual.
    with pytest.raises(ValueError, match="approval has no default"):
        codex_backend_config_overrides(planning_context(approval=None))
    with pytest.raises(ValueError, match="approval has no default"):
        codex_backend_config_overrides(planning_context(approval="nonsense"))
    with pytest.raises(ValueError, match="approval has no default"):
        plan_codex_runtime_backend(planning_context(approval=None))


def test_backend_plan_carries_no_credentials_or_files() -> None:
    plan = plan_codex_runtime_backend(planning_context())
    assert plan.backend.env == {}
    assert plan.backend.files == {}
    assert plan.backend.private_files == {}


def test_endpoint_url_helper() -> None:
    assert codex_endpoint_url("/tmp/x.sock") == "unix:///tmp/x.sock"
    assert codex_endpoint_url("unix:///tmp/x.sock") == "unix:///tmp/x.sock"


def test_frontend_plan_promptless_for_fresh_ui() -> None:
    plan = plan_codex_frontend(ENDPOINT, native_session_id=None)
    assert plan.argv == ["codex", "--remote", f"unix://{ENDPOINT}"]
    assert "resume" not in plan.argv


def test_frontend_plan_resumes_exact_thread() -> None:
    plan = plan_codex_frontend(ENDPOINT, native_session_id="th-42")
    assert plan.argv == ["codex", "--remote", f"unix://{ENDPOINT}", "resume", "th-42"]


async def test_runtime_frontend_plan_matches_pure_planner() -> None:
    runtime = make_runtime(ScriptedCodexServer())
    fresh = await runtime.frontend_plan(native_session_id=None)
    resumed = await runtime.frontend_plan(native_session_id="th-42")
    assert fresh == plan_codex_frontend(ENDPOINT, native_session_id=None)
    assert resumed == plan_codex_frontend(ENDPOINT, native_session_id="th-42")
    for plan in (fresh, resumed):
        assert isinstance(plan, LaunchPlan)
        assert not any("prompt" in arg for arg in plan.argv)


# ---------------------------------------------------------------------------
# Manifest wiring
# ---------------------------------------------------------------------------


def test_manifest_declares_valid_runtime_manifest() -> None:
    runtime = MANIFEST.runtime
    assert isinstance(runtime, RuntimeManifest)
    assert callable(runtime.probe)
    assert callable(runtime.plan)
    assert callable(runtime.factory)
    assert isinstance(runtime.channel, LiveChannelDeclaration)
    assert runtime.channel.channel.kind is ChannelKind.LIVE
    assert runtime.channel.drives_job_completion is True
    assert runtime.channel.durable_fallback is True
    observation_ids = {channel.id for channel in MANIFEST.observation.channels}
    assert runtime.channel.channel.id not in observation_ids
    validate_manifest("codex", MANIFEST)


def test_manifest_for_root_preserves_runtime() -> None:
    manifest = manifest_for_root(Path("/tmp/root"))
    assert isinstance(manifest, HarnessManifest)
    assert manifest.runtime is MANIFEST.runtime


def test_legacy_launch_behavior_unchanged() -> None:
    context = LaunchContext(
        participant_id=PARTICIPANT,
        prompt="hello",
        config_path=Path("/tmp/config.json"),
        approval="yolo",
    )
    plan = plan_launch(context)
    assert plan.argv[0] == "codex"
    assert "--dangerously-bypass-approvals-and-sandbox" in plan.argv
    assert plan.argv[-1] == "hello"


# ---------------------------------------------------------------------------
# UI-first NEW discovery
# ---------------------------------------------------------------------------


async def test_open_new_discovers_exact_ui_thread_and_handshake_dialect() -> None:
    server = ScriptedCodexServer()
    _runtime, binding = await open_new(server)
    assert binding.participant_id == PARTICIPANT
    assert binding.backend_generation == GENERATION
    assert binding.native_session_id == "ui-thread-1"
    assert binding.lifecycle.value == "bound"
    assert binding.protocol == "codex-app-server"
    assert binding.compatibility_policy == CODEX_RUNTIME_COMPATIBILITY_POLICY
    assert binding.native_version == "0.154.0"
    # Handshake dialect: initialize then exactly one initialized notification.
    initialize = server.requested("initialize")
    assert len(initialize) == 1
    assert initialize[0]["clientInfo"]["name"] == "theater"
    assert initialize[0]["capabilities"] == {"experimentalApi": True}
    assert server.notifications_sent == [("initialized", {})]
    # The runtime never submits the initial prompt and persists nothing.
    assert server.requested("turn/start") == []
    assert server.requested("thread/queue/add") == []


async def test_open_new_ignores_foreign_cwd_and_ephemeral_threads() -> None:
    server = ScriptedCodexServer()
    server.push(thread_started("other-thread", cwd="/elsewhere"))
    server.push(thread_started("title-thread", ephemeral=True))
    _runtime, binding = await open_new(server)
    assert binding.native_session_id == "ui-thread-1"


async def test_open_new_waits_for_late_broadcast() -> None:
    server = ScriptedCodexServer()
    server.push_later(thread_started("late-thread"), delay=0.05)
    runtime = make_runtime(server)
    binding = await runtime.open_session(mode=SessionOpenMode.NEW)
    assert binding.native_session_id == "late-thread"


async def test_open_new_fails_closed_without_broadcast(monkeypatch) -> None:
    monkeypatch.setattr(codex_runtime_module, "CODEX_RUNTIME_STARTUP_TIMEOUT_SECONDS", 0.05)
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
    # The lifecycle awaits the fresh UI plan before launching the UI: by the
    # time the plan is returned, this runtime's observer connection has
    # completed initialize + initialized, so the UI's eager thread/start
    # broadcast can never race past the observer.
    plan = await runtime.frontend_plan(native_session_id=None)
    assert plan.argv == ["codex", "--remote", f"unix://{ENDPOINT}"]
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
    server.respond("initialize", {"userAgent": "codex-cli/0.99.0 (macos; arm64)"})
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


# ---------------------------------------------------------------------------
# Fork / reconnect
# ---------------------------------------------------------------------------


async def test_open_fork_uses_native_thread_fork_with_exact_parent() -> None:
    server = ScriptedCodexServer()
    server.respond("thread/fork", {"thread": {"id": "fork-9", "status": {"type": "idle"}}})
    server.respond("thread/resume", {"thread": {"id": "fork-9", "status": {"type": "idle"}}})
    runtime = make_runtime(server)
    binding = await runtime.open_session(mode=SessionOpenMode.FORK, native_session_id="parent-1")
    assert binding.native_session_id == "fork-9"
    assert server.requested("thread/fork") == [{"threadId": "parent-1"}]
    await runtime.aclose()


async def test_open_fork_binds_identity_before_subscribe() -> None:
    server = ScriptedCodexServer()
    server.respond("thread/fork", {"thread": {"id": "fork-9", "status": {"type": "idle"}}})
    server.respond(
        "thread/resume",
        {"thread": {"id": "fork-9", "status": {"type": "idle"}, "turns": []}},
    )
    runtime = make_runtime(server)
    binding = await runtime.open_session(mode=SessionOpenMode.FORK, native_session_id="parent-1")
    assert binding.native_session_id == "fork-9"
    # The explicit subscribe targets the exact forked thread — proof the
    # forked identity was bound before the subscribe attempt — so live
    # notifications for the fork flow from open onward.
    assert server.requested("thread/resume") == [{"threadId": "fork-9", "excludeTurns": False}]
    await runtime.aclose()


async def test_open_reconnect_attaches_exact_thread_and_subscribes() -> None:
    server = ScriptedCodexServer()
    server.respond(
        "thread/resume",
        {
            "thread": {
                "id": "th-1",
                "status": {"type": "idle"},
                "turns": [],
            }
        },
    )
    runtime = make_runtime(server)
    binding = await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="th-1")
    assert binding.native_session_id == "th-1"
    assert server.requested("thread/resume") == [{"threadId": "th-1"}]
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


# ---------------------------------------------------------------------------
# Controls: send / steer / interrupt / settings
# ---------------------------------------------------------------------------


async def test_send_starts_turn_and_subscribes_after_rollout() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    receipt = await runtime.send(operation_id="op-1", prompt="do the thing")
    assert receipt.operation_id == "op-1"
    assert receipt.result is DeliveryResult.ACCEPTED
    assert receipt.native_turn_id == "turn-1"
    started = server.requested("turn/start")
    assert started == [
        {
            "threadId": "ui-thread-1",
            "input": [{"type": "text", "text": "do the thing"}],
            "clientUserMessageId": "op-1",
        }
    ]
    # The rollout materializes with the accepted turn: subscription follows.
    assert server.requested("thread/resume") == [{"threadId": "ui-thread-1", "excludeTurns": False}]
    snapshot = await runtime.snapshot()
    assert snapshot.native_turn_id == "turn-1"
    await runtime.aclose()


async def test_send_reports_actual_returned_turn_on_busy_race() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    # A simultaneous native-UI submission absorbed the message: turn/start
    # returns the SAME active turn instead of creating a new one.
    server.respond("turn/start", {"turn": {"id": "ui-turn-active", "status": "inProgress"}})
    server.respond(
        "thread/resume",
        {
            "thread": {
                "id": "ui-thread-1",
                "status": {"type": "active"},
                "turns": [{"id": "ui-turn-active", "status": "inProgress", "items": []}],
            }
        },
    )
    receipt = await runtime.send(operation_id="op-2", prompt="overlapping")
    assert receipt.result is DeliveryResult.ACCEPTED
    assert receipt.native_turn_id == "ui-turn-active"
    snapshot = await runtime.snapshot()
    assert snapshot.native_turn_id == "ui-turn-active"
    await runtime.aclose()


async def test_send_never_fabricates_turn_identity_from_malformed_result() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    server.respond("turn/start", {"turn": {"items": []}})
    receipt = await runtime.send(operation_id="op-3", prompt="anything")
    assert receipt.result is DeliveryResult.UNKNOWN
    assert receipt.error_code == "malformed_turn_start_result"
    await runtime.aclose()


async def test_send_rejected_and_unknown_paths() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    server.fail("turn/start", RuntimeRequestError(-32000, "thread is archived"))
    rejected = await runtime.send(operation_id="op-4", prompt="x")
    assert rejected.result is DeliveryResult.REJECTED
    assert rejected.error_code == "turn_start_refused"
    assert "archived" in (rejected.error or "")

    server.fail("turn/start", RuntimeRequestTimeout())
    unknown = await runtime.send(operation_id="op-5", prompt="x")
    assert unknown.result is DeliveryResult.UNKNOWN
    assert unknown.error_code == "control_ack_timeout"
    # An acknowledgement timeout can follow backend acceptance.  The cached
    # idle snapshot predates that mutation, so this runtime must synchronously
    # stop presenting it as proof of idle and leave daemon-owned recovery to
    # reconnect the exact session.
    snapshot = await runtime.snapshot()
    assert snapshot.health is ConnectionHealth.DISCONNECTED
    assert snapshot.execution_state is RuntimeExecutionState.UNKNOWN
    assert snapshot.native_turn_id is None
    await runtime.aclose()


async def test_steer_sends_exact_expected_turn_id() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    receipt = await runtime.steer(
        operation_id="op-6", native_turn_id="turn-1", prompt="more detail"
    )
    assert receipt.result is DeliveryResult.ACCEPTED
    assert receipt.native_turn_id == "turn-1"
    assert server.requested("turn/steer") == [
        {
            "threadId": "ui-thread-1",
            "expectedTurnId": "turn-1",
            "input": [{"type": "text", "text": "more detail"}],
        }
    ]
    await runtime.aclose()


async def test_stale_steer_refusal_stays_a_refusal() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    server.fail("turn/steer", RuntimeRequestError(-32600, "no active turn to steer"))
    receipt = await runtime.steer(operation_id="op-7", native_turn_id="turn-gone", prompt="amend")
    assert receipt.result is DeliveryResult.REJECTED
    assert receipt.error_code == "stale_turn"
    # A refusal is never reinterpreted as a send or queue operation.
    assert server.requested("turn/start") == []
    assert server.requested("thread/queue/add") == []
    await runtime.aclose()


async def test_interrupt_targets_exact_turn_ids() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    server.push(
        RuntimeNotification(
            method="turn/started",
            params={"threadId": "ui-thread-1", "turn": {"id": "turn-1", "status": "inProgress"}},
        )
    )
    await asyncio.sleep(0.02)
    receipt = await runtime.interrupt(operation_id="op-8")
    assert receipt.result is DeliveryResult.ACCEPTED
    assert receipt.native_turn_id == "turn-1"
    assert server.requested("turn/interrupt") == [{"threadId": "ui-thread-1", "turnId": "turn-1"}]
    await runtime.aclose()


async def test_interrupt_without_active_turn_is_an_honest_refusal() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    receipt = await runtime.interrupt(operation_id="op-9")
    assert receipt.result is DeliveryResult.REJECTED
    assert receipt.error_code == "no_active_turn"
    assert server.requested("turn/interrupt") == []
    await runtime.aclose()


async def test_settings_update_is_gated_supplied_fields_only_and_read_back() -> None:
    from theater.harness.contracts.runtime import RuntimeCapability

    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    snapshot = await runtime.snapshot()
    assert snapshot.capabilities.supports(RuntimeCapability.SETTINGS_UPDATE) is True
    server.respond(
        "thread/read",
        {"thread": {"id": "ui-thread-1", "model": "gpt-5.2", "reasoningEffort": "high"}},
    )
    receipt = await runtime.update_settings(operation_id="op-10", reasoning_effort="high")
    assert receipt.result is DeliveryResult.ACCEPTED
    assert server.requested("thread/settings/update")[-1] == {
        "threadId": "ui-thread-1",
        "effort": "high",
    }
    snapshot = await runtime.snapshot()
    assert snapshot.settings.reasoning_effort == "high"
    await runtime.aclose()


async def test_settings_gate_failure_reports_explicit_unavailable_reason() -> None:
    server = ScriptedCodexServer()
    server.fail(
        "thread/settings/update",
        RuntimeRequestError(-32600, "thread/settings/update requires experimentalApi capability"),
    )
    runtime = make_runtime(server)
    server.push(thread_started())
    await runtime.open_session(mode=SessionOpenMode.NEW)
    snapshot = await runtime.snapshot()
    from theater.harness.contracts.runtime import RuntimeCapability

    assert snapshot.capabilities.supports(RuntimeCapability.SETTINGS_UPDATE) is False
    assert (
        snapshot.capabilities.reason_for(RuntimeCapability.SETTINGS_UPDATE)
        is CapabilityUnavailableReason.GATED_BY_BACKEND
    )
    receipt = await runtime.update_settings(operation_id="op-11", model="gpt-5.2")
    assert receipt.result is DeliveryResult.REJECTED
    assert receipt.error_code == "settings_unavailable"
    await runtime.aclose()


async def test_settings_are_idle_only() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    server.push(
        RuntimeNotification(
            method="turn/started",
            params={"threadId": "ui-thread-1", "turn": {"id": "turn-5", "status": "inProgress"}},
        )
    )
    await asyncio.sleep(0.02)
    receipt = await runtime.update_settings(operation_id="op-12", model="gpt-5.2")
    assert receipt.result is DeliveryResult.REJECTED
    assert receipt.error_code == "session_busy"
    await runtime.aclose()


async def test_settings_require_supplied_fields() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    receipt = await runtime.update_settings(operation_id="op-13")
    assert receipt.result is DeliveryResult.REJECTED
    assert receipt.error_code == "no_settings_fields"
    await runtime.aclose()


async def test_settings_update_with_unconfirmed_readback_exposes_uncertainty() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    # The backend accepts the update but the native readback cannot confirm
    # the effective settings.
    server.fail("thread/read", RuntimeRequestError(-32000, "read failed"))
    receipt = await runtime.update_settings(operation_id="op-unconfirmed", model="gpt-5.2")
    # The application stays visibly uncertain: an UNKNOWN receipt with an
    # explicit error code — never an optimistic confirmed success — plus
    # degraded health and diagnostics; the confirmed settings stay untouched.
    assert receipt.result is DeliveryResult.UNKNOWN
    assert receipt.error_code == "settings_unconfirmed"
    snapshot = await runtime.snapshot()
    assert snapshot.health is ConnectionHealth.DEGRADED
    assert any("unconfirmed" in note for note in snapshot.health_diagnostics)
    assert snapshot.settings.model is None
    health = runtime.live_source().health_snapshot()[0]
    assert health.state is ChannelHealthState.DEGRADED
    await runtime.aclose()


async def test_queue_followup_capability_reports_theater_policy() -> None:
    from theater.harness.contracts.runtime import RuntimeCapability

    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    snapshot = await runtime.snapshot()
    assert snapshot.capabilities.supports(RuntimeCapability.QUEUE_FOLLOWUP) is False
    assert (
        snapshot.capabilities.reason_for(RuntimeCapability.QUEUE_FOLLOWUP)
        is CapabilityUnavailableReason.THEATER_POLICY
    )
    await runtime.aclose()


async def test_native_queue_methods_are_never_used() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    await runtime.send(operation_id="op-14", prompt="x")
    await runtime.steer(operation_id="op-15", native_turn_id="turn-1", prompt="y")
    await runtime.interrupt(operation_id="op-16", native_turn_id="turn-1")
    await runtime.update_settings(operation_id="op-17", model="gpt-5.2")
    for method, _params in server.requests:
        assert not method.startswith("thread/queue"), method
    await runtime.aclose()
    assert server.close_count == 1
    assert server.backend_alive is True


# ---------------------------------------------------------------------------
# Approval and clarification: observed only, never answered
# ---------------------------------------------------------------------------


async def test_approval_server_request_recorded_never_answered() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    server.push(
        RuntimeNotification(
            method="item/commandExecution/requestApproval",
            params={
                "threadId": "ui-thread-1",
                "turnId": "turn-1",
                "itemId": "item-1",
                "reason": "Allow creating file?",
                "command": "/bin/zsh -lc 'touch /repo/x'",
            },
            # Wave 0 observed the server-assigned id as integer 0; the exact
            # captured type must be retained.
            request_id=0,
        )
    )
    await asyncio.sleep(0.02)
    snapshot = await runtime.snapshot()
    interaction = snapshot.pending_interaction
    assert interaction is not None
    assert interaction.kind.value == "approval"
    assert interaction.native_request_id == 0
    assert isinstance(interaction.native_request_id, int)
    assert interaction.native_turn_id == "turn-1"
    assert interaction.native_item_id == "item-1"
    assert "Allow creating" in interaction.details
    # The runtime never answers: the only notification ever sent is the
    # initialized handshake, and no decision request appears.
    assert [method for method, _ in server.notifications_sent] == ["initialized"]
    assert not any("decision" in method for method, _ in server.requests)
    # Resolution comes from the native side only.
    server.push(
        RuntimeNotification(
            method="serverRequest/resolved",
            params={"threadId": "ui-thread-1", "requestId": 0},
        )
    )
    await asyncio.sleep(0.02)
    snapshot = await runtime.snapshot()
    assert snapshot.pending_interaction is None
    await runtime.aclose()


async def test_clarification_questions_recorded_and_superseded_by_new_turn() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    server.push(
        RuntimeNotification(
            method="item/completed",
            params={
                "threadId": "ui-thread-1",
                "turnId": "turn-1",
                "completedAtMs": 1000,
                "item": {
                    "id": "item-q",
                    "type": "agentMessage",
                    "text": "Which option?",
                    "questions": [{"title": "Pick one", "options": ["a", "b"]}],
                },
            },
        )
    )
    await asyncio.sleep(0.02)
    snapshot = await runtime.snapshot()
    assert snapshot.pending_interaction is not None
    assert snapshot.pending_interaction.kind.value == "clarification"
    assert "Pick one" in snapshot.pending_interaction.details
    # The human answers by typing, which starts a new turn.
    server.push(
        RuntimeNotification(
            method="turn/started",
            params={"threadId": "ui-thread-1", "turn": {"id": "turn-2", "status": "inProgress"}},
        )
    )
    await asyncio.sleep(0.02)
    snapshot = await runtime.snapshot()
    assert snapshot.pending_interaction is None
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Live Source: normalization, dedupe, bounds, status, recovery
# ---------------------------------------------------------------------------


async def test_live_source_is_one_instance_for_the_runtime_lifetime() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    first = runtime.live_source()
    # Exactly one Source instance for the whole runtime lifetime.
    assert runtime.live_source() is first
    assert runtime.live_source() is first
    # One instance means one status cursor: drains never duplicate or steal.
    server.push(
        RuntimeNotification(
            method="thread/status/changed",
            params={"threadId": "ui-thread-1", "status": {"type": "active"}},
        )
    )
    await asyncio.sleep(0.02)
    batch = await first.read()
    assert batch.status is Status.WORKING
    # The cached source does not re-emit the same status transition; an
    # independent second instance would have reset the cursor and stolen it.
    quiet = await first.read()
    assert quiet.progressed is False
    await runtime.aclose()


async def test_cancelled_saturated_enqueue_rolls_back_dedupe_for_replay() -> None:
    from theater.harness.contracts.runtime import (
        NativeTurnTerminal,
        ResultCompleteness,
        ResultProvenance,
    )

    server = ScriptedCodexServer()
    server.respond(
        "thread/resume", {"thread": {"id": "th-1", "status": {"type": "idle"}, "turns": []}}
    )
    runtime = make_runtime(server)
    await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="th-1")
    source = runtime.live_source()

    async def record(turn_id: str) -> None:
        await runtime._record_turn_outcome(
            "th-1",
            turn_id,
            NativeTurnTerminal.COMPLETED,
            result=None,
            completeness=ResultCompleteness.UNAVAILABLE,
            provenance=ResultProvenance.NATIVE_EVIDENCE,
            error=None,
        )

    # Saturate the bounded terminal-evidence queue.
    for index in range(codex_runtime_module.CODEX_RUNTIME_OUTCOMES_BUFFER):
        await record(f"turn-{index}")
    # An insertion blocked on the full queue holds a pending dedupe key...
    blocked = asyncio.get_running_loop().create_task(record("turn-lost"))
    await asyncio.sleep(0.01)
    # ...so a concurrent insert of the same turn is deduped against it and
    # never double-inserts.
    duplicate = asyncio.get_running_loop().create_task(record("turn-lost"))
    await asyncio.sleep(0.01)
    assert duplicate.done() is True
    assert not duplicate.exception()
    # Cancelling the blocked insertion must roll the pending key back: the
    # outcome was never enqueued, so a replay may retry loss-free.
    blocked.cancel()
    with pytest.raises(asyncio.CancelledError):
        await blocked
    # Drain the saturated queue, then replay the same turn.
    drained = await source.read()
    assert len(drained.terminal_evidence) == codex_runtime_module.CODEX_RUNTIME_OUTCOMES_BUFFER
    await record("turn-lost")
    replayed = await source.read()
    assert [outcome.native_turn_id for outcome in replayed.terminal_evidence] == ["turn-lost"]
    # Exactly one "turn-lost" outcome exists across cancel, duplicate, and
    # replay: nothing was dropped and nothing was doubled.
    ids = [outcome.native_turn_id for outcome in drained.terminal_evidence]
    assert "turn-lost" not in ids
    await runtime.aclose()


def completed_item(
    item_id: str,
    *,
    item_type: str,
    turn_id: str = "turn-1",
    text: str = "answer text",
) -> RuntimeNotification:
    item: dict[str, object] = {"id": item_id, "type": item_type}
    if item_type == "agentMessage":
        item["text"] = text
    elif item_type == "userMessage":
        item["content"] = [{"type": "text", "text": text}]
    return RuntimeNotification(
        method="item/completed",
        params={
            "threadId": "ui-thread-1",
            "turnId": turn_id,
            "completedAtMs": 1234,
            "item": item,
        },
    )


async def test_source_normalizes_items_once_by_native_id() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    server.push(completed_item("i-user", item_type="userMessage", text="do it"))
    server.push(completed_item("i-agent", item_type="agentMessage", text="done"))
    await asyncio.sleep(0.05)
    batch = await source.read()
    assert [event.kind.value for event in batch.events] == ["user", "assistant"]
    assert [event.text for event in batch.events] == ["do it", "done"]
    assert batch.progressed is True
    # The backend replayed the same completed item: identity, not text,
    # decides; nothing is normalized a second time.
    server.push(completed_item("i-agent", item_type="agentMessage", text="done"))
    await asyncio.sleep(0.05)
    replay = await source.read()
    assert not replay.events
    await runtime.aclose()


async def test_item_started_then_deltas_then_completed_emits_exactly_once() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    # The exact Wave 0 sequence: item/started → deltas → item/completed.
    server.push(
        RuntimeNotification(
            method="item/started",
            params={
                "threadId": "ui-thread-1",
                "turnId": "turn-1",
                "startedAtMs": 1000,
                "item": {"id": "i-agent", "type": "agentMessage"},
            },
        )
    )
    server.push(
        RuntimeNotification(
            method="item/agentMessage/delta",
            params={
                "threadId": "ui-thread-1",
                "turnId": "turn-1",
                "itemId": "i-agent",
                "delta": "partial",
            },
        )
    )
    server.push(completed_item("i-agent", item_type="agentMessage", text="final answer"))
    await asyncio.sleep(0.05)
    batch = await source.read()
    # item/started must not mark the completion dedupe ledger: the completed
    # item is normalized exactly once...
    assistant = [event for event in batch.events if event.kind.value == "assistant"]
    assert [event.text for event in assistant] == ["final answer"]
    # ...and a replayed item/completed emits zero.
    server.push(completed_item("i-agent", item_type="agentMessage", text="final answer"))
    await asyncio.sleep(0.05)
    replay = await source.read()
    assert [event for event in replay.events if event.kind.value == "assistant"] == []
    await runtime.aclose()


async def test_live_events_carry_native_identity_and_revision() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    server.push(completed_item("i-user", item_type="userMessage", text="do it"))
    server.push(completed_item("i-agent", item_type="agentMessage", text="done"))
    await asyncio.sleep(0.05)
    batch = await source.read()
    user, assistant = batch.events
    # Native item identity rides every completed-user/assistant event so the
    # composition reconciles live and durable records by identity.
    assert user.native_id == "i-user"
    assert assistant.native_id == "i-agent"
    # Anonymous default revision when the backend reports none.
    assert user.revision == 0
    assert assistant.revision == 0
    await runtime.aclose()


async def test_live_event_revision_comes_from_the_native_item() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    server.push(
        RuntimeNotification(
            method="item/completed",
            params={
                "threadId": "ui-thread-1",
                "turnId": "turn-1",
                "completedAtMs": 1234,
                "item": {
                    "id": "i-agent",
                    "type": "agentMessage",
                    "text": "done",
                    "revision": 7,
                },
            },
        )
    )
    await asyncio.sleep(0.05)
    batch = await source.read()
    assert batch.events[0].native_id == "i-agent"
    assert batch.events[0].revision == 7
    await runtime.aclose()


def test_native_revision_is_bounded_and_fails_closed() -> None:
    helper = codex_runtime_module._native_revision
    assert helper({}) == 0
    assert helper({"revision": -3}) == 0
    assert helper({"revision": "7"}) == 0
    assert helper({"revision": True}) == 0
    assert helper({"revision": 7}) == 7
    cap = codex_runtime_module.CODEX_RUNTIME_REVISION_MAX
    assert helper({"revision": cap + 5}) == cap


async def test_activity_callback_fires_on_live_arrival_and_detaches() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    calls: list[str] = []
    source.set_activity_callback(lambda: calls.append("wake"))

    # A completed item arrives: the callback fires once, coalesced — no task
    # per message and no daemon import.
    server.push(completed_item("i-agent", item_type="agentMessage", text="done"))
    await asyncio.sleep(0.05)
    assert calls == ["wake"]

    # A live delta arrival wakes too.
    server.push(
        RuntimeNotification(
            method="item/agentMessage/delta",
            params={
                "threadId": "ui-thread-1",
                "turnId": "turn-1",
                "itemId": "i-agent",
                "delta": "x",
            },
        )
    )
    await asyncio.sleep(0.05)
    assert calls == ["wake", "wake"]

    # Detach via None: further arrivals never call back.
    source.set_activity_callback(None)
    server.push(completed_item("i-user", item_type="userMessage", text="again"))
    await asyncio.sleep(0.05)
    assert calls == ["wake", "wake"]
    await runtime.aclose()


async def test_failed_activity_callback_degrades_health_and_detaches() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()

    def boom() -> None:
        raise RuntimeError("callback exploded")

    source.set_activity_callback(boom)
    server.push(completed_item("i-agent", item_type="agentMessage", text="done"))
    await asyncio.sleep(0.05)

    # The arrival still normalized; the broken wake hint degraded the health
    # visibly and detached itself instead of poisoning the receive loop.
    batch = await source.read()
    assert [event.native_id for event in batch.events] == ["i-agent"]
    snapshot = await runtime.snapshot()
    assert snapshot.health is ConnectionHealth.DEGRADED
    server.push(completed_item("i-user", item_type="userMessage", text="later"))
    await asyncio.sleep(0.05)
    assert len((await source.read()).events) == 1
    await runtime.aclose()


def test_activity_callback_rejects_non_callable() -> None:
    server = ScriptedCodexServer()
    runtime = make_runtime(server)
    with pytest.raises(TypeError, match="callable"):
        runtime.set_activity_callback("not callable")  # type: ignore[arg-type]


async def test_state_only_arrivals_fire_the_activity_callback() -> None:
    """Status, approval, and resolution state is readable only after a wake."""
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    calls: list[str] = []
    source.set_activity_callback(lambda: calls.append("wake"))

    # thread/status/changed carries no event, fact, or outcome: the state
    # mutation itself must wake observation.
    server.push(
        RuntimeNotification(
            method="thread/status/changed",
            params={"threadId": "ui-thread-1", "status": {"type": "active"}},
        )
    )
    await asyncio.sleep(0.05)
    assert calls == ["wake"]
    batch = await source.read()
    assert batch.status is Status.WORKING

    # A foreign thread's status never touches this runtime: no wake.
    server.push(
        RuntimeNotification(
            method="thread/status/changed",
            params={"threadId": "other-thread", "status": {"type": "idle"}},
        )
    )
    await asyncio.sleep(0.05)
    assert calls == ["wake"]

    # A recorded approval request flips the readable status to
    # AWAITING_INPUT with no event or fact landing.
    server.push(
        RuntimeNotification(
            method="item/commandExecution/requestApproval",
            params={
                "threadId": "ui-thread-1",
                "turnId": "turn-1",
                "itemId": "item-1",
                "reason": "Allow creating file?",
                "command": "/bin/zsh -lc 'touch /repo/x'",
            },
            request_id=0,
        )
    )
    await asyncio.sleep(0.05)
    assert calls == ["wake", "wake"]
    batch = await source.read()
    assert batch.status is Status.AWAITING_INPUT

    # Resolving the request clears the interaction: a state-only wake again.
    server.push(
        RuntimeNotification(
            method="serverRequest/resolved",
            params={"threadId": "ui-thread-1", "requestId": 0},
        )
    )
    await asyncio.sleep(0.05)
    assert calls == ["wake", "wake", "wake"]
    batch = await source.read()
    assert batch.status is not Status.AWAITING_INPUT

    # An adopted settings update is readable capability state.
    server.push(
        RuntimeNotification(
            method="thread/settings/updated",
            params={
                "threadId": "ui-thread-1",
                "threadSettings": {"model": "gpt-5.6-sol", "reasoningEffort": "high"},
            },
        )
    )
    await asyncio.sleep(0.05)
    assert calls == ["wake", "wake", "wake", "wake"]

    # An ignored settings update (nothing adoptable) never wakes.
    server.push(
        RuntimeNotification(
            method="thread/settings/updated",
            params={"threadId": "ui-thread-1", "threadSettings": {}},
        )
    )
    await asyncio.sleep(0.05)
    assert calls == ["wake", "wake", "wake", "wake"]
    await runtime.aclose()


async def test_turn_started_state_wakes_without_events() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    calls: list[str] = []
    source.set_activity_callback(lambda: calls.append("wake"))

    server.push(
        RuntimeNotification(
            method="turn/started",
            params={"threadId": "ui-thread-1", "turn": {"id": "turn-9"}},
        )
    )
    await asyncio.sleep(0.05)

    # The active-turn/status mutation is readable with no event or fact.
    assert calls == ["wake"]
    batch = await source.read()
    assert batch.status is Status.WORKING

    # A foreign turn never wakes.
    server.push(
        RuntimeNotification(
            method="turn/started",
            params={"threadId": "other-thread", "turn": {"id": "turn-x"}},
        )
    )
    await asyncio.sleep(0.05)
    assert calls == ["wake"]
    await runtime.aclose()


async def test_live_and_durable_item_events_reconcile_by_native_identity() -> None:
    """End to end: live normalization and the durable parser agree on identity."""
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    live = runtime.live_source()

    # Actual live normalization: one completed agent message item.
    server.push(completed_item("i-agent", item_type="agentMessage", text="done"))
    await asyncio.sleep(0.05)

    # Actual durable parser output for the same native item, from the
    # canonical paginated-rollout shape.
    durable_observer = CodexObserver()
    parsed = durable_observer.parse_record(
        json.dumps(
            {
                "timestamp": "2026-09-10T14:00:00.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "thread_id": "ui-thread-1",
                    "turn_id": "turn-1",
                    "item": {
                        "type": "AgentMessage",
                        "id": "i-agent",
                        "content": [{"type": "Text", "text": "done"}],
                    },
                },
            }
        ),
        0,
    )
    # The durable parser stamps the exact native item identity.
    assert [event.native_id for event in parsed.events] == ["i-agent"]
    assert parsed.events[0].kind.value == "assistant"

    class DurableRollout(Source):
        """Emits nothing on the first read, the parsed item on the second."""

        def __init__(self) -> None:
            self.reads = 0

        async def read(self) -> Batch:
            self.reads += 1
            if self.reads == 1:
                return Batch()
            return Batch(events=tuple(parsed.events))

    source = HybridSource(
        durable=DurableRollout(),
        live=live,
        live_channel=LiveChannelDeclaration(
            channel=ChannelDeclaration(
                id=codex_runtime_module._LIVE_CHANNEL_ID, kind=ChannelKind.LIVE
            )
        ),
    )

    first = await source.read()
    assert [(event.native_id, event.text) for event in first.events] == [("i-agent", "done")]

    # The durable transcript caught up after live already emitted the item:
    # the same native id is suppressed, never emitted twice.
    second = await source.read()
    assert second.events == ()
    await runtime.aclose()


async def test_foreign_thread_payloads_never_leak_once_bound() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    foreign = [
        RuntimeNotification(
            method="thread/status/changed",
            params={"threadId": "other-thread", "status": {"type": "active"}},
        ),
        RuntimeNotification(
            method="turn/started",
            params={"threadId": "other-thread", "turn": {"id": "t-other"}},
        ),
        RuntimeNotification(
            method="turn/completed",
            params={
                "threadId": "other-thread",
                "turn": {
                    "id": "t-other",
                    "status": "completed",
                    "itemsView": "full",
                    "items": [],
                },
            },
        ),
        RuntimeNotification(
            method="item/completed",
            params={
                "threadId": "other-thread",
                "turnId": "t-other",
                "completedAtMs": 1,
                "item": {"id": "i-foreign", "type": "agentMessage", "text": "foreign text"},
            },
        ),
        RuntimeNotification(
            method="item/agentMessage/delta",
            params={
                "threadId": "other-thread",
                "turnId": "t-other",
                "itemId": "i-foreign",
                "delta": "foreign delta",
            },
        ),
        RuntimeNotification(
            method="thread/settings/updated",
            params={"threadId": "other-thread", "threadSettings": {"model": "foreign-model"}},
        ),
        RuntimeNotification(
            method="serverRequest/resolved",
            params={"threadId": "other-thread", "requestId": 0},
        ),
        RuntimeNotification(
            method="item/commandExecution/requestApproval",
            params={
                "threadId": "other-thread",
                "turnId": "t-other",
                "itemId": "i-foreign",
                "reason": "foreign approval",
            },
            request_id=7,
        ),
        # threadId is required by the verified schema; a payload missing it is
        # malformed and exact identity is never guessed.
        RuntimeNotification(
            method="turn/completed",
            params={
                "turn": {
                    "id": "t-nothread",
                    "status": "completed",
                    "itemsView": "full",
                    "items": [],
                }
            },
        ),
    ]
    for notification in foreign:
        server.push(notification)
    await asyncio.sleep(0.05)
    snapshot = await runtime.snapshot()
    # No foreign status, interaction, item, active turn, settings, or
    # terminal evidence leaked into this runtime.
    assert snapshot.native_turn_id is None
    assert snapshot.pending_interaction is None
    assert snapshot.settings.model is None
    batch = await source.read()
    assert not batch.events
    assert batch.terminal_evidence == ()
    assert batch.status is None
    await runtime.aclose()


async def test_terminal_evidence_backpressure_is_loss_free() -> None:
    server = ScriptedCodexServer()
    server.respond(
        "thread/resume", {"thread": {"id": "th-1", "status": {"type": "idle"}, "turns": []}}
    )
    runtime = make_runtime(server)
    await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="th-1")
    source = runtime.live_source()
    total = codex_runtime_module.CODEX_RUNTIME_OUTCOMES_BUFFER + 10
    for index in range(total):
        server.push(
            RuntimeNotification(
                method="turn/completed",
                params={
                    "threadId": "th-1",
                    "turn": {
                        "id": f"turn-{index}",
                        "status": "completed",
                        "error": None,
                        "itemsView": "full",
                        "items": [],
                    },
                },
            )
        )
    # The receive loop blocks on the saturated queue and resumes only as the
    # Source drains: every exact terminal turn id is eventually emitted
    # exactly once, in order, with nothing dropped.
    collected: list[str] = []
    for _ in range(4000):
        batch = await source.read()
        collected.extend(outcome.native_turn_id for outcome in batch.terminal_evidence)
        if len(collected) == total:
            break
        await asyncio.sleep(0.002)
    assert collected == [f"turn-{index}" for index in range(total)]
    # Connection processing resumed after the barrier: a later live item is
    # still normalized.
    server.push(
        RuntimeNotification(
            method="item/completed",
            params={
                "threadId": "th-1",
                "turnId": "turn-after",
                "completedAtMs": 42,
                "item": {"id": "i-after", "type": "agentMessage", "text": "resumed"},
            },
        )
    )
    for _ in range(400):
        batch = await source.read()
        if any(event.text == "resumed" for event in batch.events):
            break
        await asyncio.sleep(0.005)
    else:
        pytest.fail("connection processing did not resume after the backpressure barrier")
    # Backpressure dropped nothing: the channel never degraded.
    health = source.health_snapshot()[0]
    assert health.dropped == 0
    await runtime.aclose()


async def test_result_at_contract_bound_stays_complete() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    bounded = "x" * HARNESS_RUNTIME_RESULT_MAX_CHARS
    server.push(
        RuntimeNotification(
            method="turn/completed",
            params={
                "threadId": "ui-thread-1",
                "turn": {
                    "id": "turn-bound",
                    "status": "completed",
                    "error": None,
                    "itemsView": "full",
                    "items": [{"id": "i-agent", "type": "agentMessage", "text": bounded}],
                },
            },
        )
    )
    await asyncio.sleep(0.05)
    outcome = (await source.read()).terminal_evidence[0]
    assert len(outcome.result) == HARNESS_RUNTIME_RESULT_MAX_CHARS
    assert outcome.completeness.value == "complete"
    assert outcome.provenance.value == "native_evidence"
    await runtime.aclose()


async def test_result_over_contract_bound_is_bounded_and_partial() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    oversized = "x" * (HARNESS_RUNTIME_RESULT_MAX_CHARS + 1000)
    server.push(
        RuntimeNotification(
            method="turn/completed",
            params={
                "threadId": "ui-thread-1",
                "turn": {
                    "id": "turn-over",
                    "status": "completed",
                    "error": None,
                    "itemsView": "full",
                    "items": [{"id": "i-agent", "type": "agentMessage", "text": oversized}],
                },
            },
        )
    )
    await asyncio.sleep(0.05)
    outcome = (await source.read()).terminal_evidence[0]
    # The stored result is bounded at the contract limit and its
    # completeness is downgraded — never COMPLETE for a truncated result.
    assert len(outcome.result) == HARNESS_RUNTIME_RESULT_MAX_CHARS
    assert outcome.completeness.value == "partial"
    assert outcome.provenance.value == "native_evidence"
    await runtime.aclose()


async def test_oversized_native_turn_error_is_bounded_not_fatal() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    oversized = "x" * (HARNESS_RUNTIME_ERROR_MAX_CHARS + 500)
    server.push(
        RuntimeNotification(
            method="turn/completed",
            params={
                "threadId": "ui-thread-1",
                "turn": {
                    "id": "turn-fail",
                    "status": "failed",
                    "error": {"message": oversized},
                    "itemsView": "full",
                    "items": [],
                },
            },
        )
    )
    await asyncio.sleep(0.05)
    # The untrusted native error text is bounded before the contract object:
    # the runtime stays connected and the terminal evidence is emitted
    # instead of a validation error disconnecting the receive loop.
    snapshot = await runtime.snapshot()
    assert snapshot.health is ConnectionHealth.CONNECTED
    batch = await source.read()
    assert [outcome.native_turn_id for outcome in batch.terminal_evidence] == ["turn-fail"]
    outcome = batch.terminal_evidence[0]
    assert outcome.error is not None
    assert len(outcome.error) == HARNESS_RUNTIME_ERROR_MAX_CHARS
    assert outcome.error_code == "turn_failed"
    assert outcome.terminal.value == "failed"
    await runtime.aclose()


def summary_turn_completed(
    *,
    thread_id: str = "ui-thread-1",
    turn_id: str = "turn-sum",
    status: str = "completed",
    items_view: object = None,
    items: list[dict[str, object]] | None = None,
    error: object = None,
) -> RuntimeNotification:
    """One turn/completed with an explicit item view, stock 0.154.0 shape.

    Stock codex-cli 0.154.0 emits this notification with itemsView=summary:
    upstream ``emit_turn_completed_with_status``
    (codex-rs/app-server/src/bespoke_event_handling.rs) builds ``items`` as
    exactly the one-item ``TurnCompletionMetadata.last_agent_message`` —
    the exact, complete final agent message that
    ``ThreadState::track_current_turn_event``
    (codex-rs/app-server/src/thread_state.rs) recorded only from a completed
    agentMessage with ``FinalAnswer`` (or absent) phase and non-empty text.
    """
    turn: dict[str, object] = {
        "id": turn_id,
        "status": status,
        "itemsView": items_view,
        "items": items if items is not None else [],
        "error": error,
    }
    return RuntimeNotification(
        method="turn/completed", params={"threadId": thread_id, "turn": turn}
    )


async def test_summary_view_final_agent_message_is_exact_complete_evidence() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    server.push(
        summary_turn_completed(
            items_view="summary",
            items=[{"id": "i-final", "type": "agentMessage", "text": "exact final answer"}],
        )
    )
    await asyncio.sleep(0.05)
    (outcome,) = (await source.read()).terminal_evidence
    assert outcome.native_turn_id == "turn-sum"
    assert outcome.terminal.value == "completed"
    assert outcome.result == "exact final answer"
    # The narrow promotion: the verified stock schema guarantees a completed
    # turn's summary carries the exact final agent message, so the evidence
    # is exact and native — never downgraded to partial live-stream merely
    # because non-result turn items were omitted.
    assert outcome.completeness.value == "complete"
    assert outcome.provenance.value == "native_evidence"
    await runtime.aclose()


async def test_summary_view_without_agent_message_invents_no_result() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    # A summary with no agentMessage text carries no final result: nothing is
    # promoted and no result is invented.
    server.push(summary_turn_completed(items_view="summary", items=[]))
    server.push(
        summary_turn_completed(
            turn_id="turn-user-only",
            items_view="summary",
            items=[
                {"id": "i-user", "type": "userMessage", "content": [{"type": "text", "text": "q"}]}
            ],
        )
    )
    await asyncio.sleep(0.05)
    outcomes = (await source.read()).terminal_evidence
    assert [outcome.result for outcome in outcomes] == [None, None]
    assert [outcome.completeness.value for outcome in outcomes] == [
        "unavailable",
        "unavailable",
    ]
    assert all(outcome.provenance.value == "native_evidence" for outcome in outcomes)
    await runtime.aclose()


async def test_not_loaded_view_is_never_promoted() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    # Stock failed/interrupted completions arrive as notLoaded with empty
    # items: no result is available and none is invented.
    server.push(
        summary_turn_completed(
            turn_id="turn-failed",
            status="failed",
            items_view="notLoaded",
            error={"message": "boom"},
        )
    )
    # A notLoaded view that nevertheless carries item text is not trusted as
    # the exact final message: no promotion.
    server.push(
        summary_turn_completed(
            turn_id="turn-notloaded-text",
            items_view="notLoaded",
            items=[{"id": "i-agent", "type": "agentMessage", "text": "untrusted text"}],
        )
    )
    await asyncio.sleep(0.05)
    outcomes = (await source.read()).terminal_evidence
    assert [outcome.native_turn_id for outcome in outcomes] == [
        "turn-failed",
        "turn-notloaded-text",
    ]
    assert outcomes[0].result is None
    assert outcomes[0].completeness.value == "unavailable"
    assert outcomes[0].error == "boom"
    assert outcomes[1].result == "untrusted text"
    assert outcomes[1].completeness.value == "partial"
    assert outcomes[1].provenance.value == "live_stream"
    await runtime.aclose()


async def test_unknown_and_malformed_item_views_are_never_promoted() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    server.push(
        summary_turn_completed(
            turn_id="turn-unknown",
            items_view="everything",
            items=[{"id": "i-agent", "type": "agentMessage", "text": "some text"}],
        )
    )
    server.push(
        summary_turn_completed(
            turn_id="turn-malformed",
            items_view=123,
            items=[{"id": "i-agent", "type": "agentMessage", "text": "more text"}],
        )
    )
    await asyncio.sleep(0.05)
    outcomes = (await source.read()).terminal_evidence
    # Unknown and malformed views are not the verified summary shape: no
    # promotion, the text stays partial live-stream evidence.
    assert [outcome.completeness.value for outcome in outcomes] == ["partial", "partial"]
    assert all(outcome.provenance.value == "live_stream" for outcome in outcomes)
    await runtime.aclose()


async def test_summary_view_failed_and_interrupted_semantics_stay_partial() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    # Stock never emits a summary for failed/interrupted completions
    # (last_agent_message is None there, so the view is notLoaded); a summary
    # that nonetheless arrives with a non-completed terminal is never
    # promoted — only COMPLETED turns carry the guaranteed final message.
    server.push(
        summary_turn_completed(
            turn_id="turn-failed-sum",
            status="failed",
            items_view="summary",
            error={"message": "boom"},
            items=[{"id": "i-agent", "type": "agentMessage", "text": "partial text"}],
        )
    )
    server.push(
        summary_turn_completed(
            turn_id="turn-interrupted-sum",
            status="interrupted",
            items_view="summary",
            items=[{"id": "i-agent", "type": "agentMessage", "text": "cut short"}],
        )
    )
    await asyncio.sleep(0.05)
    outcomes = (await source.read()).terminal_evidence
    assert [outcome.terminal.value for outcome in outcomes] == ["failed", "interrupted"]
    assert all(outcome.completeness.value == "partial" for outcome in outcomes)
    assert all(outcome.provenance.value == "live_stream" for outcome in outcomes)
    assert outcomes[0].error == "boom"
    await runtime.aclose()


async def test_oversized_summary_result_stays_bounded_and_partial() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    oversized = "x" * (HARNESS_RUNTIME_RESULT_MAX_CHARS + 500)
    server.push(
        summary_turn_completed(
            items_view="summary",
            items=[{"id": "i-final", "type": "agentMessage", "text": oversized}],
        )
    )
    await asyncio.sleep(0.05)
    (outcome,) = (await source.read()).terminal_evidence
    # The narrow promotion holds for the exact message, but the stored
    # result is bounded at the contract limit and downgraded — never
    # COMPLETE for a truncated result.
    assert len(outcome.result) == HARNESS_RUNTIME_RESULT_MAX_CHARS
    assert outcome.completeness.value == "partial"
    assert outcome.provenance.value == "native_evidence"
    await runtime.aclose()


async def test_reconcile_summary_result_is_partial_evidence_once() -> None:
    server = ScriptedCodexServer()
    # thread/resume (excludeTurns=false) defaults its turns payload to a
    # summary view; the summary projection keeps only the first user
    # message plus the final agent message
    # (apply_thread_turns_items_view,
    # codex-rs/app-server/src/request_processors/thread_processor.rs).
    server.respond(
        "thread/resume",
        {
            "thread": {
                "id": "th-1",
                "status": {"type": "idle"},
                "turns": [
                    {
                        "id": "turn-1",
                        "status": "completed",
                        "error": None,
                        "itemsView": "summary",
                        "items": [
                            {
                                "id": "i-user",
                                "type": "userMessage",
                                "content": [{"type": "text", "text": "work"}],
                            },
                            {
                                "id": "i-agent",
                                "type": "agentMessage",
                                "text": "recovered final answer",
                            },
                        ],
                    }
                ],
            }
        },
    )
    runtime = make_runtime(server)
    await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="th-1")
    source = runtime.live_source()
    (outcome,) = (await source.read()).terminal_evidence
    assert outcome.native_session_id == "th-1"
    assert outcome.native_turn_id == "turn-1"
    assert outcome.result == "recovered final answer"
    # The frozen NativeTurnOutcome contract keeps a snapshot-derived result
    # at most partial — a thread/resume snapshot is never the terminal
    # notification itself, whatever item view it carries. The narrow
    # summary promotion applies only to the live turn/completed payload.
    assert outcome.completeness.value == "partial"
    assert outcome.provenance.value == "native_evidence"
    # Exact-once: a live turn/completed replay for the same turn never
    # duplicates the reconciled terminal evidence.
    server.push(
        summary_turn_completed(
            thread_id="th-1",
            turn_id="turn-1",
            items_view="summary",
            items=[{"id": "i-agent", "type": "agentMessage", "text": "recovered final answer"}],
        )
    )
    await asyncio.sleep(0.05)
    replay = await source.read()
    assert replay.terminal_evidence == ()
    await runtime.aclose()


async def test_reconcile_summary_without_result_invents_nothing() -> None:
    server = ScriptedCodexServer()
    server.respond(
        "thread/resume",
        {
            "thread": {
                "id": "th-1",
                "status": {"type": "idle"},
                "turns": [
                    {
                        "id": "turn-empty",
                        "status": "completed",
                        "error": None,
                        "itemsView": "summary",
                        "items": [],
                    }
                ],
            }
        },
    )
    runtime = make_runtime(server)
    await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="th-1")
    source = runtime.live_source()
    (outcome,) = (await source.read()).terminal_evidence
    # A reconciled summary with no agent message carries no result: nothing
    # is invented, and the outcome is recorded exactly once.
    assert outcome.native_turn_id == "turn-empty"
    assert outcome.result is None
    assert outcome.completeness.value == "partial"
    assert outcome.provenance.value == "native_evidence"
    server.push(
        summary_turn_completed(
            thread_id="th-1",
            turn_id="turn-empty",
            items_view="summary",
            items=[{"id": "i-agent", "type": "agentMessage", "text": "late arrival"}],
        )
    )
    await asyncio.sleep(0.05)
    replay = await source.read()
    assert replay.terminal_evidence == ()
    await runtime.aclose()


async def test_source_terminal_evidence_exact_and_once() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    turn_completed = RuntimeNotification(
        method="turn/completed",
        params={
            "threadId": "ui-thread-1",
            "turn": {
                "id": "turn-1",
                "status": "completed",
                "error": None,
                "itemsView": "full",
                "items": [{"id": "i-agent", "type": "agentMessage", "text": "final answer"}],
            },
        },
    )
    server.push(turn_completed)
    await asyncio.sleep(0.05)
    batch = await source.read()
    assert len(batch.terminal_evidence) == 1
    outcome = batch.terminal_evidence[0]
    assert outcome.native_session_id == "ui-thread-1"
    assert outcome.native_turn_id == "turn-1"
    assert outcome.terminal.value == "completed"
    assert outcome.result == "final answer"
    assert outcome.completeness.value == "complete"
    assert outcome.provenance.value == "native_evidence"
    # Replayed turn/completed never repeats terminal evidence.
    server.push(turn_completed)
    await asyncio.sleep(0.05)
    replay = await source.read()
    assert replay.terminal_evidence == ()
    await runtime.aclose()


async def test_status_broadcasts_never_create_terminal_evidence() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    server.push(
        RuntimeNotification(
            method="thread/status/changed",
            params={"threadId": "ui-thread-1", "status": {"type": "active"}},
        )
    )
    server.push(
        RuntimeNotification(
            method="thread/status/changed",
            params={"threadId": "ui-thread-1", "status": {"type": "idle"}},
        )
    )
    await asyncio.sleep(0.05)
    batch = await source.read()
    assert batch.terminal_evidence == ()
    assert batch.status is Status.IDLE
    assert batch.progressed is True
    await runtime.aclose()


async def test_delta_previews_are_coalesced_and_bounded() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    for chunk in ("alpha ", "beta ", "gamma"):
        server.push(
            RuntimeNotification(
                method="item/agentMessage/delta",
                params={
                    "threadId": "ui-thread-1",
                    "turnId": "turn-1",
                    "itemId": "i-live",
                    "delta": chunk,
                },
            )
        )
    server.push(
        RuntimeNotification(
            method="item/agentMessage/delta",
            params={
                "threadId": "ui-thread-1",
                "turnId": "turn-1",
                "itemId": "i-live",
                "delta": "x" * 5000,
            },
        )
    )
    await asyncio.sleep(0.05)
    batch = await source.read()
    previews = [fact for fact in batch.trajectory if fact.kind is TrajectoryKind.ASSISTANT]
    assert len(previews) == 1
    assert previews[0].status is TrajectoryStatus.RUNNING
    assert previews[0].native_id == "i-live"
    assert len(previews[0].summary) <= codex_runtime_module.CODEX_RUNTIME_DELTA_PREVIEW_MAX_CHARS
    # No further preview until new deltas arrive.
    quiet = await source.read()
    assert [fact for fact in quiet.trajectory if fact.kind is TrajectoryKind.ASSISTANT] == []
    await runtime.aclose()


async def test_native_cumulative_usage_never_becomes_response_usage() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    server.push(
        RuntimeNotification(
            method="thread/tokenUsage/updated",
            params={
                "threadId": "ui-thread-1",
                "turnId": "turn-1",
                "tokenUsage": {"last": {}, "total": {}},
            },
        )
    )
    server.push(completed_item("i-agent", item_type="agentMessage"))
    await asyncio.sleep(0.05)
    batch = await source.read()
    assert all(event.usage is None for event in batch.events)
    assert batch.terminal_evidence == ()
    await runtime.aclose()


async def test_tool_shaped_items_become_bounded_trajectory_facts() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    server.push(
        RuntimeNotification(
            method="item/completed",
            params={
                "threadId": "ui-thread-1",
                "turnId": "turn-1",
                "completedAtMs": 1234,
                "item": {
                    "id": "i-cmd",
                    "type": "commandExecution",
                    "command": "/bin/zsh -lc 'ls'",
                },
            },
        )
    )
    await asyncio.sleep(0.05)
    batch = await source.read()
    tool_facts = [fact for fact in batch.trajectory if fact.kind is TrajectoryKind.TOOL_CALL]
    assert len(tool_facts) == 1
    assert tool_facts[0].native_id == "i-cmd"
    assert tool_facts[0].status is TrajectoryStatus.COMPLETED
    await runtime.aclose()


async def test_event_buffer_overflow_degrades_health_visibly() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    for index in range(codex_runtime_module.CODEX_RUNTIME_EVENTS_BUFFER + 10):
        server.push(completed_item(f"i-{index}", item_type="agentMessage", text=f"m{index}"))
    await asyncio.sleep(0.2)
    await source.read()
    health = source.health_snapshot()[0]
    assert health.dropped > 0
    snapshot = await runtime.snapshot()
    assert snapshot.health is ConnectionHealth.DEGRADED
    assert snapshot.health_diagnostics
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Disconnect / reconnect and missed-completion recovery
# ---------------------------------------------------------------------------


async def test_aclose_disconnects_only_and_backend_survives() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    await runtime.aclose()
    assert server.close_count == 1
    assert server.backend_alive is True
    snapshot = await runtime.snapshot()
    assert snapshot.health is ConnectionHealth.DISCONNECTED
    # Terminal evidence already recorded is still readable after disconnect.
    assert runtime.live_source() is not None


async def test_reconnect_recovers_missed_first_completion() -> None:
    server = ScriptedCodexServer()
    # First runtime instance: NEW + send; the turn is accepted.
    first, _binding = await open_new(server)
    receipt = await first.send(operation_id="op-early", prompt="work")
    assert receipt.native_turn_id == "turn-1"
    # The control connection dies before turn/completed arrives (the scripted
    # server never delivers it).
    await first.aclose()
    # Daemon restart: a fresh runtime reconnects to the exact same thread.
    server.respond(
        "thread/resume",
        {
            "thread": {
                "id": "ui-thread-1",
                "status": {"type": "idle"},
                "turns": [
                    {
                        "id": "turn-1",
                        "status": "completed",
                        "error": None,
                        "items": [{"id": "i-agent", "type": "agentMessage", "text": "recovered"}],
                    }
                ],
            }
        },
    )
    second = make_runtime(server)
    binding = await second.open_session(
        mode=SessionOpenMode.RECONNECT, native_session_id="ui-thread-1"
    )
    assert binding.native_session_id == "ui-thread-1"
    batch = await second.live_source().read()
    assert len(batch.terminal_evidence) == 1
    outcome = batch.terminal_evidence[0]
    assert outcome.native_turn_id == "turn-1"
    assert outcome.terminal.value == "completed"
    assert outcome.result == "recovered"
    assert outcome.completeness.value == "partial"
    assert outcome.provenance.value == "native_evidence"
    await second.aclose()


async def test_reconcile_reports_active_turn_and_last_terminal_turn() -> None:
    server = ScriptedCodexServer()
    server.respond(
        "thread/resume",
        {
            "thread": {
                "id": "ui-thread-1",
                "status": {"type": "active"},
                "turns": [
                    {"id": "turn-old", "status": "completed", "items": []},
                    {"id": "turn-live", "status": "inProgress", "items": []},
                ],
            }
        },
    )
    runtime = make_runtime(server)
    await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="ui-thread-1")
    snapshot = await runtime.snapshot()
    assert snapshot.native_turn_id == "turn-live"
    batch = await runtime.live_source().read()
    # turn-old is outside the bounded reconcile window? It is the previous
    # turn of the active one, so its missed terminal state is recovered.
    assert [e.native_turn_id for e in batch.terminal_evidence] == ["turn-old"]
    await runtime.aclose()


async def test_reconnect_revalidates_handshake_subscription_and_evidence_once() -> None:
    server = ScriptedCodexServer()
    # The reconnect attaches to the exact native thread and the thread/resume
    # response carries the missed terminal turn: snapshot-derived exact
    # terminal evidence, buffered once, PARTIAL per the frozen contract.
    server.respond(
        "thread/resume",
        {
            "thread": {
                "id": "th-1",
                "status": {"type": "idle"},
                "turns": [
                    {
                        "id": "turn-1",
                        "status": "completed",
                        "error": None,
                        "itemsView": "summary",
                        "items": [
                            {"id": "i-agent", "type": "agentMessage", "text": "recovered final"},
                        ],
                    }
                ],
            }
        },
    )
    io = ScriptedCodexIO(server)
    runtime = make_runtime(server, io=io)
    binding = await runtime.open_session(mode=SessionOpenMode.RECONNECT, native_session_id="th-1")
    # Handshake dialect on the reconnecting connection: initialize, then
    # exactly one initialized notification — no protocol requests added.
    assert [method for method, _ in server.requests if method == "initialize"] == ["initialize"]
    assert server.notifications_sent == [("initialized", {})]
    # Exact thread/resume attach-and-subscribe for the exact session, with
    # the original participant/generation identity bound.
    assert server.requested("thread/resume") == [{"threadId": "th-1"}]
    assert binding.native_session_id == "th-1"
    assert binding.participant_id == PARTICIPANT
    assert binding.backend_generation == GENERATION
    source = runtime.live_source()
    (outcome,) = (await source.read()).terminal_evidence
    assert outcome.native_session_id == "th-1"
    assert outcome.native_turn_id == "turn-1"
    assert outcome.terminal is NativeTurnTerminal.COMPLETED
    assert outcome.result == "recovered final"
    assert outcome.completeness is ResultCompleteness.PARTIAL
    assert outcome.provenance is ResultProvenance.NATIVE_EVIDENCE
    # No duplicate on replay: the same terminal notification changes nothing.
    server.push(
        summary_turn_completed(
            thread_id="th-1",
            turn_id="turn-1",
            items_view="summary",
            items=[{"id": "i-agent", "type": "agentMessage", "text": "recovered final"}],
        )
    )
    await asyncio.sleep(0.05)
    replay = await source.read()
    assert replay.terminal_evidence == ()
    # The subscription survives the reconnect: a follow-up send never
    # re-subscribes (no second thread/resume).
    receipt = await runtime.send(operation_id="op-2", prompt="more work")
    assert receipt.result is DeliveryResult.ACCEPTED
    assert server.requested("thread/resume") == [{"threadId": "th-1"}]
    snapshot = await runtime.snapshot()
    assert snapshot.participant_id == PARTICIPANT
    assert snapshot.backend_generation == GENERATION
    assert snapshot.native_session_id == "th-1"
    assert snapshot.execution_state is RuntimeExecutionState.ACTIVE
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Plugin-confirmed execution state (RuntimeExecutionState producer)
# ---------------------------------------------------------------------------


def thread_status_changed(status: str, thread_id: str = "ui-thread-1") -> RuntimeNotification:
    return RuntimeNotification(
        method="thread/status/changed",
        params={"threadId": thread_id, "status": {"type": status}},
    )


async def test_execution_state_lifecycle_is_native_derived_only() -> None:
    server = ScriptedCodexServer()
    runtime = make_runtime(server)
    # Before any native session exists, nothing is confirmed: UNKNOWN is
    # never proof of idle.
    assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.UNKNOWN
    server.push(thread_started())
    await runtime.open_session(mode=SessionOpenMode.NEW)
    # The thread/started broadcast carried the backend's exact idle status
    # on a bound, connected session.
    assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.IDLE
    # thread/status/changed(active) with no turn/started is still the
    # backend's exact active fact: ACTIVE.
    server.push(thread_status_changed("active"))
    await asyncio.sleep(0.05)
    snapshot = await runtime.snapshot()
    assert snapshot.execution_state is RuntimeExecutionState.ACTIVE
    assert snapshot.native_turn_id is None
    server.push(thread_status_changed("idle"))
    await asyncio.sleep(0.05)
    assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.IDLE
    # turn/started is the native active fact as well.
    server.push(
        RuntimeNotification(
            method="turn/started",
            params={"threadId": "ui-thread-1", "turn": {"id": "turn-1"}},
        )
    )
    await asyncio.sleep(0.05)
    assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.ACTIVE
    # The turn completed; the thread-status broadcast has not arrived yet, so
    # the last native fact is still active — no heuristic idle.
    server.push(
        summary_turn_completed(
            turn_id="turn-1",
            items_view="full",
            items=[{"id": "i-agent", "type": "agentMessage", "text": "final answer"}],
        )
    )
    await asyncio.sleep(0.05)
    assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.ACTIVE
    # An unrecognized thread status is never idle: UNKNOWN.
    server.push(thread_status_changed("paused"))
    await asyncio.sleep(0.05)
    assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.UNKNOWN
    await runtime.aclose()


async def test_execution_state_idle_requires_bound_live_connection() -> None:
    server = ScriptedCodexServer()
    io = ScriptedCodexIO(server)
    runtime, _binding = await open_new(server, io=io)
    assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.IDLE
    # The notification stream ends: the exact idle status is no longer
    # confirmed on a live connection — UNKNOWN, never idle.
    assert io.connection is not None
    io.connection.closed = True
    for _ in range(400):
        if (await runtime.snapshot()).health is ConnectionHealth.DISCONNECTED:
            break
        await asyncio.sleep(0.005)
    snapshot = await runtime.snapshot()
    assert snapshot.health is ConnectionHealth.DISCONNECTED
    assert snapshot.execution_state is RuntimeExecutionState.UNKNOWN
    await runtime.aclose()


async def test_execution_state_active_is_still_known_after_disconnect() -> None:
    server = ScriptedCodexServer()
    io = ScriptedCodexIO(server)
    runtime, _binding = await open_new(server, io=io)
    server.push(
        RuntimeNotification(
            method="turn/started",
            params={"threadId": "ui-thread-1", "turn": {"id": "turn-1"}},
        )
    )
    await asyncio.sleep(0.05)
    assert (await runtime.snapshot()).execution_state is RuntimeExecutionState.ACTIVE
    # A dropped connection does not erase the still-known active turn: the
    # state stays ACTIVE across the disconnect; the daemon decides recovery.
    assert io.connection is not None
    io.connection.closed = True
    for _ in range(400):
        if (await runtime.snapshot()).health is ConnectionHealth.DISCONNECTED:
            break
        await asyncio.sleep(0.005)
    snapshot = await runtime.snapshot()
    assert snapshot.health is ConnectionHealth.DISCONNECTED
    assert snapshot.native_turn_id == "turn-1"
    assert snapshot.execution_state is RuntimeExecutionState.ACTIVE
    await runtime.aclose()


async def test_stream_end_marks_disconnected_wakes_readers_and_never_reconnects() -> None:
    server = ScriptedCodexServer()
    io = ScriptedCodexIO(server)
    runtime, _binding = await open_new(server, io=io)
    source = runtime.live_source()
    wakes: list[str] = []
    source.set_activity_callback(lambda: wakes.append("wake"))
    assert io.connection is not None
    io.connection.closed = True
    # The health transition itself wakes the observer state waiter so a
    # blocked reader sees the disconnect promptly.
    for _ in range(400):
        if (await runtime.snapshot()).health is ConnectionHealth.DISCONNECTED:
            break
        await asyncio.sleep(0.005)
    snapshot = await runtime.snapshot()
    assert snapshot.health is ConnectionHealth.DISCONNECTED
    assert snapshot.execution_state is RuntimeExecutionState.UNKNOWN
    assert wakes
    # No reconnect from the plugin: the same single connection, and no
    # second initialize handshake.
    assert server.connect_count == 1
    assert [method for method, _ in server.requests if method == "initialize"] == ["initialize"]
    await runtime.aclose()


async def test_buffer_overflow_degrade_wakes_readers() -> None:
    server = ScriptedCodexServer()
    runtime, _binding = await open_new(server)
    source = runtime.live_source()
    wakes: list[str] = []
    source.set_activity_callback(lambda: wakes.append("wake"))
    total = codex_runtime_module.CODEX_RUNTIME_EVENTS_BUFFER + 10
    for index in range(total):
        server.push(completed_item(f"i-{index}", item_type="agentMessage", text=f"m{index}"))
    await asyncio.sleep(0.2)
    await source.read()
    health = source.health_snapshot()[0]
    assert health.state is ChannelHealthState.DEGRADED
    assert health.dropped > 0
    # Every bounded-buffer push wakes the reader — the overflow transition
    # is visible state, not a silent stall.
    assert len(wakes) >= total
    await runtime.aclose()


# ---------------------------------------------------------------------------
# Factory and end-to-end smoke through the manifest
# ---------------------------------------------------------------------------


async def test_manifest_factory_creates_runtime_from_context() -> None:
    server = ScriptedCodexServer()
    context = RuntimeContext(
        participant_id=PARTICIPANT,
        cwd=CWD,
        io=ScriptedCodexIO(server),
        backend_generation=GENERATION,
        endpoint=ENDPOINT,
        approval="manual",
    )
    runtime = codex_runtime_factory(context)
    assert isinstance(runtime, CodexRuntime)
    server.push(thread_started())
    binding = await runtime.open_session(mode=SessionOpenMode.NEW)
    assert binding.participant_id == PARTICIPANT
    await runtime.aclose()


async def test_planning_matches_manifest_runtime_plan() -> None:
    plan = MANIFEST.runtime.plan(planning_context(approval="edits"))
    assert isinstance(plan, RuntimePlan)
    assert plan.backend.argv[-1] == f"unix://{ENDPOINT}"
    compatibility = MANIFEST.runtime.probe(RuntimeProbeContext(binary="/nonexistent"))
    assert isinstance(compatibility, RuntimeCompatibility)
    assert compatibility.supported is False


def test_batch_contract_still_accepts_plain_construction() -> None:
    # Existing event constructors remain valid: terminal_evidence is
    # default-empty.
    batch = Batch()
    assert batch.terminal_evidence == ()
