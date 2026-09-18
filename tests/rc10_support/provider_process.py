"""Run a non-tmux RC10 terminal-provider fixture against an explicit public socket.

The CLI accepts ``--socket``, durable provider identity, and an optional JSON
``--plan`` so an integration gate can point this process at a candidate daemon.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from theater.frontend.provider import CallbackRequest, CallbackResponse, ProviderClient

_CALLBACK_METHODS = frozenset(
    {
        "terminal.create",
        "terminal.inventory",
        "terminal.inspect",
        "terminal.deliver",
        "terminal.interrupt",
        "terminal.terminate",
    }
)
_CONTROL_ENVIRONMENT = frozenset(
    {
        "TMUX",
        "TMUX_PANE",
        "TMUX_TMPDIR",
        "PYTHONHOME",
        "PYTHONPATH",
        "THEATER_ID",
        "CLAUDE_CODE_MESSAGING_SOCKET",
        "CLAUDE_CODE_MESSAGING_TOKEN",
        "OPENCODE_CONFIG",
        "OPENCODE_DB",
        "OPENCODE_SERVER_PASSWORD",
        "OPENCODE_TUI_CONFIG",
        "VIBE_MCP_SERVERS",
    }
)
_STAND_IN_PROGRAM = (
    "import signal,time; signal.signal(signal.SIGINT, lambda *_: None); time.sleep(3600)"
)
_MAX_FIXTURE_ROOT_BYTES = 80


@dataclass(frozen=True, slots=True)
class FixturePlan:
    """Deterministic controls for one fixture-provider process."""

    terminal_id: str = "fixture-terminal"
    occupant_id: str = "fixture-occupant"
    bind_requested_participant: bool = False
    reuse_terminal_id: bool = False
    inventory_complete: bool = True
    reply_delays: Mapping[str, float] = field(default_factory=dict)
    disconnect_before: frozenset[str] = frozenset()
    disconnect_after: frozenset[str] = frozenset()
    replace_occupant_after: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_path(cls, path: Path | None) -> FixturePlan:
        """Load one deliberately small, validated failure-injection plan."""
        if path is None:
            return cls()
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"could not read fixture plan {path}: {exc}") from exc
        if not isinstance(loaded, Mapping):
            raise TypeError("fixture plan must be a JSON object")
        allowed = {
            "terminal_id",
            "occupant_id",
            "bind_requested_participant",
            "reuse_terminal_id",
            "inventory_complete",
            "reply_delays",
            "disconnect_before",
            "disconnect_after",
            "replace_occupant_after",
        }
        unexpected = sorted(set(loaded) - allowed)
        if unexpected:
            raise ValueError(f"fixture plan has unknown fields: {', '.join(unexpected)}")
        defaults = cls()
        terminal_id = _identifier(loaded.get("terminal_id", defaults.terminal_id), "terminal_id")
        occupant_id = _identifier(loaded.get("occupant_id", defaults.occupant_id), "occupant_id")
        reuse_terminal_id = loaded.get("reuse_terminal_id", defaults.reuse_terminal_id)
        if type(reuse_terminal_id) is not bool:
            raise ValueError("fixture plan reuse_terminal_id must be a boolean")
        bind_requested_participant = loaded.get(
            "bind_requested_participant", defaults.bind_requested_participant
        )
        if type(bind_requested_participant) is not bool:
            raise ValueError("fixture plan bind_requested_participant must be a boolean")
        inventory_complete = loaded.get("inventory_complete", defaults.inventory_complete)
        if type(inventory_complete) is not bool:
            raise ValueError("fixture plan inventory_complete must be a boolean")
        return cls(
            terminal_id=terminal_id,
            occupant_id=occupant_id,
            bind_requested_participant=bind_requested_participant,
            reuse_terminal_id=reuse_terminal_id,
            inventory_complete=inventory_complete,
            reply_delays=_method_seconds(loaded.get("reply_delays", {}), "reply_delays"),
            disconnect_before=_methods(loaded.get("disconnect_before", []), "disconnect_before"),
            disconnect_after=_methods(loaded.get("disconnect_after", []), "disconnect_after"),
            replace_occupant_after=_method_identifiers(
                loaded.get("replace_occupant_after", {}), "replace_occupant_after"
            ),
        )


@dataclass(frozen=True, slots=True)
class ProviderFixtureConfig:
    """Explicit public-connection and local-fixture facts."""

    socket_path: Path
    root: Path
    ready_file: Path
    evidence_file: Path
    client_id: str
    provider_id: str
    provider_credential: str = field(repr=False)
    callback_timeout: float = 30.0
    handshake_timeout: float = 10.0
    plan: FixturePlan = field(default_factory=FixturePlan)


@dataclass(slots=True)
class _TerminalStandIn:
    terminal_id: str
    incarnation: str
    occupant_id: str
    launch_id: str
    cwd: str
    process: subprocess.Popen[bytes]
    started_at: float
    alive: bool = True

    def identity(self, *, provider_id: str, provider_generation: int) -> dict[str, object]:
        return {
            "provider_id": provider_id,
            "provider_generation": provider_generation,
            "terminal_id": self.terminal_id,
            "terminal_incarnation": self.incarnation,
            "occupant": {
                "id": self.occupant_id,
                "occupant_id": self.occupant_id,
                "kind": "fixture",
                "harness": "codex",
                "cwd": self.cwd,
            },
            "process": {
                "pid": self.process.pid,
                "started_at": self.started_at,
                "executable": sys.executable,
            },
            "launch_id": self.launch_id,
        }


class _EvidenceLog:
    """Append fsync'd fixture evidence independently of callback acknowledgements."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._sequence = 0

    def append(self, event: str, **facts: object) -> dict[str, object]:
        self._sequence += 1
        entry: dict[str, object] = {
            "sequence": self._sequence,
            "event": event,
            "at": time.time(),
            **facts,
        }
        encoded = json.dumps(entry, allow_nan=False, separators=(",", ":"), sort_keys=True)
        with self._path.open("a", encoding="utf-8") as output:
            output.write(encoded + "\n")
            output.flush()
            os.fsync(output.fileno())
        return entry


