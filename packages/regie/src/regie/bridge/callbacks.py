"""Schema-shaped terminal callbacks backed by exact tmux evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping

from regie.bridge.state import BridgeStateStore
from regie.tmux.command import TmuxError
from regie.tmux.identity import PaneSnapshot, exact_match, pane_snapshot
from regie.tmux.terminals import (
    create_terminal,
    deliver_action,
    inspect_terminal,
    interrupt_terminal,
    managed_inventory,
    terminate_terminal,
)
from theater.frontend import CallbackHandler, CallbackRequest, CallbackResponse


class TmuxProviderCallbacks:
    """One bridge-wide execution fence shared across callback generations."""

    def __init__(
        self,
        state: BridgeStateStore,
        *,
        generation_usable: Callable[[int], bool],
    ) -> None:
        self._state = state
        self._generation_usable = generation_usable
        self._terminal_locks: dict[str, asyncio.Lock] = {}
        self._launch_locks: dict[str, asyncio.Lock] = {}

    @property
    def handlers(self) -> Mapping[str, CallbackHandler]:
        return {
            "terminal.create": self.create,
            "terminal.inventory": self.inventory,
            "terminal.inspect": self.inspect,
            "terminal.deliver": self.deliver,
            "terminal.interrupt": self.interrupt,
            "terminal.terminate": self.terminate,
        }

    async def create(self, request: CallbackRequest) -> Mapping[str, object] | CallbackResponse:
        stale = self._stale(request)
        if stale is not None:
            return stale
        params = request.params
        operation_id = str(params["operation_id"])
        launch_id = str(params["launch_id"])
        cached = self._state.receipt(request.method, operation_id)
        if cached is not None and cached.get("provider_generation") == request.provider_generation:
            return cached
        launch = params["launch"]
        assert isinstance(launch, Mapping)
        argv = launch["argv"]
        environment = launch["environment"]
        assert isinstance(argv, list) and isinstance(environment, Mapping)
        if not all(isinstance(item, str) for item in argv) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in environment.items()
        ):
            return _error("bad_request", "The tmux launch vector is invalid.")
        digest = _launch_digest(
            provider_id=self._provider_id,
            operation_id=operation_id,
            participant_id=str(params["participant_id"]),
            launch_id=launch_id,
            launch=launch,
        )
        lock = self._launch_locks.setdefault(launch_id, asyncio.Lock())
        async with lock:
            stale = self._stale(request)
            if stale is not None:
                return stale
            cached = self._state.receipt(request.method, operation_id)
            if (
                cached is not None
                and cached.get("provider_generation") == request.provider_generation
            ):
                return cached
            intent = self._state.prepare_launch(
                launch_id,
                digest,
                operation_id=operation_id,
                provider_id=self._provider_id,
                provider_generation=request.provider_generation,
                participant_id=str(params["participant_id"]),
                executable=str(launch["executable"]),
            )

            def before_create() -> None:
                nonlocal intent
                self._require_usable(request)
                intent = self._state.mark_launch_dispatched(intent)
                self._require_usable(request)

            def ensure_usable() -> None:
                self._require_usable(request)

            identity = await create_terminal(
                provider_id=self._provider_id,
                generation=request.provider_generation,
                participant_id=str(params["participant_id"]),
                launch_id=launch_id,
                executable=str(launch["executable"]),
                argv=argv,
                cwd=str(launch["cwd"]),
                environment={str(key): str(value) for key, value in environment.items()},
                presentation=(
                    launch.get("presentation")
                    if isinstance(launch.get("presentation"), Mapping)
                    else None
                ),
                expected_server_identity=self._server_identity,
                terminal_incarnation=intent.terminal_incarnation,
                provisional_window_name=intent.provisional_window_name,
                dispatch_previously_started=intent.dispatched,
                before_create=before_create,
                ensure_usable=ensure_usable,
            )
            result: dict[str, object] = {
                "operation_id": operation_id,
                "provider_generation": request.provider_generation,
                "outcome": "accepted",
                "terminal": identity,
                "launch_id": launch_id,
            }
            self._state.complete_launch(intent)
            self._state.write_receipt(request.method, operation_id, result)
            return result

    async def inventory(self, request: CallbackRequest) -> Mapping[str, object] | CallbackResponse:
        stale = self._stale(request)
        if stale is not None:
            return stale
        inventory = sorted(
            await managed_inventory(
                provider_id=self._provider_id,
                generation=request.provider_generation,
                expected_server_identity=self._server_identity,
            ),
            key=lambda terminal: str(terminal["terminal_id"]),
        )
        cursor = request.params.get("cursor")
        start = 0
        if cursor is not None:
            start = next(
                (
                    index + 1
                    for index, terminal in enumerate(inventory)
                    if terminal["terminal_id"] == cursor
                ),
                -1,
            )
            if start < 0:
                return _error("bad_request", "The terminal inventory cursor is unknown.")
        limit = request.params.get("limit", 500)
        assert type(limit) is int
        terminals = inventory[start : start + limit]
        more = start + len(terminals) < len(inventory)
        next_cursor = str(terminals[-1]["terminal_id"]) if more and terminals else None
        return {
            "provider_generation": request.provider_generation,
            "report_revision": self._state.next_report_revision(),
            "complete": cursor is None and not more,
            "terminals": list(terminals),
            "next_cursor": next_cursor,
        }

    async def inspect(self, request: CallbackRequest) -> Mapping[str, object] | CallbackResponse:
        stale = self._stale(request)
        if stale is not None:
            return stale
        params = request.params
        screen_max_bytes = params.get("screen_max_bytes", 0)
        assert type(screen_max_bytes) is int
        try:
            identity, presence, screen, alive = await inspect_terminal(
                provider_id=self._provider_id,
                generation=request.provider_generation,
                terminal_id=str(params["terminal_id"]),
                terminal_incarnation=str(params["terminal_incarnation"]),
                expected_server_identity=self._server_identity,
                screen_max_bytes=screen_max_bytes,
            )
        except TmuxError:
            return _error("stale_terminal", "The tmux terminal identity is absent or stale.")
        revision = self._state.next_presence_revision()
        return {
            "provider_generation": request.provider_generation,
            "report_revision": self._state.next_report_revision(),
            "terminal": identity,
            "presence": {
                "state": presence.state,
                "revision": revision,
                "reason": presence.reason,
            },
            "mode": presence.mode,
            "screen": screen,
            "lifecycle": {"alive": alive, "authoritative": True},
        }

    async def deliver(self, request: CallbackRequest) -> Mapping[str, object] | CallbackResponse:
        async def apply(pane_id: str) -> None:
            action = request.params["action"]
            assert isinstance(action, Mapping)
            await deliver_action(
                pane_id,
                action,
                before_effect=lambda: self._require_usable(request),
            )

        return await self._mutation(request, apply)

    async def interrupt(self, request: CallbackRequest) -> Mapping[str, object] | CallbackResponse:
        async def apply(pane_id: str) -> None:
            await interrupt_terminal(
                pane_id,
                request.params["action"],
                before_effect=lambda: self._require_usable(request),
            )

        return await self._mutation(request, apply)

    async def terminate(self, request: CallbackRequest) -> Mapping[str, object] | CallbackResponse:
        stale = self._stale(request)
        if stale is not None:
            return stale
        operation_id = str(request.params["operation_id"])
        cached = self._state.receipt(request.method, operation_id)
        if cached is not None and cached.get("provider_generation") == request.provider_generation:
            return cached
        if cached is not None:
            return _unknown(request, "The operation belongs to an earlier provider generation.")
        lock = self._terminal_locks.setdefault(str(request.params["terminal_id"]), asyncio.Lock())
        async with lock:
            inspected = await self._mutation_preflight(request)
            if isinstance(inspected, CallbackResponse):
                return inspected
            snapshot, identity, revision = inspected
            stale = self._stale(request)
            if stale is not None:
                return stale
            exit_confirmed = await terminate_terminal(
                snapshot,
                before_effect=lambda: self._require_usable(request),
            )
            if not exit_confirmed:
                raise TmuxError("tmux did not confirm exit of the exact terminal")
            result = self._delivery_result(request, identity, revision)
            result["exit_confirmed"] = True
            self._state.write_receipt(request.method, operation_id, result)
            return result

    async def _mutation(
        self,
        request: CallbackRequest,
        apply: Callable[[str], Awaitable[None]],
    ) -> Mapping[str, object] | CallbackResponse:
        stale = self._stale(request)
        if stale is not None:
            return stale
        operation_id = str(request.params["operation_id"])
        cached = self._state.receipt(request.method, operation_id)
        if cached is not None:
            if cached.get("provider_generation") == request.provider_generation:
                return cached
            return _unknown(request, "The operation belongs to an earlier provider generation.")
        terminal_id = str(request.params["terminal_id"])
        lock = self._terminal_locks.setdefault(terminal_id, asyncio.Lock())
        async with lock:
            inspected = await self._mutation_preflight(request)
            if isinstance(inspected, CallbackResponse):
                return inspected
            snapshot, identity, revision = inspected
            current = await pane_snapshot(terminal_id)
            if current != snapshot:
                return _error(
                    "stale_terminal", "The tmux terminal changed immediately before dispatch."
                )
            stale = self._stale(request)
            if stale is not None:
                return stale
            await apply(terminal_id)
            result = self._delivery_result(request, identity, revision)
            self._state.write_receipt(request.method, operation_id, result)
            return result

    async def _mutation_preflight(
        self, request: CallbackRequest
    ) -> tuple[PaneSnapshot, dict[str, object], int] | CallbackResponse:
        stale = self._stale(request)
        if stale is not None:
            return stale
        params = request.params
        if params["participant_id"] != params["expected_occupant"]:
            return _error(
                "stale_terminal",
                "The participant and expected terminal occupant do not match.",
            )
        snapshot = await pane_snapshot(str(params["terminal_id"]))
        expected_occupant = str(params["expected_occupant"])
        if snapshot is None or not exact_match(
            snapshot,
            server_identity=self._server_identity,
            provider_id=self._provider_id,
            terminal_incarnation=str(params["terminal_incarnation"]),
            occupant_id=expected_occupant,
        ):
            return _error("stale_terminal", "The tmux terminal occupant or incarnation changed.")
        try:
            identity, presence, _screen, _alive = await inspect_terminal(
                provider_id=self._provider_id,
                generation=request.provider_generation,
                terminal_id=snapshot.pane_id,
                terminal_incarnation=str(params["terminal_incarnation"]),
                expected_server_identity=self._server_identity,
            )
        except TmuxError:
            return _error("stale_terminal", "The tmux terminal changed during inspection.")
        revision = self._state.next_presence_revision()
        if self._stale(request) is not None:
            return _error("stale_generation", "The provider generation changed before dispatch.")
        if params["require_absent"] and presence.state != "absent":
            return _error(
                "human_present",
                f"Terminal presence is {presence.state}; protected input was not applied.",
                details={"presence": {"state": presence.state, "revision": revision}},
            )
        if request.method in {"terminal.deliver", "terminal.interrupt"} and presence.mode == "copy":
            return _error("pane_in_mode", "The tmux pane is in copy mode; input was not applied.")
        return snapshot, identity, revision

    def _delivery_result(
        self, request: CallbackRequest, identity: dict[str, object], revision: int
    ) -> dict[str, object]:
        return {
            "operation_id": request.params["operation_id"],
            "provider_generation": request.provider_generation,
            "terminal_id": request.params["terminal_id"],
            "terminal_incarnation": request.params["terminal_incarnation"],
            "delivery": "accepted",
            "presence_revision": revision,
            "terminal": identity,
        }

    @property
    def _provider_id(self) -> str:
        provider_id = self._state.state.provider_id
        if provider_id is None:
            raise RuntimeError("tmux provider has not been registered")
        return provider_id

    @property
    def _server_identity(self) -> str:
        identity = self._state.state.tmux_server_identity
        if identity is None:
            raise RuntimeError("tmux provider has not pinned a server")
        return identity

    def _stale(self, request: CallbackRequest) -> CallbackResponse | None:
        if self._generation_usable(request.provider_generation):
            return None
        return _error("stale_generation", "The callback belongs to an inactive generation.")

    def _require_usable(self, request: CallbackRequest) -> None:
        if not self._generation_usable(request.provider_generation):
            raise TmuxError("the callback generation became inactive before terminal mutation")


def _error(
    code: str, message: str, *, details: Mapping[str, object] | None = None
) -> CallbackResponse:
    error: dict[str, object] = {"code": code[:512], "message": message[:8192]}
    if details is not None:
        error["details"] = dict(details)
    return CallbackResponse(error=error)


def _unknown(request: CallbackRequest, message: str) -> dict[str, object]:
    result: dict[str, object] = {
        "operation_id": request.params["operation_id"],
        "provider_generation": request.provider_generation,
        "terminal_id": request.params["terminal_id"],
        "terminal_incarnation": request.params["terminal_incarnation"],
        "delivery": "unknown",
        "error": {"code": "stale_generation", "message": message},
    }
    if request.method == "terminal.terminate":
        result["exit_confirmed"] = False
    return result


def _launch_digest(
    *,
    provider_id: str,
    operation_id: str,
    participant_id: str,
    launch_id: str,
    launch: Mapping[str, object],
) -> str:
    payload = {
        "provider_id": provider_id,
        "operation_id": operation_id,
        "participant_id": participant_id,
        "launch_id": launch_id,
        "launch": dict(launch),
    }
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = ["TmuxProviderCallbacks"]
