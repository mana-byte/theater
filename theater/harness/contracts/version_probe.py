"""Fresh, read-only CLI version probes; plugins own banner syntax and admission."""

from __future__ import annotations

import re
import shutil
import subprocess


def probe_release(binary: str, pattern: re.Pattern[str], *, timeout: float) -> str | None:
    """Read the first capture afresh: package updates need not change an executable shim."""
    target = shutil.which(binary) or binary
    shown = subprocess.run(
        [target, "--version"], capture_output=True, text=True, timeout=timeout, check=False
    )
    if shown.returncode != 0:
        raise subprocess.CalledProcessError(
            shown.returncode, [target, "--version"], output=shown.stdout, stderr=shown.stderr
        )
    for output in (shown.stdout, shown.stderr):
        if match := pattern.search(output.strip()):
            return match[1]
    return None