class ProviderFixture:
    """A public callback provider backed by harmless fixture-owned child processes."""

    def __init__(self, config: ProviderFixtureConfig) -> None:
        self._config = config
        self._evidence = _EvidenceLog(config.evidence_file)
        self._terminals: dict[str, _TerminalStandIn] = {}
        self._operation_results: dict[tuple[str, str], dict[str, object]] = {}
        self._consumed_controls: set[tuple[str, str]] = set()
        self._report_revision = 0
        self._presence_revision = 0
        self._incarnation = 0
        self._client = ProviderClient(
            config.socket_path,
            client_id=config.client_id,
            provider_id=config.provider_id,
            provider_credential=config.provider_credential,
            handlers={
                "terminal.create": self._create,
                "terminal.inventory": self._inventory,
                "terminal.inspect": self._inspect,
                "terminal.deliver": self._deliver,
                "terminal.interrupt": self._interrupt,
                "terminal.terminate": self._terminate,
            },
            handshake_timeout=config.handshake_timeout,
            callback_timeout=config.callback_timeout,
        )

    @property
    def client(self) -> ProviderClient:
        """Expose the public client solely for the process lifecycle runner."""
        return self._client

    async def connect(self) -> int:
        """Connect only to the supplied Unix socket and return its acquired generation."""
        result = await self._client.connect()
        generation = result.provider_generation
        if type(generation) is not int:
            raise RuntimeError("provider callback handshake did not acquire a generation")
        self._evidence.append(
            "connected",
            provider_id=self._config.provider_id,
            provider_generation=generation,
        )
        return generation

    async def close(self) -> None:
        """Bound fixture shutdown without touching any non-fixture process."""
        await self._client.close()
        for terminal in tuple(self._terminals.values()):
            if terminal.alive:
                self._stop_stand_in(terminal)
        self._evidence.append("stopped", provider_id=self._config.provider_id)

    async def _create(self, request: CallbackRequest) -> Mapping[str, object]:
        before = await self._before_side_effect(request)
        if before is not None:
            return before
        cached = self._cached_result(request)
        if cached is not None:
            return cached
        launch_id = request.params.get("launch_id")
        assert isinstance(launch_id, str)
        terminal_id = self._terminal_id_for(launch_id)
        existing = self._terminals.get(terminal_id)
        if existing is not None and existing.alive:
            return self._create_rejected(
                request, "terminal_busy", "The fixture terminal is occupied."
            )
        self._incarnation += 1
        participant_id = request.params.get("participant_id")
        launch = request.params.get("launch")
        assert isinstance(participant_id, str) and isinstance(launch, Mapping)
        cwd = launch.get("cwd")
        assert isinstance(cwd, str)
        occupant_id = (
            participant_id
            if self._config.plan.bind_requested_participant
            else self._config.plan.occupant_id
        )
        terminal = self._start_stand_in(
            terminal_id,
            launch_id=launch_id,
            occupant_id=occupant_id,
            cwd=cwd,
        )
        self._terminals[terminal.terminal_id] = terminal
        result = {
            "operation_id": _operation_id(request),
            "provider_generation": request.provider_generation,
            "outcome": "accepted",
            "terminal": terminal.identity(
                provider_id=self._config.provider_id,
                provider_generation=request.provider_generation,
            ),
        }
        self._record_execution(request, terminal, result)
        return await self._after_side_effect(request, result)

    async def _inventory(self, request: CallbackRequest) -> Mapping[str, object]:
        self._report_revision += 1
        terminals = [
            terminal.identity(
                provider_id=self._config.provider_id,
                provider_generation=request.provider_generation,
            )
            for terminal in self._terminals.values()
            if terminal.alive
        ]
        result: dict[str, object] = {
            "provider_generation": request.provider_generation,
            "report_revision": self._report_revision,
            "complete": self._config.plan.inventory_complete,
            "terminals": terminals,
        }
        if not self._config.plan.inventory_complete:
            result["next_cursor"] = f"fixture-report-{self._report_revision}"
        return await self._delayed(request.method, result)

    async def _inspect(self, request: CallbackRequest) -> Mapping[str, object] | CallbackResponse:
        terminal = self._target(request)
        if terminal is None:
            return CallbackResponse(
                error={
                    "code": "stale_terminal",
                    "message": "The fixture terminal is absent.",
                }
            )
        self._report_revision += 1
        self._presence_revision += 1
        result: dict[str, object] = {
            "provider_generation": request.provider_generation,
            "report_revision": self._report_revision,
            "terminal": terminal.identity(
                provider_id=self._config.provider_id,
                provider_generation=request.provider_generation,
            ),
            "presence": {"state": "absent", "revision": self._presence_revision},
            "mode": "fixture",
            "screen": None,
            "lifecycle": {"alive": terminal.alive},
        }
        self._replace_occupant_if_planned(request.method, terminal)
        return await self._delayed(request.method, result)

    async def _deliver(self, request: CallbackRequest) -> Mapping[str, object]:
        return await self._mutate_terminal(request, "deliver")

    async def _interrupt(self, request: CallbackRequest) -> Mapping[str, object]:
        return await self._mutate_terminal(request, "interrupt")

    async def _terminate(self, request: CallbackRequest) -> Mapping[str, object]:
        return await self._mutate_terminal(request, "terminate")

    async def _mutate_terminal(self, request: CallbackRequest, action: str) -> Mapping[str, object]:
        before = await self._before_side_effect(request)
        if before is not None:
            return before
        cached = self._cached_result(request)
        if cached is not None:
            return cached
        terminal = self._target(request)
        if terminal is None:
            return self._rejected_delivery(
                request, "stale_terminal", "The fixture terminal is absent."
            )
        self._presence_revision += 1
        if action == "interrupt":
            terminal.process.send_signal(signal.SIGINT)
        elif action == "terminate":
            self._stop_stand_in(terminal)
        result = self._delivery_result(request, terminal, "accepted")
        if action == "terminate":
            result["exit_confirmed"] = not terminal.alive
        self._record_execution(request, terminal, result)
        self._replace_occupant_if_planned(request.method, terminal)
        return await self._after_side_effect(request, result)

    async def _before_side_effect(self, request: CallbackRequest) -> Mapping[str, object] | None:
        if not self._consume_control("disconnect_before", request.method):
            return None
        self._evidence.append(
            "disconnect_before_side_effect",
            method=request.method,
            operation_id=_operation_id(request),
        )
        await self._client.close()
        return self._unknown_result(request)

    async def _after_side_effect(
        self, request: CallbackRequest, result: dict[str, object]
    ) -> Mapping[str, object]:
        if self._consume_control("disconnect_after", request.method):
            self._evidence.append(
                "disconnect_after_side_effect",
                method=request.method,
                operation_id=_operation_id(request),
            )
            await self._client.close()
            return result
        return await self._delayed(request.method, result)

    async def _delayed(self, method: str, result: Mapping[str, object]) -> Mapping[str, object]:
        delay = self._config.plan.reply_delays.get(method, 0.0)
        if delay:
            await asyncio.sleep(delay)
        return result

    def _cached_result(self, request: CallbackRequest) -> dict[str, object] | None:
        key = (request.method, _operation_id(request))
        cached = self._operation_results.get(key)
        if cached is None:
            return None
        self._evidence.append(
            "execution_replayed",
            method=request.method,
            operation_id=_operation_id(request),
        )
        return dict(cached)

    def _record_execution(
        self,
        request: CallbackRequest,
        terminal: _TerminalStandIn,
        result: dict[str, object],
    ) -> None:
        operation_id = _operation_id(request)
        self._operation_results[(request.method, operation_id)] = dict(result)
        identity = terminal.identity(
            provider_id=self._config.provider_id,
            provider_generation=request.provider_generation,
        )
        receipt = self._receipt_path(request.method, operation_id)
        _write_json(
            receipt,
            {"method": request.method, "operation_id": operation_id, "result": result},
        )
        self._evidence.append(
            "physical_side_effect",
            method=request.method,
            operation_id=operation_id,
            terminal=identity,
            receipt=str(receipt),
        )

    def _start_stand_in(
        self, terminal_id: str, *, launch_id: str, occupant_id: str, cwd: str
    ) -> _TerminalStandIn:
        environment = isolated_environment(os.environ, self._config.root)
        process = subprocess.Popen(
            [sys.executable, "-c", _STAND_IN_PROGRAM],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            start_new_session=True,
        )
        return _TerminalStandIn(
            terminal_id=terminal_id,
            incarnation=f"fixture-incarnation-{self._incarnation}",
            occupant_id=occupant_id,
            launch_id=launch_id,
            cwd=cwd,
            process=process,
            started_at=time.time(),
        )

    def _stop_stand_in(self, terminal: _TerminalStandIn) -> None:
        if terminal.process.poll() is None:
            terminal.process.terminate()
            try:
                terminal.process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                terminal.process.kill()
                terminal.process.wait(timeout=0.5)
        terminal.alive = False
        self._evidence.append(
            "stand_in_stopped",
            terminal_id=terminal.terminal_id,
            terminal_incarnation=terminal.incarnation,
            pid=terminal.process.pid,
        )

    def _target(self, request: CallbackRequest) -> _TerminalStandIn | None:
        params = request.params
        terminal_id = params.get("terminal_id")
        terminal_incarnation = params.get("terminal_incarnation")
        expected_occupant = params.get("expected_occupant")
        if not isinstance(terminal_id, str) or not isinstance(terminal_incarnation, str):
            return None
        if request.method != "terminal.inspect" and not isinstance(expected_occupant, str):
            return None
        terminal = self._terminals.get(terminal_id)
        if (
            terminal is None
            or not terminal.alive
            or terminal.incarnation != terminal_incarnation
            or (request.method != "terminal.inspect" and terminal.occupant_id != expected_occupant)
        ):
            return None
        return terminal

    def _delivery_result(
        self, request: CallbackRequest, terminal: _TerminalStandIn, delivery: str
    ) -> dict[str, object]:
        result: dict[str, object] = {
            "operation_id": _operation_id(request),
            "provider_generation": request.provider_generation,
            "terminal_id": terminal.terminal_id,
            "terminal_incarnation": terminal.incarnation,
            "delivery": delivery,
            "presence_revision": self._presence_revision,
        }
        return result

    def _rejected_delivery(
        self, request: CallbackRequest, code: str, message: str
    ) -> dict[str, object]:
        params = request.params
        terminal_id = params.get("terminal_id")
        terminal_incarnation = params.get("terminal_incarnation")
        assert isinstance(terminal_id, str)
        assert isinstance(terminal_incarnation, str)
        result: dict[str, object] = {
            "operation_id": _operation_id(request),
            "provider_generation": request.provider_generation,
            "terminal_id": terminal_id,
            "terminal_incarnation": terminal_incarnation,
            "delivery": "rejected",
            "presence_revision": self._presence_revision,
            "error": {"code": code, "message": message},
        }
        if request.method == "terminal.terminate":
            result["exit_confirmed"] = False
        return result

    def _create_rejected(
        self, request: CallbackRequest, code: str, message: str
    ) -> dict[str, object]:
        return {
            "operation_id": _operation_id(request),
            "provider_generation": request.provider_generation,
            "outcome": "rejected",
            "terminal": None,
            "error": {"code": code, "message": message},
        }

    def _unknown_result(self, request: CallbackRequest) -> dict[str, object]:
        if request.method == "terminal.create":
            return {
                "operation_id": _operation_id(request),
                "provider_generation": request.provider_generation,
                "outcome": "unknown",
                "terminal": None,
                "error": {
                    "code": "fixture_disconnect",
                    "message": "The fixture closed before starting the side effect.",
                },
            }
        params = request.params
        terminal_id = params.get("terminal_id")
        terminal_incarnation = params.get("terminal_incarnation")
        assert isinstance(terminal_id, str)
        assert isinstance(terminal_incarnation, str)
        result: dict[str, object] = {
            "operation_id": _operation_id(request),
            "provider_generation": request.provider_generation,
            "terminal_id": terminal_id,
            "terminal_incarnation": terminal_incarnation,
            "delivery": "unknown",
            "presence_revision": self._presence_revision,
            "error": {
                "code": "fixture_disconnect",
                "message": "The fixture closed before starting the side effect.",
            },
        }
        if request.method == "terminal.terminate":
            result["exit_confirmed"] = False
        return result

    def _replace_occupant_if_planned(self, method: str, terminal: _TerminalStandIn) -> None:
        replacement = self._config.plan.replace_occupant_after.get(method)
        if replacement is None or not self._consume_control("replace_occupant_after", method):
            return
        previous = terminal.occupant_id
        terminal.occupant_id = replacement
        self._evidence.append(
            "occupant_replaced",
            terminal_id=terminal.terminal_id,
            terminal_incarnation=terminal.incarnation,
            previous_occupant=previous,
            occupant=replacement,
        )

    def _consume_control(self, name: str, method: str) -> bool:
        if name == "disconnect_before":
            enabled = method in self._config.plan.disconnect_before
        elif name == "disconnect_after":
            enabled = method in self._config.plan.disconnect_after
        else:
            enabled = method in self._config.plan.replace_occupant_after
        key = (name, method)
        if not enabled or key in self._consumed_controls:
            return False
        self._consumed_controls.add(key)
        return True

    def _receipt_path(self, method: str, operation_id: str) -> Path:
        digest = hashlib.sha256(f"{method}\0{operation_id}".encode()).hexdigest()
        path = self._config.root / "receipts" / f"{digest}.json"
        path.parent.mkdir(mode=0o700, exist_ok=True)
        return path

    def _terminal_id_for(self, launch_id: str) -> str:
        if self._config.plan.reuse_terminal_id:
            return self._config.plan.terminal_id
        digest = hashlib.sha256(launch_id.encode()).hexdigest()[:16]
        return f"fixture-terminal-{digest}"


