"""Black-box checks for the standalone RC10 non-tmux provider fixture process."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from theater import paths, protocol
from theater.daemon.plugins.credentials import credential_verifier
from theater.frontend import FrontendClient
from theater.frontend.schemas import validate_callback_response, validate_public_request

PROCESS = Path(__file__).parent / "rc10_support" / "provider_process.py"
REPOSITORY = Path(__file__).parents[1]
_LIMITS = {
    "max_frame_bytes": 67_108_864,
    "provider_pending_callbacks": 32,
    "provider_callback_timeout_seconds": 30,
    "provider_lease_seconds": 30,
    "provider_mutations_per_terminal": 1,
}


@dataclass
class _CallbackSession:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter

    async def send(self, frame: Mapping[str, object]) -> None:
        self.writer.write(json.dumps(dict(frame), separators=(",", ":")).encode() + b"\n")
        await self.writer.drain()

    async def read(self, method: str, *, timeout: float = 1) -> dict[str, object]:
        raw = await asyncio.wait_for(self.reader.readline(), timeout=timeout)
        assert raw.endswith(b"\n")
        frame = json.loads(raw)
        assert isinstance(frame, dict)
        validate_callback_response(method, frame)
        return frame


class _CallbackPeer:
    def __init__(self, socket_path: Path, sessions: asyncio.Queue[_CallbackSession]) -> None:
        self.socket_path = socket_path
        self._sessions = sessions

    async def next_session(self) -> _CallbackSession:
        return await asyncio.wait_for(self._sessions.get(), timeout=1)


@asynccontextmanager
async def _callback_peer(socket_path: Path) -> AsyncIterator[_CallbackPeer]:
    sessions: asyncio.Queue[_CallbackSession] = asyncio.Queue()
    stop = asyncio.Event()
    writers: set[asyncio.StreamWriter] = set()
    tasks: set[asyncio.Task[None]] = set()
    failures: list[BaseException] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        tasks.add(task)
        writers.add(writer)
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=1)
            request = json.loads(raw)
            validate_public_request(request)
            assert request == {
                "id": 1,
                "method": "frontend.handshake",
                "params": {
                    "api": {"major": 1, "minor": 0},
                    "client_id": "fixture-client",
                    "role": "provider",
                    "channel": "callback",
                    "required_capabilities": ["terminal-provider.v1"],
                    "provider_id": "fixture-provider",
                    "provider_credential": "fixture-credential",
                },
            }
            response = {
                "id": 1,
                "ok": True,
                "result": {
                    "api": {"major": 1, "minor": 0},
                    "daemon_instance_id": "fixture-daemon",
                    "package_version": "1.0.0rc10",
                    "capabilities": ["terminal-provider.v1"],
                    "limits": _LIMITS,
                    "provider_generation": 7,
                },
            }
            writer.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
            await writer.drain()
            await sessions.put(_CallbackSession(reader, writer))
            await stop.wait()
        except BaseException as exc:
            failures.append(exc)
        finally:
            tasks.discard(task)
            writers.discard(writer)
            writer.close()
            with contextlib.suppress(OSError, ConnectionError):
                await writer.wait_closed()

    server = await asyncio.start_unix_server(serve, path=str(socket_path))
    try:
        yield _CallbackPeer(socket_path, sessions)
    finally:
        stop.set()
        server.close()
        await server.wait_closed()
        for writer in tuple(writers):
            writer.close()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=1)
        socket_path.unlink(missing_ok=True)
        if failures:
            raise failures[0]


@dataclass
class _ProviderProcess:
    process: asyncio.subprocess.Process
    root: Path
    ready: dict[str, object]

    @property
    def evidence_path(self) -> Path:
        value = self.ready["evidence_file"]
        assert isinstance(value, str)
        return Path(value)

    async def wait(self, *, timeout: float = 1) -> int:
        return await asyncio.wait_for(self.process.wait(), timeout=timeout)

    async def stop(self) -> None:
        if self.process.returncode is None:
            self.process.send_signal(signal.SIGTERM)
        returncode = await self.wait(timeout=2)
        stdout, stderr = await self.process.communicate()
        assert returncode == 0, (stdout.decode(), stderr.decode())

    async def wait_for_effects(self, *operation_ids: str) -> None:
        async with asyncio.timeout(2):
            while not set(operation_ids).issubset(
                event["operation_id"] for event in _physical_events(self)
            ):
                await asyncio.sleep(0.005)

    def evidence(self) -> list[dict[str, object]]:
        if not self.evidence_path.exists():
            return []
        return [
            value
            for line in self.evidence_path.read_text(encoding="utf-8").splitlines()
            if isinstance(value := json.loads(line), dict)
        ]


@asynccontextmanager
async def _provider_process(
    socket_path: Path,
    *,
    plan: Mapping[str, object] | None = None,
    callback_timeout: float = 30.0,
    provider_id: str = "fixture-provider",
    provider_credential: str = "fixture-credential",
) -> AsyncIterator[_ProviderProcess]:
    root = Path(tempfile.mkdtemp(prefix="r10pp-", dir="/tmp"))
    ready_file = root / "ready.json"
    plan_path = root / "plan.json"
    if plan is not None:
        plan_path.write_text(json.dumps(dict(plan)), encoding="utf-8")
    inherited = {
        **os.environ,
        "THEATER_HOME": "/control/theater",
        "THEATER_ID": "control-participant",
        "THEATER_RUNTIME_TOKEN": "control-token",
        "TMUX": "/private/tmp/tmux-501/default,123,0",
        "TMUX_PANE": "%99",
        "TMUX_TMPDIR": "/private/tmp",
        "CLAUDE_CODE_MESSAGING_SOCKET": "/control/claude.sock",
        "CLAUDE_CODE_MESSAGING_TOKEN": "control-token",
    }
    inherited.pop("PYTHONHOME", None)
    inherited.pop("PYTHONPATH", None)
    command = [
        sys.executable,
        str(PROCESS),
        "--socket",
        str(socket_path),
        "--root",
        str(root),
        "--ready-file",
        str(ready_file),
        "--provider-id",
        provider_id,
        "--provider-credential",
        provider_credential,
        "--client-id",
        "fixture-client",
        "--callback-timeout",
        str(callback_timeout),
    ]
    if plan is not None:
        command.extend(("--plan", str(plan_path)))
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=REPOSITORY,
        env=inherited,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    provider: _ProviderProcess | None = None
    try:
        ready = await _read_ready(process, ready_file)
        provider = _ProviderProcess(process, root, ready)
        yield provider
    finally:
        if provider is None:
            if process.returncode is None:
                process.kill()
            await process.communicate()
        elif process.returncode is None:
            await provider.stop()
        shutil.rmtree(root, ignore_errors=True)


async def _read_ready(process: asyncio.subprocess.Process, ready_file: Path) -> dict[str, object]:
    async with asyncio.timeout(2):
        while not ready_file.exists():
            if process.returncode is not None:
                stdout, stderr = await process.communicate()
                raise AssertionError((process.returncode, stdout.decode(), stderr.decode()))
            await asyncio.sleep(0.005)
    value = json.loads(ready_file.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


async def _raw_register_provider(socket_path: Path, credential: str) -> str:
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    try:
        requests = (
            {
                "id": 1,
                "method": "frontend.handshake",
                "params": {
                    "api": {"major": 1, "minor": 0},
                    "client_id": "fixture-operator",
                    "role": "operator",
                    "channel": "rpc",
                    "required_capabilities": [],
                },
            },
            {
                "id": 2,
                "method": "frontend.providers.register",
                "params": {
                    "selector": "fixture-candidate",
                    "kind": "test",
                    "credential_verifier": credential_verifier(credential),
                    "capabilities": ["terminal-provider.v1"],
                    "limits": {},
                },
                "idempotency_key": "fixture-candidate-registration",
            },
        )
        responses = []
        for request in requests:
            writer.write(protocol.encode(request))
            await writer.drain()
            responses.append(json.loads(await protocol.read_message(reader)))
        assert all(response["ok"] is True for response in responses)
        provider_id = responses[1]["result"]["provider_id"]
        assert isinstance(provider_id, str)
        return provider_id
    finally:
        writer.close()
        await writer.wait_closed()


def _create(
    callback_id: str, operation_id: str, *, launch_id: str = "launch-a"
) -> dict[str, object]:
    return {
        "type": "request",
        "id": callback_id,
        "method": "terminal.create",
        "params": {
            "operation_id": operation_id,
            "provider_generation": 7,
            "participant_id": "participant-a",
            "launch_id": launch_id,
            "launch": {
                "executable": "fixture-agent",
                "argv": ["fixture-agent"],
                "cwd": "/tmp",
                "environment": {},
            },
        },
    }


def _inventory(callback_id: str) -> dict[str, object]:
    return {
        "type": "request",
        "id": callback_id,
        "method": "terminal.inventory",
        "params": {"provider_generation": 7},
    }


def _inspect(callback_id: str, terminal: Mapping[str, object]) -> dict[str, object]:
    return {
        "type": "request",
        "id": callback_id,
        "method": "terminal.inspect",
        "params": {
            "provider_generation": 7,
            "terminal_id": terminal["terminal_id"],
            "terminal_incarnation": terminal["terminal_incarnation"],
        },
    }


def _mutation(
    method: str,
    callback_id: str,
    operation_id: str,
    terminal: Mapping[str, object],
    *,
    occupant: str = "fixture-occupant",
) -> dict[str, object]:
    params: dict[str, object] = {
        "operation_id": operation_id,
        "provider_generation": 7,
        "participant_id": "participant-a",
        "terminal_id": terminal["terminal_id"],
        "terminal_incarnation": terminal["terminal_incarnation"],
        "expected_occupant": occupant,
        "require_absent": True,
    }
    if method == "terminal.deliver":
        params["action"] = {"kind": "submit_text", "text": "review"}
    elif method == "terminal.interrupt":
        params["action"] = "interrupt"
    return {"type": "request", "id": callback_id, "method": method, "params": params}


def _result(frame: Mapping[str, object]) -> Mapping[str, object]:
    value = frame["result"]
    assert isinstance(value, Mapping)
    return value


def _error_code(frame: Mapping[str, object]) -> str:
    error = frame["error"]
    assert isinstance(error, Mapping)
    code = error["code"]
    assert isinstance(code, str)
    return code


def _integer(value: object) -> int:
    assert type(value) is int
    return value


def _physical_events(provider: _ProviderProcess) -> list[dict[str, object]]:
    return [entry for entry in provider.evidence() if entry["event"] == "physical_side_effect"]


@pytest.mark.asyncio
async def test_fixture_process_connects_to_candidate_daemon_public_socket(daemon) -> None:
    credential = "fixture-candidate-credential"
    provider_id = await _raw_register_provider(paths.socket_path(), credential)

    async with _provider_process(
        paths.socket_path(), provider_id=provider_id, provider_credential=credential
    ) as provider:
        generation = _integer(provider.ready["provider_generation"])
        created = await daemon.terminal_service.connections.request(
            provider_id,
            generation,
            "terminal.create",
            {
                "operation_id": "candidate-create",
                "provider_generation": generation,
                "participant_id": "candidate-participant",
                "launch_id": "candidate-launch",
                "launch": {
                    "executable": "fixture-agent",
                    "argv": ["fixture-agent"],
                    "cwd": "/tmp",
                    "environment": {},
                },
            },
        )
        terminal = created["terminal"]
        assert isinstance(terminal, Mapping)

        client = FrontendClient(paths.socket_path(), client_id="fixture-observer")
        try:
            refreshed = await client.providers.terminals.list(provider_id, refresh=True)
            inspected = await client.providers.terminals.inspect(
                provider_id,
                str(terminal["terminal_id"]),
                str(terminal["terminal_incarnation"]),
            )
        finally:
            await client.close()

        assert refreshed.value.items[0].terminal_id == terminal["terminal_id"]
        assert (
            inspected.value["terminal"]["terminal_incarnation"] == terminal["terminal_incarnation"]
        )
        assert [entry["method"] for entry in _physical_events(provider)] == ["terminal.create"]


@pytest.mark.asyncio
async def test_fixture_process_handles_public_callbacks_with_durable_evidence() -> None:
    root = Path(tempfile.mkdtemp(prefix="r10peer-", dir="/tmp"))
    socket_path = root / "callback.sock"
    try:
        async with (
            _callback_peer(socket_path) as peer,
            _provider_process(socket_path, plan={"inventory_complete": False}) as provider,
        ):
            session = await peer.next_session()
            assert provider.ready["provider_generation"] == 7
            assert provider.ready["control_environment_clean"] is True
            assert provider.ready["theater_home"] == str(provider.root / "theater")
            assert provider.ready["tmux_tmpdir"] == str(provider.root / "tmux")
            assert str(provider.root).startswith("/tmp/")
            assert len(os.fsencode(provider.root.resolve())) <= 80

            stale_inventory = _inventory("stale-generation")
            stale_inventory["params"] = {"provider_generation": 6}
            await session.send(stale_inventory)
            assert _error_code(await session.read("terminal.inventory")) == "stale_generation"

            await session.send(_create("create", "operation-create"))
            created = _result(await session.read("terminal.create"))
            terminal = created["terminal"]
            assert isinstance(terminal, Mapping)
            process = terminal["process"]
            assert isinstance(process, Mapping)
            assert process["pid"] != os.getpid()
            assert created["outcome"] == "accepted"

            await session.send(_inventory("inventory-first"))
            first_inventory = _result(await session.read("terminal.inventory"))
            await session.send(_inventory("inventory-second"))
            second_inventory = _result(await session.read("terminal.inventory"))
            assert first_inventory["complete"] is False
            assert first_inventory["next_cursor"] is not None
            assert _integer(second_inventory["report_revision"]) > _integer(
                first_inventory["report_revision"]
            )

            await session.send(_inspect("inspect", terminal))
            inspection = _result(await session.read("terminal.inspect"))
            assert _integer(inspection["report_revision"]) > _integer(
                second_inventory["report_revision"]
            )
            assert inspection["presence"] == {"state": "absent", "revision": 1}

            for method, operation_id in (
                ("terminal.deliver", "operation-deliver"),
                ("terminal.interrupt", "operation-interrupt"),
                ("terminal.terminate", "operation-terminate"),
            ):
                await session.send(_mutation(method, method, operation_id, terminal))
                result = _result(await session.read(method))
                assert result["delivery"] == "accepted"
                if method == "terminal.terminate":
                    assert result["exit_confirmed"] is True

            physical = _physical_events(provider)
            assert [entry["method"] for entry in physical] == [
                "terminal.create",
                "terminal.deliver",
                "terminal.interrupt",
                "terminal.terminate",
            ]
            for entry in physical:
                receipt = entry["receipt"]
                assert isinstance(receipt, str)
                assert Path(receipt).is_file()
                assert entry["terminal"] == terminal
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
async def test_fixture_process_replaces_occupants_reuses_ids_and_deduplicates_execution() -> None:
    root = Path(tempfile.mkdtemp(prefix="r10peer-", dir="/tmp"))
    socket_path = root / "callback.sock"
    try:
        async with _callback_peer(socket_path) as peer:
            plan = {
                "reuse_terminal_id": True,
                "replace_occupant_after": {"terminal.deliver": "replacement-occupant"},
            }
            async with _provider_process(socket_path, plan=plan) as provider:
                session = await peer.next_session()
                await session.send(_create("create-first", "operation-create"))
                first = _result(await session.read("terminal.create"))
                first_terminal = first["terminal"]
                assert isinstance(first_terminal, Mapping)

                await session.send(_create("create-retry", "operation-create"))
                replayed = _result(await session.read("terminal.create"))
                assert replayed == first
                assert [entry["method"] for entry in _physical_events(provider)] == [
                    "terminal.create"
                ]

                await session.send(
                    _mutation("terminal.deliver", "replace", "operation-replace", first_terminal)
                )
                assert _result(await session.read("terminal.deliver"))["delivery"] == "accepted"
                await session.send(
                    _mutation("terminal.deliver", "stale-owner", "operation-stale", first_terminal)
                )
                stale_owner = _result(await session.read("terminal.deliver"))
                assert stale_owner["delivery"] == "rejected"

                await session.send(_inspect("inspect-replaced", first_terminal))
                inspected = _result(await session.read("terminal.inspect"))
                inspected_terminal = inspected["terminal"]
                assert isinstance(inspected_terminal, Mapping)
                occupant = inspected_terminal["occupant"]
                assert isinstance(occupant, Mapping)
                assert occupant["id"] == "replacement-occupant"

                await session.send(
                    _mutation(
                        "terminal.terminate",
                        "terminate-first",
                        "operation-terminate-first",
                        first_terminal,
                        occupant="replacement-occupant",
                    )
                )
                assert _result(await session.read("terminal.terminate"))["exit_confirmed"] is True
                await session.send(_create("create-second", "operation-create-second"))
                second = _result(await session.read("terminal.create"))
                second_terminal = second["terminal"]
                assert isinstance(second_terminal, Mapping)
                assert second_terminal["terminal_id"] == first_terminal["terminal_id"]
                assert (
                    second_terminal["terminal_incarnation"]
                    != first_terminal["terminal_incarnation"]
                )

                await session.send(
                    _mutation(
                        "terminal.deliver", "stale-incarnation", "operation-old", first_terminal
                    )
                )
                assert _result(await session.read("terminal.deliver"))["delivery"] == "rejected"
                assert any(entry["event"] == "occupant_replaced" for entry in provider.evidence())
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
async def test_fixture_process_allows_independent_terminals_but_serializes_one_terminal() -> None:
    root = Path(tempfile.mkdtemp(prefix="r10peer-", dir="/tmp"))
    socket_path = root / "callback.sock"
    try:
        async with (
            _callback_peer(socket_path) as peer,
            _provider_process(socket_path, plan={"reply_gates": ["terminal.deliver"]}) as provider,
        ):
            release = provider.root / "terminal.deliver.release"
            session = await peer.next_session()
            await session.send(_create("create-a", "operation-create-a", launch_id="launch-a"))
            first = _result(await session.read("terminal.create"))
            first_terminal = first["terminal"]
            assert isinstance(first_terminal, Mapping)
            await session.send(_create("create-b", "operation-create-b", launch_id="launch-b"))
            second = _result(await session.read("terminal.create"))
            second_terminal = second["terminal"]
            assert isinstance(second_terminal, Mapping)
            assert first_terminal["terminal_id"] != second_terminal["terminal_id"]

            await session.send(
                _mutation("terminal.deliver", "deliver-a", "operation-deliver-a", first_terminal)
            )
            await session.send(
                _mutation("terminal.deliver", "deliver-b", "operation-deliver-b", second_terminal)
            )
            await provider.wait_for_effects("operation-deliver-a", "operation-deliver-b")
            deliveries = [
                event
                for event in _physical_events(provider)
                if event["method"] == "terminal.deliver"
            ]
            assert {event["operation_id"] for event in deliveries} == {
                "operation-deliver-a",
                "operation-deliver-b",
            }
            release.touch()
            assert {
                _result(await session.read("terminal.deliver"))["operation_id"] for _ in range(2)
            } == {
                "operation-deliver-a",
                "operation-deliver-b",
            }
            release.unlink()

            await session.send(
                _mutation(
                    "terminal.deliver", "serial-first", "operation-serial-first", first_terminal
                )
            )
            await session.send(
                _mutation(
                    "terminal.deliver", "serial-second", "operation-serial-second", first_terminal
                )
            )
            await provider.wait_for_effects("operation-serial-first")
            serial_deliveries = [
                event["operation_id"]
                for event in _physical_events(provider)
                if event["method"] == "terminal.deliver"
            ]
            assert serial_deliveries == [
                "operation-deliver-a",
                "operation-deliver-b",
                "operation-serial-first",
            ]
            release.touch()
            assert _result(await session.read("terminal.deliver"))["operation_id"] == (
                "operation-serial-first"
            )
            assert _result(await session.read("terminal.deliver"))["operation_id"] == (
                "operation-serial-second"
            )
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("control", "expected_physical"), [("disconnect_before", 0), ("disconnect_after", 1)]
)
async def test_fixture_process_disconnect_controls_bound_execution_evidence(
    control: str, expected_physical: int
) -> None:
    root = Path(tempfile.mkdtemp(prefix="r10peer-", dir="/tmp"))
    socket_path = root / "callback.sock"
    try:
        async with (
            _callback_peer(socket_path) as peer,
            _provider_process(socket_path, plan={control: ["terminal.create"]}) as provider,
        ):
            session = await peer.next_session()
            await session.send(_create("disconnect", "operation-disconnect"))
            assert await provider.wait() == 0
            assert len(_physical_events(provider)) == expected_physical
            assert any(entry["event"] == f"{control}_side_effect" for entry in provider.evidence())
            assert await asyncio.wait_for(session.reader.readline(), timeout=1) == b""
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
async def test_fixture_process_timeout_replays_unknown_without_repeating_side_effect() -> None:
    root = Path(tempfile.mkdtemp(prefix="r10peer-", dir="/tmp"))
    socket_path = root / "callback.sock"
    try:
        async with _callback_peer(socket_path) as peer:
            plan = {"reply_delays": {"terminal.create": 0.1}}
            async with _provider_process(socket_path, plan=plan, callback_timeout=0.01) as provider:
                session = await peer.next_session()
                request = _create("slow-create", "operation-slow")
                await session.send(request)
                first = await session.read("terminal.create")
                assert _result(first)["outcome"] == "unknown"

                await session.send(request)
                replay = await session.read("terminal.create", timeout=0.1)
                assert replay == first
                assert [entry["method"] for entry in _physical_events(provider)] == [
                    "terminal.create"
                ]
                await asyncio.sleep(0.12)
                await session.send(_inventory("after-timeout"))
                assert _result(await session.read("terminal.inventory"))["provider_generation"] == 7
    finally:
        shutil.rmtree(root, ignore_errors=True)
