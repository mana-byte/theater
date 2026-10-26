"""Black-box checks for the standalone non-tmux provider fixture process."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.support.provider_callback_daemon import provider_callback_server
from tests.support.provider_launcher import (
    physical_events,
    provider_process,
    raw_register_provider,
)
from theater import paths
from theater.frontend import FrontendClient
from theater.frontend.schemas import validate_callback_response, validate_public_request


@dataclass
class _CallbackSession:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter

    async def send(self, frame: Mapping[str, object]) -> None:
        self.writer.write(json.dumps(dict(frame), separators=(",", ":")).encode() + b"\n")
        await self.writer.drain()

    async def read(self, method: str, *, timeout: float = 30) -> dict[str, object]:
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
        return await asyncio.wait_for(self._sessions.get(), timeout=10)


@asynccontextmanager
async def _callback_peer(socket_path: Path) -> AsyncIterator[_CallbackPeer]:
    async with provider_callback_server(
        socket_path,
        client_id="fixture-client",
        provider_id="fixture-provider",
        credential="fixture-credential",
        make_session=_CallbackSession,
        check_request=validate_public_request,
    ) as sessions:
        yield _CallbackPeer(socket_path, sessions)


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


async def test_fixture_process_connects_to_candidate_daemon_public_socket(daemon) -> None:
    credential = "fixture-candidate-credential"
    provider_id = await raw_register_provider(paths.socket_path(), credential)

    async with provider_process(
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
        assert [entry["method"] for entry in physical_events(provider)] == ["terminal.create"]


async def test_fixture_process_handles_public_callbacks_with_durable_evidence() -> None:
    root = Path(tempfile.mkdtemp(prefix="provider-peer-", dir="/tmp"))
    socket_path = root / "callback.sock"
    try:
        async with (
            _callback_peer(socket_path) as peer,
            provider_process(socket_path, plan={"inventory_complete": False}) as provider,
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

            physical = physical_events(provider)
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


async def test_fixture_process_replaces_occupants_reuses_ids_and_deduplicates_execution() -> None:
    root = Path(tempfile.mkdtemp(prefix="provider-peer-", dir="/tmp"))
    socket_path = root / "callback.sock"
    try:
        async with _callback_peer(socket_path) as peer:
            plan = {
                "reuse_terminal_id": True,
                "replace_occupant_after": {"terminal.deliver": "replacement-occupant"},
            }
            async with provider_process(socket_path, plan=plan) as provider:
                session = await peer.next_session()
                await session.send(_create("create-first", "operation-create"))
                first = _result(await session.read("terminal.create"))
                first_terminal = first["terminal"]
                assert isinstance(first_terminal, Mapping)

                await session.send(_create("create-retry", "operation-create"))
                replayed = _result(await session.read("terminal.create"))
                assert replayed == first
                assert [entry["method"] for entry in physical_events(provider)] == [
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


async def test_fixture_process_allows_independent_terminals_but_serializes_one_terminal() -> None:
    root = Path(tempfile.mkdtemp(prefix="provider-peer-", dir="/tmp"))
    socket_path = root / "callback.sock"
    try:
        async with (
            _callback_peer(socket_path) as peer,
            provider_process(socket_path, plan={"reply_gates": ["terminal.deliver"]}) as provider,
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
                for event in physical_events(provider)
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
                for event in physical_events(provider)
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


@pytest.mark.parametrize(
    ("control", "expected_physical"), [("disconnect_before", 0), ("disconnect_after", 1)]
)
async def test_fixture_process_disconnect_controls_bound_execution_evidence(
    control: str, expected_physical: int
) -> None:
    root = Path(tempfile.mkdtemp(prefix="provider-peer-", dir="/tmp"))
    socket_path = root / "callback.sock"
    try:
        async with (
            _callback_peer(socket_path) as peer,
            provider_process(socket_path, plan={control: ["terminal.create"]}) as provider,
        ):
            session = await peer.next_session()
            await session.send(_create("disconnect", "operation-disconnect"))
            assert await provider.wait() == 0
            assert len(physical_events(provider)) == expected_physical
            assert any(entry["event"] == f"{control}_side_effect" for entry in provider.evidence())
            assert await asyncio.wait_for(session.reader.readline(), timeout=10) == b""
    finally:
        shutil.rmtree(root, ignore_errors=True)


async def test_fixture_process_timeout_replays_unknown_without_repeating_side_effect() -> None:
    root = Path(tempfile.mkdtemp(prefix="provider-peer-", dir="/tmp"))
    socket_path = root / "callback.sock"
    try:
        async with _callback_peer(socket_path) as peer:
            plan = {"reply_delays": {"terminal.create": 0.1}}
            async with provider_process(socket_path, plan=plan, callback_timeout=0.01) as provider:
                session = await peer.next_session()
                request = _create("slow-create", "operation-slow")
                await session.send(request)
                first = await session.read("terminal.create")
                assert _result(first)["outcome"] == "unknown"

                await session.send(request)
                replay = await session.read("terminal.create")
                assert replay == first
                assert [entry["method"] for entry in physical_events(provider)] == [
                    "terminal.create"
                ]
                await asyncio.sleep(0.12)
                await session.send(_inventory("after-timeout"))
                assert _result(await session.read("terminal.inventory"))["provider_generation"] == 7
    finally:
        shutil.rmtree(root, ignore_errors=True)
