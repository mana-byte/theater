"""Plugin-owned parsing of the OpenCode server's documented stdout endpoint line.

The daemon owns bounds and persistence; a malformed announcement raises, since it is a failure.
"""

from __future__ import annotations

from .http import validate_loopback_endpoint

#: The exact documented serve banner, pinned to 1.18.29+c470c79.
SERVER_STDOUT_PREFIX = "opencode server listening on "


def parse_server_stdout_endpoint(line: str) -> str | None:
    """Return the loopback origin from one serve stdout line, or None.

    A banner with a non-loopback URL raises so the daemon reaps the backend instead of persisting
    it.
    """
    stripped = line.strip()
    # Compare without the banner's trailing space so a bare "listening on"
    # line still counts as a malformed announcement, not silence.
    prefix = SERVER_STDOUT_PREFIX.rstrip()
    if not stripped.startswith(prefix):
        return None
    announced = stripped[len(prefix) :].strip()
    if not announced:
        raise ValueError("serve banner announced an empty endpoint")
    host, port = validate_loopback_endpoint(announced)
    return f"http://{host}:{port}"
