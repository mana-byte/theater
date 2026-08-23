"""Command runner and shared format infrastructure for tmux calls.

Everything that shells out to tmux goes through ``run`` / ``run_sync`` here so
the surface stays small and mockable. tmux is a hard dependency; if it is
missing we fail loudly rather than degrading, because there is no inbound
delivery path without it.

Targets are always written ``session:``, never bare
-----------------------------------------------
tmux resolves a bare ``-t 0`` as *window index 0*, not as the session named
``0``, and unnamed sessions are named by number — so on a default setup
``new-window -t 0`` means "create at index 0" and fails with "index 0 in use".
The trailing colon makes it a session target and lets tmux choose the index.
This cost a real spawn failure; the argv is now asserted in
tests/test_tmux_client.py, because argv can be checked without a tmux server
and the behaviour cannot.
"""

from __future__ import annotations

import asyncio
import subprocess
from dataclasses import dataclass

from theater import timing
from theater.constants.tmux import (
    TMUX_FIELD_SEPARATOR,
    TMUX_PANE_FORMAT,
    TMUX_RUN_TIMEOUT_SECONDS,
)
from theater.models import TheaterError
from theater.observability.catalog import TMUX_COMMAND

_FORMAT_SEP = TMUX_FIELD_SEPARATOR
_PANE_FORMAT = TMUX_PANE_FORMAT
RUN_TIMEOUT = TMUX_RUN_TIMEOUT_SECONDS


class TmuxError(TheaterError):
    code = "tmux_error"


class TmuxMissing(TmuxError):
    code = "tmux_missing"


@dataclass(frozen=True, slots=True)
class Pane:
    pane_id: str
    pane_pid: int
    cwd: str
    window_id: str
    session: str
    window_name: str
    current_command: str

    @classmethod
    def parse(cls, line: str) -> Pane:
        parts = line.split(_FORMAT_SEP)
        if len(parts) != 7:
            raise TmuxError(f"unexpected list-panes row: {line!r}")
        return cls(
            pane_id=parts[0],
            pane_pid=int(parts[1]),
            cwd=parts[2],
            window_id=parts[3],
            session=parts[4],
            window_name=parts[5],
            current_command=parts[6],
        )


def _require() -> None:
    # Resolve from the facade so test patches to client.available are seen.
    from theater.tmux.client import available

    if not available():
        raise TmuxMissing("tmux is not on PATH; Theater cannot run without it")


def _run_timeout() -> float:
    from theater.tmux.client import RUN_TIMEOUT

    return RUN_TIMEOUT


def run_sync(*args: str, check: bool = True) -> str:
    _require()
    # text=True uses locale.getpreferredencoding(False); pin UTF-8 for non-ASCII pane paths.
    proc = subprocess.run(
        ["tmux", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="backslashreplace",
        timeout=_run_timeout(),
        check=False,
    )
    if check and proc.returncode != 0:
        raise TmuxError(f"tmux {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout.rstrip("\n")


async def run(*args: str, check: bool = True) -> str:
    _require()
    with timing.span(TMUX_COMMAND, command=args[0]) as sp:
        proc = await asyncio.create_subprocess_exec(
            "tmux",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=_run_timeout())
        except TimeoutError:
            proc.kill()
            raise TmuxError(f"tmux {' '.join(args)} timed out") from None
        if proc.returncode != 0:
            sp.set_result("error", error_type="tmux_error")
            if check:
                msg = err.decode("utf-8", "backslashreplace").strip()
                raise TmuxError(f"tmux {' '.join(args)} failed: {msg}")
    return out.decode("utf-8", "backslashreplace").rstrip("\n")
