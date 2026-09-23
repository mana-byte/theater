"""Régie-owned rotating logs and event-loop health monitoring."""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import os
import re
import stat
import sys
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import cast

_LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s %(message)s"
_LOG_MAX_BYTES = 10_485_760
_LOG_BACKUPS = 3
_LOG_GENERATIONS = 3
_LAG_INTERVAL_SECONDS = 1.0
_LAG_WARNING_SECONDS = 0.2
_REGIE_IDENTITY_PATTERN = re.compile(r"^(?:pane|pid)-[0-9]+$")
_REGIE_GENERATION_PATTERN = re.compile(
    r"^(?P<identity>(?:pane|pid)-[0-9]+)\.log(?:\.(?:[1-9][0-9]*))?$"
)


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """Open every log generation without following a substituted symlink."""

    def _open(self) -> io.TextIOWrapper:
        def opener(path: str, flags: int) -> int:
            return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)

        stream = cast(
            io.TextIOWrapper,
            open(  # noqa: SIM115 - handler owns and closes the returned stream.
                self.baseFilename,
                self.mode,
                encoding=self.encoding,
                errors=self.errors,
                opener=opener,
            ),
        )
        try:
            os.fchmod(stream.fileno(), 0o600)
        except BaseException:
            stream.close()
            raise
        return stream


class LoggingHandle:
    def __init__(self, handler: logging.Handler) -> None:
        self._handler = handler

    def close(self) -> None:
        logger = logging.getLogger("regie")
        logger.removeHandler(self._handler)
        self._handler.close()


def configure_logging(path: Path) -> LoggingHandle:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent_mode = path.parent.lstat().st_mode
    if stat.S_ISLNK(parent_mode) or not stat.S_ISDIR(parent_mode):
        raise OSError(f"Régie log path is not a directory: {path.parent}")
    path.parent.chmod(0o700)
    handler = _PrivateRotatingFileHandler(
        path,
        maxBytes=_LOG_MAX_BYTES,
        backupCount=_LOG_BACKUPS,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    logger = logging.getLogger("regie")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False
    return LoggingHandle(handler)


def configure_bridge_logging(stream=None) -> LoggingHandle:
    """Send bridge callback latency to the worker's inherited log stream."""
    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    logger = logging.getLogger("regie.bridge.latency")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False
    return _BridgeLoggingHandle(handler)


class _BridgeLoggingHandle(LoggingHandle):
    def close(self) -> None:
        logging.getLogger("regie.bridge.latency").removeHandler(self._handler)
        self._handler.close()


def prune_regie_generations(
    directory: Path,
    current: Path | None,
    protected: Iterable[Path | str] = (),
    retain: int = _LOG_GENERATIONS,
) -> int:
    """Prune inactive log groups while preserving current and live pane identities."""
    protected_identities = {
        identity
        for value in (current, *protected)
        if (identity := _regie_identity(value, allow_input_identity=True)) is not None
    }
    groups: dict[str, list[tuple[float, Path]]] = {}
    failed: set[str] = set()
    try:
        directory_entries = list(directory.iterdir())
    except OSError as error:
        _stderr_diagnostic(f"log prune: cannot list {directory}: {error}")
        return 0

    for entry in directory_entries:
        identity = _regie_identity(entry)
        if identity is None:
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError as error:
            failed.add(identity)
            _stderr_diagnostic(f"log prune: cannot stat {entry.name}: {error}")
            continue
        groups.setdefault(identity, []).append((mtime, entry))

    candidates = [
        (max(mtime for mtime, _path in group_entries), identity, group_entries)
        for identity, group_entries in groups.items()
        if identity not in protected_identities and identity not in failed
    ]
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)

    deleted = 0
    for _mtime, _identity, group_entries in candidates[max(retain, 0) :]:
        for _entry_mtime, path in group_entries:
            try:
                path.unlink(missing_ok=True)
                deleted += 1
            except OSError as error:
                _stderr_diagnostic(f"log prune: cannot unlink {path.name}: {error}")
    return deleted


def _regie_identity(value: Path | str | None, *, allow_input_identity: bool = False) -> str | None:
    if value is None:
        return None
    name = Path(value).name
    if allow_input_identity:
        if name.startswith("%") and name[1:].isdigit():
            return f"pane-{name[1:]}"
        if _REGIE_IDENTITY_PATTERN.fullmatch(name) is not None:
            return name
    match = _REGIE_GENERATION_PATTERN.fullmatch(name)
    return match.group("identity") if match is not None else None


def _stderr_diagnostic(message: str) -> None:
    with contextlib.suppress(Exception):
        sys.stderr.write(f"regie: {message}\n")
        sys.stderr.flush()


def log_exception(target: logging.Logger, message: str, error: Exception) -> None:
    with contextlib.suppress(Exception):
        target.error(message, exc_info=(type(error), error, error.__traceback__))


async def lag_monitor(stopping: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    while not stopping.is_set():
        started = loop.time()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stopping.wait(), timeout=_LAG_INTERVAL_SECONDS)
        if stopping.is_set():
            return
        lag = loop.time() - started - _LAG_INTERVAL_SECONDS
        if lag >= _LAG_WARNING_SECONDS:
            logging.getLogger("regie").warning("event loop blocked for %.0fms", lag * 1_000)


__all__ = [
    "LoggingHandle",
    "configure_bridge_logging",
    "configure_logging",
    "lag_monitor",
    "log_exception",
    "prune_regie_generations",
]
