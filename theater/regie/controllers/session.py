"""Session/tmux lifecycle controller for the régie.

``SessionController`` owns the régie's tmux session discovery, mouse/status
option changes, return-key binding, and teardown sequencing. It receives
the ``tmux`` and ``panes`` module objects as explicit collaborators and
resolves their function attributes at call time, so tests that monkeypatch
those modules after app construction still see the patched functions.

The controller never touches Textual widgets, reactives, or notifications.
Mount and teardown dispatch through caller-provided hooks so the app's
legacy wrappers and subclass overrides stay in the call path.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Protocol

logger = logging.getLogger("theater.regie")

# Note tag on the <prefix> h return key, so teardown can tell ours from theirs.
_RETURN_KEY_NOTE = "theater-regie-return"

#: Lifecycle hook: an async callable taking no arguments.
LifecycleHook = Callable[[], Awaitable[None]] | None


class TmuxOptions(Protocol):
    """Session-scoped tmux options and key-binding functions the controller needs."""

    def current_pane(self) -> str | None: ...
    async def display_message(self, fmt: str, *, target: str | None = ...) -> str: ...
    async def show_option(self, name: str, *, target: str) -> str | None: ...
    async def set_option(self, name: str, value: str, *, target: str) -> None: ...
    async def unset_option(self, name: str, *, target: str) -> None: ...
    async def bind_key_if_free(
        self, table: str, key: str, command: list[str], *, note: str
    ) -> bool: ...
    async def unbind_key_if_owned(self, table: str, key: str, *, note: str) -> None: ...


class PaneOps(Protocol):
    """Pane mutation the controller needs for teardown unstaging."""

    async def break_pane(self, pane_id: str, *, target_window: str | None = ...) -> None: ...


class _DynamicSessionOps:
    """Resolve tmux/panes functions from module objects at call time.

    Storing the module objects (not snapshots of their functions) means
    monkeypatching ``module.attr`` after construction is visible on the
    next call.
    """

    def __init__(self, tmux: TmuxOptions, panes: PaneOps) -> None:
        self._tmux = tmux
        self._panes = panes

    def current_pane(self) -> str | None:
        return self._tmux.current_pane()

    async def display_message(self, fmt: str, *, target: str | None = None) -> str:
        return await self._tmux.display_message(fmt, target=target)

    async def show_option(self, name: str, *, target: str) -> str | None:
        return await self._tmux.show_option(name, target=target)

    async def set_option(self, name: str, value: str, *, target: str) -> None:
        await self._tmux.set_option(name, value, target=target)

    async def unset_option(self, name: str, *, target: str) -> None:
        await self._tmux.unset_option(name, target=target)

    async def bind_key_if_free(
        self, table: str, key: str, command: list[str], *, note: str
    ) -> bool:
        return await self._tmux.bind_key_if_free(table, key, command, note=note)

    async def unbind_key_if_owned(self, table: str, key: str, *, note: str) -> None:
        await self._tmux.unbind_key_if_owned(table, key, note=note)

    async def break_pane(self, pane_id: str, *, target_window: str | None = None) -> None:
        await self._panes.break_pane(pane_id, target_window=target_window)


class SessionController:
    """Owns tmux session discovery, option changes, and teardown sequencing.

    Stateful: holds ``my_pane``, ``my_window``, ``my_session``,
    ``my_session_name``, prior mouse/status values, changed/owned flags,
    return-key flag, and torn-down flag.

    Constructed with the ``tmux`` and ``panes`` module collaborators (or
    test doubles) and used by ``RegieApp`` via explicit hooks and thin
    wrappers — never receives ``RegieApp`` itself.
    """

    def __init__(self, tmux: TmuxOptions, panes: PaneOps) -> None:
        self._ops = _DynamicSessionOps(tmux, panes)
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

    async def discover_and_setup(
        self,
        *,
        bind_return_key: LifecycleHook = None,
        enable_mouse: LifecycleHook = None,
        hide_status: LifecycleHook = None,
    ) -> None:
        """Sequential pane/window/session/name discovery, then mouse and status.

        Preserves exact mount ordering: partial discovery of pane, then
        window/session/name; return-key bind whenever a pane exists even if
        later display discovery fails; then enable mouse; then hide status.
        When hooks are provided, they are called instead of the controller's
        own methods so the app's legacy wrappers (and any monkeypatched
        subclass overrides) stay in the dispatch path.
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
            await (bind_return_key if bind_return_key is not None else self._bind_return_key)()
        await (enable_mouse if enable_mouse is not None else self._enable_mouse)()
        await (hide_status if hide_status is not None else self._hide_status)()

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

    async def teardown(
        self,
        *,
        staged_pane: str | None,
        restore_mouse: LifecycleHook = None,
        restore_status: LifecycleHook = None,
        unbind_return_key: LifecycleHook = None,
    ) -> None:
        """Leave tmux as we found it: nothing staged, options restored.

        Preserves exact teardown ordering: mark torn down first; break staged
        pane best-effort; restore mouse; restore status; unbind owned return
        key. Each failure is isolated so later restores still run. When hooks
        are provided, they are called instead of the controller's own methods
        so the app's legacy wrappers stay in the dispatch path.
        """
        if self._torn_down:
            return
        self._torn_down = True
        if staged_pane:
            try:
                await self._ops.break_pane(staged_pane)
            except Exception as exc:
                logger.debug("unstage on exit failed: %s", exc)
        await (restore_mouse if restore_mouse is not None else self._restore_mouse)()
        await (restore_status if restore_status is not None else self._restore_status)()
        await (unbind_return_key if unbind_return_key is not None else self._unbind_return_key)()
