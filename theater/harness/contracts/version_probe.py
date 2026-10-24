"""Read-only CLI version probes, cached by executable identity and banner pattern."""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .version_policy import version_key

_CACHE_LIMIT = 128
_FAILURE_BACKOFF = 2.0
_cache: dict[tuple[str, int, int, str, int], tuple[str | None, float]] = {}
_lock = threading.Lock()


def probe_release(binary: str, pattern: re.Pattern[str], *, timeout: float) -> str | None:
    """Read the pattern's first capture; malformed banners retry after a short backoff.

    Each plugin owns its banner pattern and admission policy. A replaced executable
    invalidates the cache immediately; process failures are never cached.
    """
    target = shutil.which(binary) or binary
    key = None
    try:
        real = Path(target).resolve()
        info = real.stat()
        key = (str(real), info.st_mtime_ns, info.st_size, pattern.pattern, pattern.flags)
    except OSError:
        pass
    if key is not None:
        with _lock:
            cached = _cache.get(key)
        if cached is not None:
            version, expires = cached
            if time.monotonic() < expires:
                return version
    shown = subprocess.run(
        [target, "--version"], capture_output=True, text=True, timeout=timeout, check=False
    )
    if shown.returncode != 0:
        raise subprocess.CalledProcessError(
            shown.returncode, [target, "--version"], output=shown.stdout, stderr=shown.stderr
        )
    version = None
    for output in (shown.stdout, shown.stderr):
        if match := pattern.search(output.strip()):
            version = match[1]
            break
    expires = time.monotonic() + _FAILURE_BACKOFF
    if version is not None:
        try:
            version_key(version)
        except ValueError:
            pass
        else:
            expires = float("inf")
    if key is not None:
        with _lock:
            if len(_cache) >= _CACHE_LIMIT:
                _cache.pop(next(iter(_cache)))
            _cache[key] = (version, expires)
    return version
