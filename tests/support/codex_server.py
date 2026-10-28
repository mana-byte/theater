"""A scripted Codex app-server and connection, with the notification builders tests feed it."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field

from theater.harness.builtin.plugins.codex.runtime import CodexRuntime
from theater.harness.builtin.plugins.codex.runtime_plan import (
    VERSION_POLICY,
)
from theater.harness.contracts.runtime import (
    RuntimeConnection,
    RuntimeConnectionError,
    RuntimeContext,
    RuntimeIO,
    RuntimeNotification,
    SessionOpenMode,
)

ENDPOINT = "/private/theater/var/runtime/codex-p1.sock"


PARTICIPANT = "codex-p1"


CWD = "/repo"


GENERATION = 7


# Release the scripted backend reports: always the qualified floor, never a capture of one.
SCRIPTED_CODEX_RELEASE = VERSION_POLICY.minimum


class RequestFailure(Exception):
    """Scripted failure for one request method."""


@dataclass
class ScriptedCodexServer:
    """Everything the scripted backend records and serves."""

    requests: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    notifications_sent: list[tuple[str, dict[str, object]]] = field(default_factory=list)
    pending: list[RuntimeNotification] = field(default_factory=list)
    responses: dict[str, object] = field(default_factory=dict)
    #: Per-request scripted response selection for pagination and other
    #: parameter-sensitive protocol paths.
    response_handlers: dict[str, Callable[[Mapping[str, object]], object]] = field(
        default_factory=dict
    )
    failures: dict[str, Exception] = field(default_factory=dict)
    #: A gate is observed only after the request has been physically recorded,
    #: which makes post-write cancellation regressions deterministic.
    request_gates: dict[str, asyncio.Event] = field(default_factory=dict)
    request_entered: dict[str, asyncio.Event] = field(default_factory=dict)
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
                "userAgent": (
                    f"theater/{SCRIPTED_CODEX_RELEASE} (macos; arm64) xterm (theater; 1.0)"
                ),
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
        if method == "thread/turns/list":
            return {"data": [], "nextCursor": None}
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
        self._server.request_entered.setdefault(method, asyncio.Event()).set()
        gate = self._server.request_gates.get(method)
        if gate is not None:
            await gate.wait()
        failure = self._server.failures.get(method)
        if failure is not None:
            raise failure
        handler = self._server.response_handlers.get(method)
        result = handler(params) if handler is not None else self._server.responses.get(method)
        if result is None:
            result = self._server.default_response(method)
        if isinstance(result, Mapping):
            result = dict(result)
            if (
                method == "thread/resume"
                and params.get("excludeTurns") is True
                and "initialTurnsPage" in params
                and "initialTurnsPage" not in result
                and isinstance(result.get("thread"), Mapping)
            ):
                # Mirror the stock CLI: full turns and initialTurnsPage are separate fields.
                thread = dict(result["thread"])
                turns = thread.get("turns", [])
                page = list(reversed(turns[-2:])) if isinstance(turns, list) else []
                result["initialTurnsPage"] = {"data": page, "nextCursor": None}
                thread["turns"] = []
                result["thread"] = thread
            return result
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
    approval: str = "manual",
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> CodexRuntime:
    context = RuntimeContext(
        participant_id=PARTICIPANT,
        cwd=cwd,
        io=io if io is not None else ScriptedCodexIO(server),
        backend_generation=GENERATION,
        endpoint=ENDPOINT,
        approval=approval,
        model=model,
        reasoning_effort=reasoning_effort,
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
                "cliVersion": SCRIPTED_CODEX_RELEASE,
                "status": {"type": status},
            }
        },
    )


async def open_new(server: ScriptedCodexServer, **kwargs) -> tuple[CodexRuntime, object]:
    runtime = make_runtime(server, **kwargs)
    server.push(thread_started())
    binding = await runtime.open_session(mode=SessionOpenMode.NEW)
    return runtime, binding


def summary_turn_completed(
    *,
    thread_id: str = "ui-thread-1",
    turn_id: str = "turn-sum",
    status: str = "completed",
    items_view: object = None,
    items: list[dict[str, object]] | None = None,
    error: object = None,
) -> RuntimeNotification:
    """One turn/completed with the captured stock release's item view."""
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


def thread_status_changed(status: str, thread_id: str = "ui-thread-1") -> RuntimeNotification:
    return RuntimeNotification(
        method="thread/status/changed",
        params={"threadId": thread_id, "status": {"type": status}},
    )
