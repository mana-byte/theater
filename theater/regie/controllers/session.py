"""Session/tmux lifecycle controller for the régie.

``SessionController`` owns the régie's tmux session discovery, mouse/status
option changes, return-key binding, and teardown sequencing. It receives an
explicit ``SessionOperations`` collaborator (the ``app_mod.tmux`` /
``app_mod.panes`` modules or test doubles) rather than ``RegieApp`` or an
untyped service locator. Functions are resolved at call time so tests that
monkeypatch ``app_mod.tmux`` / ``app_mod.panes`` after app construction still
see the patched functions.

The controller never touches Textual widgets, reactives, or notifications.
Mount and teardown return explicit values; the app performs all side effects
in the same order as before, preserving exact observable behavior.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol

logger = logging.getLogger("theater.regie")

#: Note tag on the <prefix> h return key, so teardown can tell "ours" from
#: a binding someone else made after we installed it.
_RETURN_KEY_NOTE = "theater-regie-return"


class SessionOperations(Protocol):
    """tmux functions the controller needs, resolved at call time.

    Mirrors ``theater.tmux`` (client) and ``theater.tmux.panes`` so the app
    can pass both module objects and tests that monkeypatch them after
    construction still see the patched functions.
    """

    def current_pane(self) -> str | None: ...
    async def display_message(self, fmt: str, *, target: str | None = ...) -> str: ...
    async def show_option(self, name: str, *, target: str) -> str | None: ...
    async def set_option(self, name: str, value: str, *, target: str) -> None: ...
    async def unset_option(self, name: str, *, target: str) -> None: ...
    async def bind_key_if_free(
        self, table: str, key: str, command: list[str], *, note: str
    ) -> bool: ...
    async def unbind_key_if_owned(self, table: str, key: str, *, note: str) -> None: ...
    async def break_pane(self, pane_id: str, *, target_window: str | None = ...) -> None: ...


class _DynamicSessionOps:
    """Resolve tmux functions from app_mod at call time.

    Holding the module object itself would snapshot function references at
    construction; resolving by name on each call picks up monkeypatched
    functions set after the app was built.
    """

    def __init__(self, app_mod: Any) -> None:
        self._app_mod = app_mod

    def current_pane(self) -> str | None:
        return self._app_mod.tmux.current_pane()

    async def display_message(self, fmt: str, *, target: str | None = None) -> str:
        return await self._app_mod.tmux.display_message(fmt, target=target)

    async def show_option(self, name: str, *, target: str) -> str | None:
        return await self._app_mod.tmux.show_option(name, target=target)

    async def set_option(self, name: str, value: str, *, target: str) -> None:
        await self._app_mod.tmux.set_option(name, value, target=target)

    async def unset_option(self, name: str, *, target: str) -> None:
        await self._app_mod.tmux.unset_option(name, target=target)

    async def bind_key_if_free(
        self, table: str, key: str, command: list[str], *, note: str
    ) -> bool:
        return await self._app_mod.tmux.bind_key_if_free(table, key, command, note=note)

    async def unbind_key_if_owned(self, table: str, key: str, *, note: str) -> None:
        await self._app_mod.tmux.unbind_key_if_owned(table, key, note=note)

    async def break_pane(self, pane_id: str, *, target_window: str | None = None) -> None:
        await self._app_mod.panes.break_pane(pane_id, target_window=target_window)


class SessionController:
    """Owns tmux session discovery, option changes, and teardown sequencing.

    Stateful: holds ``my_pane``, ``my_window``, ``my_session``,
    ``my_session_name``, prior mouse/status values, changed/owned flags,
    return-key flag, and torn-down flag.

    Constructed with the ``app_mod`` (for dynamic tmux resolution) and used
    by ``RegieApp`` via explicit values and thin wrappers — never receives
    ``RegieApp`` itself.
    """

    def __init__(self, app_mod: Any) -> None:
        self._ops: SessionOperations = _DynamicSessionOps(app_mod)
        self.my_pane: str | None = None
        self.my_window: str | None = None
        self.my_session: str | None = None
        self.my_session_name: str | None = None
        self._mouse_prev: str | None = None
        self._mouse_set: bool = False
        self._status_prev: str | None = None
        self._status_set: bool = False
        self._return_key_set: bool = False
        self._torn_down: bool = False

    async def discover_and_setup(self) -> None:
        """Sequential pane/window/session/name discovery, then mouse and status.

        Preserves exact mount ordering: partial discovery of pane, then
        window/session/name; return-key bind whenever a pane exists even if
        later display discovery fails; then enable mouse; then hide status.
        """
        my_pane = self._ops.current_pane()
        if my_pane:
            self.my_pane = my_pane
            try:
                self.my_window = await self._ops.display_message("#{window_id}", target=my_pane)
                self.my_session = await self._ops.display_message("#{session_id}", target=my_pane)
                self.my_session_name = await self._ops.display_message(
                    "#{session_name}", target=my_pane
                )
            except Exception as exc:
                logger.debug("could not discover window/session id: %s", exc)
            await self._bind_return_key()
        await self._enable_mouse()
        await self._hide_status()

    async def _enable_mouse(self) -> None:
        """Turn tmux mouse reporting on for the régie's session."""
        if not self.my_session:
            return
        try:
            self._mouse_prev = await self._ops.show_option("mouse", target=self.my_session)
            await self._ops.set_option("mouse", "on", target=self.my_session)
            self._mouse_set = True
        except Exception as exc:
            logger.debug("could not enable mouse: %s", exc)

    async def _restore_mouse(self) -> None:
        if not self._mouse_set or not self.my_session:
            return
        self._mouse_set = False
        try:
            if self._mouse_prev is None:
                await self._ops.unset_option("mouse", target=self.my_session)
            else:
                await self._ops.set_option("mouse", self._mouse_prev, target=self.my_session)
        except Exception as exc:
            logger.debug("could not restore mouse: %s", exc)

    async def _hide_status(self) -> None:
        """Hide tmux's own status line while the régie is up."""
        if not self.my_session:
            return
        try:
            self._status_prev = await self._ops.show_option("status", target=self.my_session)
            await self._ops.set_option("status", "off", target=self.my_session)
            self._status_set = True
        except Exception as exc:
            logger.debug("could not hide status line: %s", exc)

    async def _restore_status(self) -> None:
        if not self._status_set or not self.my_session:
            return
        self._status_set = False
        try:
            if self._status_prev is None:
                await self._ops.unset_option("status", target=self.my_session)
            else:
                await self._ops.set_option("status", self._status_prev, target=self.my_session)
        except Exception as exc:
            logger.debug("could not restore status line: %s", exc)

    async def _bind_return_key(self) -> None:
        """Claim <prefix> h for select-pane -L, unless the user already has it."""
        try:
            self._return_key_set = await self._ops.bind_key_if_free(
                "prefix", "h", ["select-pane", "-L"], note=_RETURN_KEY_NOTE
            )
        except Exception as exc:
            logger.debug("could not bind <prefix> h return key: %s", exc)

    async def _unbind_return_key(self) -> None:
        if not self._return_key_set:
            return
        self._return_key_set = False
        try:
            await self._ops.unbind_key_if_owned("prefix", "h", note=_RETURN_KEY_NOTE)
        except Exception as exc:
            logger.debug("could not unbind <prefix> h return key: %s", exc)

    async def teardown(self, *, staged_pane: str | None) -> None:
        """Leave tmux as we found it: nothing staged, options restored.

        Preserves exact teardown ordering: mark torn down first; break staged
        pane best-effort; restore mouse; restore status; unbind owned return
        key. Each failure is isolated so later restores still run.
        """
        if self._torn_down:
            return
        self._torn_down = True
        if staged_pane:
            try:
                await self._ops.break_pane(staged_pane)
            except Exception as exc:
                logger.debug("unstage on exit failed: %s", exc)
        await self._restore_mouse()
        await self._restore_status()
        await self._unbind_return_key()
