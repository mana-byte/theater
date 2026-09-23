"""Régie's reversible tmux session presentation lifecycle."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass

from regie.tmux.bootstrap import (
    REGIE_PANE_OPTION,
    REGIE_WINDOW_OPTION,
    REGIE_WINDOW_OPTION_VALUE,
)
from regie.tmux.command import TmuxError, run
from regie.tmux.identity import current_server_identity, pane_snapshot
from regie.ui_constants import REGIE_RETURN_SIGNAL_TMUX

logger = logging.getLogger("regie")

_SESSION_ID = re.compile(r"^\$[0-9]+$")
_RETURN_KEY = "h"
_RETURN_KEY_NOTE = "theater-regie-return"
_KEY_FORMAT = "#{key_string}\t#{key_note}"
REGIE_LAUNCH_SESSION_OPTION = "@theater-regie-launch-session"


@dataclass(frozen=True, slots=True)
class _RegiePane:
    server_identity: str
    pane_id: str
    window_id: str
    session_id: str


@dataclass(frozen=True, slots=True)
class _OptionLease:
    previous: str | None
    installed: str


class TmuxPresentationSession:
    """Borrow session presentation state and put back only what Régie owns."""

    def __init__(self, *, expected_server_identity: str | None = None) -> None:
        self._server_identity = expected_server_identity
        self._regie: _RegiePane | None = None
        self._mouse: _OptionLease | None = None
        self._status: _OptionLease | None = None
        self._window_marker: _OptionLease | None = None
        self._pane_marker: _OptionLease | None = None
        self._launch_session: _OptionLease | None = None
        self._return_key_owned = False
        self._return_key_note: str | None = None
        self._opened = False
        self._closed = False

    @property
    def server_identity(self) -> str | None:
        return self._server_identity

    async def open(self) -> None:
        if self._opened or self._closed:
            return
        regie = await self._require_current()
        self._opened = True
        try:
            await self._bind_return_key(regie)
        except Exception as exc:
            logger.debug("could not bind <prefix> h return key: %s", exc)
        try:
            self._mouse = await self._set_option(regie, "mouse", "on")
        except Exception as exc:
            logger.debug("could not enable mouse: %s", exc)
        try:
            self._status = await self._set_option(regie, "status", "off")
        except Exception as exc:
            logger.debug("could not hide status line: %s", exc)
        try:
            self._window_marker = await self._set_window_option(
                regie, REGIE_WINDOW_OPTION, REGIE_WINDOW_OPTION_VALUE
            )
        except Exception as exc:
            logger.debug("could not mark Régie's tmux window: %s", exc)
        try:
            self._pane_marker = await self._set_window_option(
                regie, REGIE_PANE_OPTION, regie.pane_id
            )
        except Exception as exc:
            logger.debug("could not mark Régie's tmux pane: %s", exc)
        try:
            self._launch_session = await self._set_global_option(
                REGIE_LAUNCH_SESSION_OPTION, regie.session_id
            )
        except Exception as exc:
            logger.debug("could not pin provider launches to Régie's session: %s", exc)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if not self._opened:
            return
        regie: _RegiePane | None
        try:
            regie = await self._require_current()
        except Exception as exc:
            logger.debug("could not verify Régie's tmux session during teardown: %s", exc)
            regie = self._regie
            if regie is None:
                return
            try:
                server_identity = await current_server_identity()
            except Exception as server_exc:
                logger.debug("could not verify the tmux server during teardown: %s", server_exc)
                return
            if server_identity != regie.server_identity:
                logger.debug("tmux server identity changed; skipping stale presentation teardown")
                return
        await self._restore_option_isolated(regie, "mouse", self._take_mouse())
        await self._restore_option_isolated(regie, "status", self._take_status())
        await self._restore_window_option_isolated(
            regie, REGIE_PANE_OPTION, self._take_pane_marker()
        )
        await self._restore_window_option_isolated(
            regie, REGIE_WINDOW_OPTION, self._take_window_marker()
        )
        await self._restore_global_option_isolated(
            REGIE_LAUNCH_SESSION_OPTION, self._take_launch_session()
        )
        try:
            await self._unbind_return_key(regie)
        except Exception as exc:
            logger.debug("could not unbind <prefix> h return key: %s", exc)

    async def require_window(self, expected_window: str | None = None) -> str:
        regie = await self._require_current()
        if expected_window is not None and regie.window_id != expected_window:
            raise TmuxError("the requested staging window is no longer Régie's current window")
        return regie.window_id

    async def resize(self, *, width: int) -> None:
        if type(width) is not int or width <= 0:
            raise ValueError("pane width must be a positive integer")
        regie = await self._require_current()
        await run("resize-pane", "-t", regie.pane_id, "-x", str(width))
        await self._require_current()

    async def _require_current(self) -> _RegiePane:
        pane_id = os.environ.get("TMUX_PANE")
        if not pane_id:
            raise TmuxError("the Régie process has no current tmux pane")
        snapshot = await pane_snapshot(pane_id)
        if snapshot is None or snapshot.dead or not snapshot.window_id:
            raise TmuxError("the current Régie pane or window cannot be verified")
        if self._server_identity is None:
            self._server_identity = snapshot.server_identity
        elif snapshot.server_identity != self._server_identity:
            raise TmuxError("the current Régie pane belongs to another tmux server")
        session_id = snapshot.session_id
        if session_id is None:
            session_id = await run("display-message", "-p", "-t", pane_id, "#{session_id}")
        if not _SESSION_ID.fullmatch(session_id):
            raise TmuxError("tmux returned an invalid Régie session identity")
        current = _RegiePane(
            server_identity=snapshot.server_identity,
            pane_id=snapshot.pane_id,
            window_id=snapshot.window_id,
            session_id=session_id,
        )
        if self._regie is None:
            self._regie = current
        elif current != self._regie:
            raise TmuxError("Régie's tmux pane, window, or session identity changed")
        return current

    async def _set_option(self, regie: _RegiePane, name: str, value: str) -> _OptionLease:
        previous = await self._show_option(regie, name)
        await run("set-option", "-t", regie.session_id, name, value)
        return _OptionLease(previous=previous, installed=value)

    async def _show_option(self, regie: _RegiePane, name: str) -> str | None:
        output = await run("show-options", "-t", regie.session_id, name, check=False)
        if not output.strip():
            return None
        parts = output.split(None, 1)
        if len(parts) != 2 or parts[0] != name:
            raise TmuxError(f"tmux returned an invalid {name!r} option")
        return parts[1].strip()

    async def _set_global_option(self, name: str, value: str) -> _OptionLease:
        previous = await self._show_global_option(name)
        await run("set-option", "-g", name, value)
        return _OptionLease(previous=previous, installed=value)

    async def _show_global_option(self, name: str) -> str | None:
        output = await run("show-options", "-g", name, check=False)
        if not output.strip():
            return None
        parts = output.split(None, 1)
        if len(parts) != 2 or parts[0] != name:
            raise TmuxError(f"tmux returned an invalid {name!r} option")
        return parts[1].strip()

    async def _set_window_option(self, regie: _RegiePane, name: str, value: str) -> _OptionLease:
        previous = await self._show_window_option(regie, name)
        await run("set-option", "-w", "-t", regie.window_id, name, value)
        return _OptionLease(previous=previous, installed=value)

    async def _show_window_option(self, regie: _RegiePane, name: str) -> str | None:
        output = await run("show-options", "-w", "-t", regie.window_id, name, check=False)
        if not output.strip():
            return None
        parts = output.split(None, 1)
        if len(parts) != 2 or parts[0] != name:
            raise TmuxError(f"tmux returned an invalid {name!r} option")
        return parts[1].strip()

    async def _restore_option_isolated(
        self,
        regie: _RegiePane,
        name: str,
        lease: _OptionLease | None,
    ) -> None:
        if lease is None:
            return
        try:
            current = await self._show_option(regie, name)
            if current != lease.installed:
                return
            if lease.previous is None:
                await run("set-option", "-u", "-t", regie.session_id, name, check=False)
            else:
                await run("set-option", "-t", regie.session_id, name, lease.previous)
        except Exception as exc:
            logger.debug("could not restore %s: %s", name, exc)

    async def _restore_global_option_isolated(
        self,
        name: str,
        lease: _OptionLease | None,
    ) -> None:
        if lease is None:
            return
        try:
            current = await self._show_global_option(name)
            if current != lease.installed:
                return
            if lease.previous is None:
                await run("set-option", "-u", "-g", name, check=False)
            else:
                await run("set-option", "-g", name, lease.previous)
        except Exception as exc:
            logger.debug("could not restore %s: %s", name, exc)

    async def _restore_window_option_isolated(
        self,
        regie: _RegiePane,
        name: str,
        lease: _OptionLease | None,
    ) -> None:
        if lease is None:
            return
        try:
            current = await self._show_window_option(regie, name)
            if current != lease.installed:
                return
            if lease.previous is None:
                await run("set-option", "-u", "-w", "-t", regie.window_id, name, check=False)
            else:
                await run("set-option", "-w", "-t", regie.window_id, name, lease.previous)
        except Exception as exc:
            logger.debug("could not restore %s: %s", name, exc)

    def _take_mouse(self) -> _OptionLease | None:
        lease, self._mouse = self._mouse, None
        return lease

    def _take_status(self) -> _OptionLease | None:
        lease, self._status = self._status, None
        return lease

    def _take_window_marker(self) -> _OptionLease | None:
        lease, self._window_marker = self._window_marker, None
        return lease

    def _take_pane_marker(self) -> _OptionLease | None:
        lease, self._pane_marker = self._pane_marker, None
        return lease

    def _take_launch_session(self) -> _OptionLease | None:
        lease, self._launch_session = self._launch_session, None
        return lease

    async def _bind_return_key(self, regie: _RegiePane) -> None:
        keys = await run("list-keys", "-T", "prefix", "-F", "#{key_string}")
        if _RETURN_KEY in keys.splitlines():
            return
        note = f"{_RETURN_KEY_NOTE}:{regie.pane_id}"
        on_regie = f"#{{==:#{{pane_id}},{regie.pane_id}}}"
        await run(
            "bind-key",
            "-T",
            "prefix",
            "-N",
            note,
            _RETURN_KEY,
            "if-shell",
            "-F",
            on_regie,
            f"send-keys -t {regie.pane_id} {REGIE_RETURN_SIGNAL_TMUX}",
            "select-pane -L",
        )
        self._return_key_note = note
        self._return_key_owned = True

    async def _unbind_return_key(self, regie: _RegiePane) -> None:
        if not self._return_key_owned or self._return_key_note is None:
            return
        note = self._return_key_note
        self._return_key_owned = False
        self._return_key_note = None
        output = await run("list-keys", "-T", "prefix", "-F", _KEY_FORMAT, check=False)
        for line in output.splitlines():
            key, separator, current_note = line.partition("\t")
            if separator and key == _RETURN_KEY and current_note == note:
                await run("unbind-key", "-T", "prefix", _RETURN_KEY, check=False)
                return


__all__ = ["REGIE_LAUNCH_SESSION_OPTION", "TmuxPresentationSession"]
