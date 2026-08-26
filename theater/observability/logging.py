"""Owned log handlers, crash capture, rotation, and stderr-generation pruning."""

from __future__ import annotations

import contextlib
import logging
import os
import re
import secrets
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from theater.constants.observability import (
    DEFAULT_LOG_BACKUP_COUNT,
    DEFAULT_LOG_MAX_BYTES,
    MAX_ERROR_TYPE_LEN,
    STDERR_GENERATIONS,
    STDERR_TOKEN_BYTES,
    STDERR_TOKEN_HEX_LEN,
    STDERR_TOKEN_RETRIES,
)

FORMATTER_FMT = "%(asctime)s %(levelname)-7s %(name)s %(message)s"
_TOKEN_PATTERN = re.compile(r"^[0-9a-f]+$")
_GENERATION_PATTERN = re.compile(rf"^daemon\.([0-9a-f]{{{STDERR_TOKEN_HEX_LEN}}})\.stderr\.log$")


def make_formatter() -> logging.Formatter:
    return logging.Formatter(FORMATTER_FMT)


def make_rotating_handler(
    path: Path,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    backup_count: int = DEFAULT_LOG_BACKUP_COUNT,
) -> RotatingFileHandler:
    handler = RotatingFileHandler(
        filename=str(path), maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8"
    )
    handler.setFormatter(make_formatter())
    handler.setLevel(logging.NOTSET)
    return handler


def make_stderr_handler() -> logging.StreamHandler:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(make_formatter())
    handler.setLevel(logging.NOTSET)
    return handler


def log_exception(target: logging.Logger, message: str, error: Exception) -> None:
    """Record one exception without allowing telemetry failure to replace it."""
    error_type = f"{type(error).__module__}.{type(error).__qualname__}"[:MAX_ERROR_TYPE_LEN]
    with contextlib.suppress(Exception):
        target.error(
            message,
            exc_info=(type(error), error, error.__traceback__),
            extra={"error.type": error_type},
        )


@contextmanager
def log_unhandled_exceptions(target: logging.Logger, process: str) -> Iterator[None]:
    """Log ordinary unhandled failures and preserve their original traceback."""
    try:
        yield
    except Exception as error:
        log_exception(target, f"{process} crashed", error)
        raise


def validate_token(token: str) -> bool:
    if len(token) != STDERR_TOKEN_HEX_LEN:
        return False
    return bool(_TOKEN_PATTERN.match(token))


def generate_token() -> str:
    return secrets.token_hex(STDERR_TOKEN_BYTES)


def generation_path(directory: Path, token: str) -> Path:
    if not validate_token(token):
        raise ValueError(f"invalid stderr token: {token!r}")
    return directory / f"daemon.{token}.stderr.log"


def create_generation_file(
    directory: Path,
    token: str | None = None,
    retries: int = STDERR_TOKEN_RETRIES,
) -> tuple[Path, str, int]:
    """Create a mode-0600 stderr generation and return its path, token, and fd."""
    if token is not None and not validate_token(token):
        raise ValueError(f"invalid stderr token: {token!r}")
    for _ in range(retries + 1):
        if token is None:
            token = generate_token()
        path = generation_path(directory, token)
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if token is not None and retries == 0:
                raise
            token = None
            continue
        else:
            return path, token, fd
    raise FileExistsError(f"could not create unique generation file after {retries} retries")


def delete_generation_file(path: Path) -> None:
    if _GENERATION_PATTERN.fullmatch(path.name) is None:
        raise ValueError(f"not a stderr generation path: {path}")
    with contextlib.suppress(Exception):
        path.unlink(missing_ok=True)


def prune_stderr_generations(
    directory: Path,
    current: Path | None,
    retain: int = STDERR_GENERATIONS,
) -> int:
    """Prune generations by mtime while pinning current."""
    pattern = "daemon.*.stderr.log"
    files: list[tuple[float, Path]] = []
    for entry in directory.glob(pattern):
        if _GENERATION_PATTERN.fullmatch(entry.name) is None:
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            _stderr_diagnostic(f"prune: cannot stat {entry.name}")
            continue
        files.append((mtime, entry))

    files.sort(key=lambda x: x[0], reverse=True)

    deleted = 0
    non_current_retained = 0
    current_in_files = current is not None and any(p == current for _, p in files)
    non_current_budget = retain - 1 if current_in_files else retain
    non_current_budget = max(non_current_budget, 0)
    for _mtime, path in files:
        if path == current:
            continue
        if non_current_retained < non_current_budget:
            non_current_retained += 1
            continue
        try:
            path.unlink(missing_ok=True)
            deleted += 1
        except OSError:
            _stderr_diagnostic(f"prune: cannot unlink {path.name}")

    return deleted


def _stderr_diagnostic(msg: str) -> None:
    """Write concise diagnostic to raw stderr, best-effort."""
    with contextlib.suppress(Exception):
        sys.stderr.write(f"theater: {msg}\n")
        sys.stderr.flush()