def isolated_environment(base: Mapping[str, str], root: Path) -> dict[str, str]:
    """Return a child-only environment with control routing and credentials removed."""
    environment = {
        name: value
        for name, value in base.items()
        if name not in _CONTROL_ENVIRONMENT
        and not name.startswith("THEATER_")
        and not name.startswith("CLAUDE_CODE_MESSAGING_")
    }
    theater_home = root / "theater"
    tmux_root = root / "tmux"
    theater_home.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmux_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    environment["THEATER_HOME"] = str(theater_home)
    environment["TMUX_TMPDIR"] = str(tmux_root)
    return environment


async def run_fixture(config: ProviderFixtureConfig, stop: asyncio.Event) -> int:
    """Run one provider until its explicit callback connection or process stop ends."""
    fixture = ProviderFixture(config)
    try:
        generation = await fixture.connect()
        _write_json(
            config.ready_file,
            {
                "pid": os.getpid(),
                "provider_id": config.provider_id,
                "provider_generation": generation,
                "evidence_file": str(config.evidence_file),
                "theater_home": os.environ.get("THEATER_HOME"),
                "tmux_tmpdir": os.environ.get("TMUX_TMPDIR"),
                "control_environment_clean": _control_environment_is_clean(),
            },
        )
        stop_task = asyncio.create_task(stop.wait())
        closed_task = asyncio.create_task(fixture.client.wait_closed())
        done, pending = await asyncio.wait(
            {stop_task, closed_task}, return_when=asyncio.FIRST_COMPLETED
        )
        del done
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    except Exception as exc:
        fixture._evidence.append("failure", error=repr(exc))
        return 2
    else:
        return 0
    finally:
        await fixture.close()


