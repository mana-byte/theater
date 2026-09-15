"""Plugin-owned parsing of the OpenCode server's documented stdout endpoint line.

The daemon owns the deadline, byte and line bounds, the log path, the pid,
and persistence; this parser only maps one captured stdout line to the
loopback origin it announces. A line that claims to announce the server but
carries a malformed endpoint raises instead of being ignored — a malformed
announcement is a discovery failure, not silence.
"""

from __future__ import annotations

from .http import validate_loopback_endpoint

#: The exact documented serve banner, pinned to 1.18.29+c470c79.
SERVER_STDOUT_PREFIX = "opencode server listening on "


def parse_server_stdout_endpoint(line: str) -> str | None:
    """Return the loopback origin from one serve stdout line, or None.

    Only the documented banner yields an endpoint. A banner whose URL is not
    a bare loopback http origin raises ValueError so the daemon reaps the
    backend rather than persisting an endpoint nothing may safely reach.
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
