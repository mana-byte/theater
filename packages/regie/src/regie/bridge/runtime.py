"""Persistent tmux terminal-provider lifecycle over the public Theater SDK."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import math
import os
from collections.abc import Mapping

from regie.bridge.callbacks import TmuxProviderCallbacks
from regie.bridge.state import BridgeStateStore
from regie.contracts import BridgeConfig, BridgeStatus
from regie.tmux.command import TmuxError
from regie.tmux.identity import current_server_identity
from regie.tmux.terminals import (
    ensure_server,
    managed_inventory,
    recover_terminal_launch,
    terminal_identity,
)
from theater.frontend import ConnectionChannel, ConnectionRole, FrontendClient, ProviderClient

_CAPABILITY = "terminal-provider.v1"
_DEFAULT_HEARTBEAT_SECONDS = 10.0
_LIMITS: Mapping[str, object] = {
    "max_frame_bytes": 64 * 1024 * 1024,
    "provider_pending_callbacks": 32,
    "provider_mutations_per_terminal": 1,
    "provider_callback_timeout_seconds": 30,
    "terminals": 500,
}


class TmuxBridge:
    """Own one durable provider identity and reconnect it without touching terminals."""

    def __init__(self, config: BridgeConfig) -> None:
        _validate_config(config)
        self._config = config
        self._state = BridgeStateStore(config.state_dir)
        self._close_event = asyncio.Event()
        self._provider: ProviderClient | None = None
        self._report_client: FrontendClient | None = None
        self._generation: int | None = None
        self._status = BridgeStatus(
            running=False,
            connection_state="stopped",
            process_id=None,
        )
        self._callbacks = TmuxProviderCallbacks(
            self._state,
            generation_usable=lambda generation: (
                self._generation == generation
                and self._provider is not None
                and self._provider.generation_active
            ),
        )

    @property
    def status(self) -> BridgeStatus:
        return self._status

    async def run(self) -> None:
        """Acquire the process lock and reconnect until explicitly closed."""
        self._state.acquire()
        self._close_event.clear()
        self._set_status("starting")
        try:
            delay = self._config.reconnect_initial_seconds
            while not self._close_event.is_set():
                try:
                    await self._register()
                    await self._pin_server()
                    await self._connected_generation()
                    delay = self._config.reconnect_initial_seconds
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._set_status("retrying", detail=self._bounded_detail(exc))
                finally:
                    await self._close_connections()
                    self._generation = None
                if self._close_event.is_set():
                    break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._close_event.wait(), timeout=delay)
                delay = min(delay * 2, self._config.reconnect_max_seconds)
        finally:
            await self._close_connections()
            self._generation = None
            self._state.release()
            self._status = BridgeStatus(running=False, connection_state="stopped")

    async def close(self) -> None:
        """Stop provider connections without killing or moving any terminal."""
        self._close_event.set()
        await self._close_connections()

    async def _register(self) -> None:
        state = self._state.state
        if state.provider_id is not None:
            self._set_status("registered")
            return
        client = FrontendClient(
            self._config.theater_socket,
            client_id=self._config.client_id,
            required_capabilities=(_CAPABILITY,),
        )
        try:
            result = await client.providers.register(
                self._config.selector,
                "tmux",
                hashlib.sha256(state.provider_credential.encode("utf-8")).hexdigest(),
                (_CAPABILITY,),
                _LIMITS,
                idempotency_key=state.registration_key,
            )
        finally:
            await client.close()
        self._state.update(provider_id=result.value.provider_id)
        self._set_status("registered")

    async def _pin_server(self) -> None:
        identity = await ensure_server(cwd=str(self._config.state_dir))
        pinned = self._state.state.tmux_server_identity
        if pinned != identity:
            self._state.update(tmux_server_identity=identity)
        self._set_status("tmux_ready")

    async def _connected_generation(self) -> None:
        state = self._state.state
        assert state.provider_id is not None
        assert state.tmux_server_identity is not None
        provider = ProviderClient(
            self._config.theater_socket,
            client_id=self._config.client_id,
            provider_id=state.provider_id,
            provider_credential=state.provider_credential,
            handlers=self._callbacks.handlers,
            required_capabilities=(_CAPABILITY,),
        )
        self._provider = provider
        handshake = await provider.connect()
        generation = handshake.provider_generation
        if type(generation) is not int:
            raise RuntimeError("provider callback handshake omitted its generation")
        self._generation = generation
        self._set_status("reconciling")
        report_client = FrontendClient(
            self._config.theater_socket,
            client_id=self._config.client_id,
            role=ConnectionRole.PROVIDER,
            channel=ConnectionChannel.RPC,
            required_capabilities=(_CAPABILITY,),
            provider_id=state.provider_id,
            provider_credential=state.provider_credential,
        )
        self._report_client = report_client
        report_handshake = await report_client.connect()
        if report_handshake.provider_generation != generation:
            raise RuntimeError("provider report connection acquired a different generation")
        await self._report_inventory(report_client, provider, generation)
        self._set_status("online")
        heartbeat_seconds = _heartbeat_seconds(handshake.limits)
        while not self._close_event.is_set() and provider.connected:
            closed = asyncio.create_task(provider.wait_closed())
            stopping = asyncio.create_task(self._close_event.wait())
            done, pending = await asyncio.wait(
                {closed, stopping},
                timeout=heartbeat_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if done:
                await asyncio.gather(*done, return_exceptions=True)
                break
            if await current_server_identity() != self._server_identity:
                raise RuntimeError("the pinned tmux server identity changed")
            await self._heartbeat(report_client, provider, generation)
        if not self._close_event.is_set():
            error = provider.last_error
            raise RuntimeError(str(error) if error is not None else "provider connection closed")

    async def _report_inventory(
        self,
        report_client: FrontendClient,
        provider: ProviderClient,
        generation: int,
    ) -> None:
        await self._recover_launches(provider, generation)
        terminals = await managed_inventory(
            provider_id=self._provider_id,
            generation=generation,
            expected_server_identity=self._server_identity,
        )
        revision = self._state.next_report_revision()
        receipts = self._state.receipts()
        await report_client.providers.report(
            generation,
            revision,
            facts={
                "terminals": list(terminals),
                "complete": True,
                "receipts": list(receipts),
            },
        )
        self._state.acknowledge_receipts(receipts)
        provider.renew_lease(generation=generation)

    async def _heartbeat(
        self,
        report_client: FrontendClient,
        provider: ProviderClient,
        generation: int,
    ) -> None:
        revision = self._state.next_report_revision()
        receipts = self._state.receipts()
        if receipts:
            await report_client.providers.report(
                generation,
                revision,
                facts={"complete": False, "receipts": list(receipts)},
            )
            self._state.acknowledge_receipts(receipts)
        else:
            await report_client.providers.heartbeat(generation, revision)
        provider.renew_lease(generation=generation)

    async def _recover_launches(self, provider: ProviderClient, generation: int) -> None:
        def ensure_usable() -> None:
            if (
                self._provider is not provider
                or self._generation != generation
                or not provider.generation_active
            ):
                raise TmuxError("provider generation changed during launch recovery")

        for intent in self._state.launch_intents():
            if not intent.dispatched:
                continue
            if intent.provider_id != self._provider_id:
                raise RuntimeError("durable launch intent belongs to another provider")
            if intent.tmux_server_identity != self._server_identity:
                continue
            recovered = await recover_terminal_launch(
                provider_id=intent.provider_id,
                participant_id=intent.participant_id,
                launch_id=intent.launch_id,
                executable=intent.executable,
                terminal_incarnation=intent.terminal_incarnation,
                provisional_window_name=intent.provisional_window_name,
                expected_server_identity=self._server_identity,
                ensure_usable=ensure_usable,
            )
            if recovered is not None:
                self._state.write_receipt(
                    "terminal.create",
                    intent.operation_id,
                    {
                        "operation_id": intent.operation_id,
                        "provider_generation": intent.provider_generation,
                        "outcome": "accepted",
                        "terminal": terminal_identity(
                            recovered,
                            provider_id=intent.provider_id,
                            generation=intent.provider_generation,
                        ),
                        "launch_id": intent.launch_id,
                    },
                )
                self._state.complete_launch(intent)

    async def _close_connections(self) -> None:
        provider, self._provider = self._provider, None
        report, self._report_client = self._report_client, None
        if provider is not None:
            with contextlib.suppress(Exception):
                await provider.close()
        if report is not None:
            with contextlib.suppress(Exception):
                await report.close()

    @property
    def _provider_id(self) -> str:
        value = self._state.state.provider_id
        if value is None:
            raise RuntimeError("provider is not registered")
        return value

    @property
    def _server_identity(self) -> str:
        value = self._state.state.tmux_server_identity
        if value is None:
            raise RuntimeError("tmux server is not pinned")
        return value

    def _set_status(self, connection_state: str, *, detail: str | None = None) -> None:
        state = self._state.state
        self._status = BridgeStatus(
            running=True,
            connection_state=connection_state,
            provider_id=state.provider_id,
            provider_generation=self._generation,
            process_id=os.getpid(),
            tmux_server_identity=state.tmux_server_identity,
            detail=detail,
        )

    def _bounded_detail(self, error: Exception) -> str:
        detail = f"{type(error).__name__}: {error}"
        credential = self._state.state.provider_credential
        return detail.replace(credential, "[redacted]")[:1024]


def _heartbeat_seconds(limits: Mapping[str, object]) -> float:
    value = limits.get("provider_heartbeat_seconds", _DEFAULT_HEARTBEAT_SECONDS)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or value <= 0
    ):
        return _DEFAULT_HEARTBEAT_SECONDS
    return min(float(value), _DEFAULT_HEARTBEAT_SECONDS)


def _validate_config(config: BridgeConfig) -> None:
    if not config.selector or not config.client_id:
        raise ValueError("bridge selector and client_id must be non-empty")
    values = (config.reconnect_initial_seconds, config.reconnect_max_seconds)
    if any(
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or value <= 0
        for value in values
    ):
        raise ValueError("bridge reconnect delays must be finite positive numbers")
    if config.reconnect_initial_seconds > config.reconnect_max_seconds:
        raise ValueError("initial reconnect delay cannot exceed its maximum")


__all__ = ["TmuxBridge"]