def parser() -> argparse.ArgumentParser:
    """Build the integration-facing fixture CLI without a default daemon path."""
    result = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "The optional plan is a JSON object with inventory_complete, reply_delays, "
            "disconnect_before, disconnect_after, replace_occupant_after, and "
            "reuse_terminal_id controls."
        ),
    )
    result.add_argument("--socket", type=_absolute_path, required=True, metavar="PATH")
    result.add_argument("--root", type=_absolute_path, required=True, metavar="PATH")
    result.add_argument("--provider-id", required=True)
    result.add_argument("--provider-credential", required=True)
    result.add_argument("--client-id", default="fixture-provider-client")
    result.add_argument("--ready-file", type=_absolute_path, metavar="PATH")
    result.add_argument("--evidence-file", type=_absolute_path, metavar="PATH")
    result.add_argument("--plan", type=_absolute_path, metavar="PATH")
    result.add_argument("--callback-timeout", type=_positive_seconds, default=30.0)
    result.add_argument("--handshake-timeout", type=_positive_seconds, default=10.0)
    return result


def main(argv: Sequence[str] | None = None) -> int:
    """Run the fixture process and clean only its own stand-in children on exit."""
    arguments = parser().parse_args(argv)
    root = arguments.root
    if root.is_symlink():
        raise ValueError(f"fixture root must not be a symlink: {root}")
    resolved_root = root.resolve()
    if not resolved_root.is_relative_to(Path("/tmp").resolve()):
        raise ValueError(f"fixture root must be beneath /tmp: {root}")
    if len(os.fsencode(resolved_root)) > _MAX_FIXTURE_ROOT_BYTES:
        raise ValueError(f"fixture root is too long for Unix-socket-safe test paths: {root}")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    environment = isolated_environment(os.environ, root)
    os.environ.clear()
    os.environ.update(environment)
    plan = FixturePlan.from_path(arguments.plan)
    config = ProviderFixtureConfig(
        socket_path=arguments.socket,
        root=root,
        ready_file=arguments.ready_file or root / "ready.json",
        evidence_file=arguments.evidence_file or root / "evidence.jsonl",
        client_id=_identifier(arguments.client_id, "client_id"),
        provider_id=_identifier(arguments.provider_id, "provider_id"),
        provider_credential=_identifier(arguments.provider_credential, "provider_credential"),
        callback_timeout=arguments.callback_timeout,
        handshake_timeout=arguments.handshake_timeout,
        plan=plan,
    )
    stop = asyncio.Event()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    for received in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(received, stop.set)
    try:
        return loop.run_until_complete(run_fixture(config, stop))
    finally:
        loop.close()


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise ValueError(f"{name} must be a non-empty string no longer than 512 characters")
    return value


