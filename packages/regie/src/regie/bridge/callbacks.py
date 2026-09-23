"""Schema-shaped terminal callbacks backed by exact tmux evidence."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping

from regie.bridge.persistence import BridgePersistence
from regie.bridge.state import BridgeStateStore
from regie.bridge.timing import phase, timed_handlers
from regie.tmux.command import TmuxError, TmuxOutcomeUnknown
from regie.tmux.identity import PaneSnapshot, exact_match, pane_snapshot
from regie.tmux.presence import PresenceChanged, PresenceEvidence, PresenceObserver
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
        presence_observer: PresenceObserver | None = None,
        presence_validator: Callable[[PresenceEvidence], None] | None = None,
        persistence: BridgePersistence | None = None,
    ) -> None:
        self._state = state
        self._persistence = persistence or BridgePersistence()
        self._generation_usable = generation_usable
        self._presence_observer = presence_observer
        self._presence_validator = presence_validator
        self._terminal_locks: dict[str, asyncio.Lock] = {}
        self._launch_locks: dict[str, asyncio.Lock] = {}

    @property
    def handlers(self) -> Mapping[str, CallbackHandler]:
        return timed_handlers(
            {
                "terminal.create": self.create,
                "terminal.inventory": self.inventory,
                "terminal.inspect": self.inspect,
                "terminal.deliver": self.deliver,
                "terminal.interrupt": self.interrupt,
                "terminal.terminate": self.terminate,
            }
        )

    async def create(self, request: CallbackRequest) -> Mapping[str, object] | CallbackResponse:
        stale = self._stale(request)
        if stale is not None:
            return stale
        params = request.params
        operation_id = str(params["operation_id"])
        launch_id = str(params["launch_id"])
        cached = await self._persistence.run(self._state.receipt, request.method, operation_id)
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
            cached = await self._persistence.run(self._state.receipt, request.method, operation_id)
            if (
                cached is not None
                and cached.get("provider_generation") == request.provider_generation
            ):
                return cached
            intent = await self._persistence.run(
                self._state.prepare_launch,
                launch_id,
                digest,
                operation_id=operation_id,
                provider_id=self._provider_id,
                provider_generation=request.provider_generation,
                participant_id=str(params["participant_id"]),
                executable=str(launch["executable"]),
                tmux_server_identity=self._server_identity,
            )

            async def before_create() -> None:
                nonlocal intent
                self._require_usable(request)
                intent = await self._persistence.run(self._state.mark_launch_dispatched, intent)
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
            await self._persistence.run(self._state.complete_launch, intent)
            await self._persistence.run(
                self._state.write_receipt, request.method, operation_id, result
            )
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
            "report_revision": await self._persistence.run(self._state.next_report_revision),
            "complete": cursor is None and not more,
            "terminals": list(terminals),
            "next_cursor": next_cursor,
            "tmux_server_identity": self._server_identity,
        }

    async def inspect(self, request: CallbackRequest) -> Mapping[str, object] | CallbackResponse:
        stale = self._stale(request)
        if stale is not None:
            return stale
        params = request.params
        screen_max_bytes = params.get("screen_max_bytes", 0)
        assert type(screen_max_bytes) is int
        expected_terminal = params.get("expected_terminal")
        assert expected_terminal is None or isinstance(expected_terminal, Mapping)
        try:
            with phase("inspect"):
                identity, presence, screen, alive = await inspect_terminal(
                    provider_id=self._provider_id,
                    generation=request.provider_generation,
                    terminal_id=str(params["terminal_id"]),
                    terminal_incarnation=str(params["terminal_incarnation"]),
                    expected_server_identity=self._server_identity,
                    screen_max_bytes=screen_max_bytes,
                    expected_terminal=expected_terminal,
                    presence_observer=self._presence_observer,
                )
        except TmuxError:
            return _error("stale_terminal", "The tmux terminal identity is absent or stale.")
        with phase("revision"):
            report_revision, revision = await self._persistence.run(
                self._state.next_inspection_revisions
            )
        stale = self._stale(request)
        if stale is not None:
            return stale
        if alive and self._presence_validator is not None:
            try:
                self._presence_validator(presence)
            except TmuxError:
                presence = PresenceEvidence("unknown", "focus_changed_during_inspection", None)
        return {
            "provider_generation": request.provider_generation,
            "report_revision": report_revision,
            "terminal": identity,
            "presence": {
                "state": presence.state,
                "revision": revision,
                "reason": presence.reason,
            },
            "mode": presence.mode,
            "screen": screen,
            "lifecycle": {"alive": alive, "authoritative": True, "reason": presence.reason},
        }

    async def deliver(self, request: CallbackRequest) -> Mapping[str, object] | CallbackResponse:
        async def apply(pane_id: str, before_effect: Callable[[], None]) -> None:
            action = request.params["action"]
            assert isinstance(action, Mapping)
            await deliver_action(
                pane_id,
                action,
                before_effect=before_effect,
            )

        return await self._mutation(request, apply)

    async def interrupt(self, request: CallbackRequest) -> Mapping[str, object] | CallbackResponse:
        check: Callable[[], None]

        async def recheck() -> None:
            nonlocal check
            inspected = await self._mutation_preflight(request)
            if isinstance(inspected, CallbackResponse):
                raise TmuxError("terminal identity or presence changed between interrupt keys")
            snapshot, _identity, _revision, check = inspected
            if await pane_snapshot(snapshot.pane_id) != snapshot:
                raise TmuxError("terminal changed between interrupt keys")

        def before_key() -> None:
            check()

        async def apply(pane_id: str, before_effect: Callable[[], None]) -> None:
            nonlocal check
            check = before_effect
            await interrupt_terminal(
                pane_id,
                request.params["action"],
                before_effect=before_key,
                recheck=recheck,
            )

        return await self._mutation(request, apply)

    async def terminate(self, request: CallbackRequest) -> Mapping[str, object] | CallbackResponse:
        operation_id = str(request.params["operation_id"])
        cached = await self._cached_mutation(request)
        if cached is not None:
            return cached
        lock = self._terminal_locks.setdefault(str(request.params["terminal_id"]), asyncio.Lock())
        with phase("lock_wait"):
            await lock.acquire()
        try:
            cached = await self._cached_mutation(request)
            if cached is not None:
                return cached
            with phase("preflight"):
                inspected = await self._mutation_preflight(request)
            if isinstance(inspected, CallbackResponse):
                return inspected
            snapshot, identity, revision, before_effect = inspected
            stale = self._stale(request)
            if stale is not None:
                return stale
            try:
                with phase("effect"):
                    exit_confirmed = await terminate_terminal(
                        snapshot,
                        before_effect=before_effect,
                    )
            except PresenceChanged as exc:
                return _error("human_present", str(exc))
            if not exit_confirmed:
                raise TmuxError("tmux did not confirm exit of the exact terminal")
            result = self._delivery_result(request, identity, revision)
            result["exit_confirmed"] = True
            with phase("receipt"):
                await self._persistence.run(
                    self._state.write_receipt, request.method, operation_id, result
                )
            return result
        finally:
            lock.release()

    async def _mutation(
        self,
        request: CallbackRequest,
        apply: Callable[[str, Callable[[], None]], Awaitable[None]],
    ) -> Mapping[str, object] | CallbackResponse:
        operation_id = str(request.params["operation_id"])
        cached = await self._cached_mutation(request)
        if cached is not None:
            return cached
        terminal_id = str(request.params["terminal_id"])
        lock = self._terminal_locks.setdefault(terminal_id, asyncio.Lock())
        with phase("lock_wait"):
            await lock.acquire()
        try:
            cached = await self._cached_mutation(request)
            if cached is not None:
                return cached
            with phase("preflight"):
                inspected = await self._mutation_preflight(request)
            if isinstance(inspected, CallbackResponse):
                return inspected
            snapshot, identity, revision, before_effect = inspected
            current = await pane_snapshot(terminal_id)
            if current != snapshot:
                return _error(
                    "stale_terminal", "The tmux terminal changed immediately before dispatch."
                )
            stale = self._stale(request)
            if stale is not None:
                return stale
            try:
                with phase("effect"):
                    await apply(terminal_id, before_effect)
            except PresenceChanged as exc:
                return _error("human_present", str(exc))
            except TmuxOutcomeUnknown as exc:
                result = _unknown(request, str(exc), code="delivery_unknown")
                await self._persistence.run(
                    self._state.write_receipt, request.method, operation_id, result
                )
                return result
            except asyncio.CancelledError:
                await self._persistence.run(
                    self._state.write_receipt,
                    request.method,
                    operation_id,
                    _unknown(
                        request,
                        "Terminal mutation was cancelled after dispatch began.",
                        code="delivery_unknown",
                    ),
                )
                raise
            result = self._delivery_result(request, identity, revision)
            with phase("receipt"):
                await self._persistence.run(
                    self._state.write_receipt, request.method, operation_id, result
                )
            return result
        finally:
            lock.release()

    async def _cached_mutation(self, request: CallbackRequest) -> dict | CallbackResponse | None:
        stale = self._stale(request)
        if stale is not None:
            return stale
        cached = await self._persistence.run(
            self._state.receipt, request.method, str(request.params["operation_id"])
        )
        stale = self._stale(request)
        if stale is not None:
            return stale
        if cached is not None and cached.get("provider_generation") != request.provider_generation:
            return _unknown(request, "The operation belongs to an earlier provider generation.")
        return cached

    async def _mutation_preflight(
        self, request: CallbackRequest
    ) -> tuple[PaneSnapshot, dict[str, object], int, Callable[[], None]] | CallbackResponse:
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
            with phase("inspect"):
                identity, presence, _screen, _alive = await inspect_terminal(
                    provider_id=self._provider_id,
                    generation=request.provider_generation,
                    terminal_id=snapshot.pane_id,
                    terminal_incarnation=str(params["terminal_incarnation"]),
                    expected_server_identity=self._server_identity,
                    presence_observer=self._presence_observer,
                    snapshot=snapshot,
                )
        except TmuxError:
            return _error("stale_terminal", "The tmux terminal changed during inspection.")
        with phase("revision"):
            revision = await self._persistence.run(self._state.next_presence_revision)
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

        def before_effect() -> None:
            self._require_usable(request)
            if self._presence_validator is not None and not snapshot.dead:
                self._presence_validator(presence)

        return snapshot, identity, revision, before_effect

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


def _unknown(
    request: CallbackRequest, message: str, *, code: str = "stale_generation"
) -> dict[str, object]:
    result: dict[str, object] = {
        "operation_id": request.params["operation_id"],
        "provider_generation": request.provider_generation,
        "terminal_id": request.params["terminal_id"],
        "terminal_incarnation": request.params["terminal_incarnation"],
        "delivery": "unknown",
        "error": {"code": code, "message": message},
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