def _positive_seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a finite positive number of seconds") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number of seconds")
    return seconds


def _absolute_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise argparse.ArgumentTypeError(f"must be an absolute path: {value}")
    return path


def _method_seconds(value: object, name: str) -> Mapping[str, float]:
    if not isinstance(value, Mapping):
        raise TypeError(f"fixture plan {name} must be an object")
    parsed: dict[str, float] = {}
    for method, seconds in value.items():
        _known_method(method, name)
        if (
            not isinstance(seconds, (int, float))
            or isinstance(seconds, bool)
            or not math.isfinite(float(seconds))
            or seconds < 0
        ):
            raise ValueError(f"fixture plan {name}.{method} must be a finite non-negative number")
        parsed[method] = float(seconds)
    return MappingProxyType(parsed)


def _methods(value: object, name: str) -> frozenset[str]:
    if not isinstance(value, list):
        raise TypeError(f"fixture plan {name} must be an array")
    parsed: set[str] = set()
    for method in value:
        _known_method(method, name)
        parsed.add(method)
    return frozenset(parsed)


def _method_identifiers(value: object, name: str) -> Mapping[str, str]:
    if not isinstance(value, Mapping):
        raise TypeError(f"fixture plan {name} must be an object")
    parsed: dict[str, str] = {}
    for method, identifier in value.items():
        _known_method(method, name)
        parsed[method] = _identifier(identifier, f"fixture plan {name}.{method}")
    return MappingProxyType(parsed)


def _known_method(value: object, name: str) -> str:
    if not isinstance(value, str) or value not in _CALLBACK_METHODS:
        raise ValueError(f"fixture plan {name} contains an unknown callback method: {value!r}")
    return value


def _operation_id(request: CallbackRequest) -> str:
    operation_id = request.params.get("operation_id")
    assert isinstance(operation_id, str)
    return operation_id


def _control_environment_is_clean() -> bool:
    if any(name in os.environ for name in _CONTROL_ENVIRONMENT - {"TMUX_TMPDIR"}):
        return False
    return all(name == "THEATER_HOME" for name in os.environ if name.startswith("THEATER_"))


def _write_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    encoded = json.dumps(dict(value), allow_nan=False, separators=(",", ":"), sort_keys=True)
    with temporary.open("w", encoding="utf-8") as output:
        output.write(encoded)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


if __name__ == "__main__":
    raise SystemExit(main())
