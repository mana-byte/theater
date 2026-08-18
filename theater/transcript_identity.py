"""Shared transcript identity quarantine helpers."""

from __future__ import annotations

import errno
import re
import stat
from pathlib import Path

from theater.provenance import is_trusted_provenance

TRANSCRIPT_IDENTITY_LOST_CODE = "transcript_identity_lost"
TRANSCRIPT_SOURCE_UNAVAILABLE_CODE = "transcript_source_unavailable"

#: RFC 3986 scheme grammar followed by ``://``. A location matching this is
#: treated as opaque; a relative path whose first segment ends in a colon
#: (``a://b``) would also match and be treated as opaque. That collision is
#: accepted — such paths do not occur as transcript locations, and the
#: alternative disambiguation heuristics are worse than the case they fix.
_OPAQUE_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")


def is_opaque_location(value: str) -> bool:
    """True for a location a source addresses by scheme rather than by path.

    Such locations are opaque tokens — compare them literally, never
    ``expanduser``/``resolve``/``stat`` them. Only path-shaped locations
    get filesystem treatment.
    """
    return bool(_OPAQUE_SCHEME_RE.match(value))


def transcript_identity_recovery_message(pid: str, detail: str | None = None) -> str:
    """Actionable operator recovery text for a quarantined transcript identity."""
    prefix = f"participant {pid!r} has lost transcript identity"
    if detail:
        prefix = f"{prefix}: {detail}"
    return (
        f"{prefix} ({TRANSCRIPT_IDENTITY_LOST_CODE}). Screen status remains live, but "
        "Theater will not attribute transcript text, complete turns from that transcript, "
        "or create send jobs until an operator rebinds it. Run "
        f"`theater candidates {pid}` to inspect candidates, then "
        f"`theater bind {pid} <candidate> --confirm-id {pid}` for the candidate you "
        "verified. If no candidates are listed yet, retry after the next observation poll "
        "before binding."
    )


def trusted_location_unavailable_reason(
    *,
    location: str | None,
    provenance: str | None,
    domain: str | None = None,
) -> str | None:
    """Why a trusted file-backed transcript pin is no longer safe to read.

    ``None`` means either the location is not a trusted pin or it still looks
    readable. Locations addressed by URI scheme (e.g. ``opencode://...``,
    ``nova://...``) are opaque tokens whose liveness is not represented by the
    filesystem, so they are left to their source adapter.
    """
    if not location or not is_trusted_provenance(provenance):
        return None
    if is_opaque_location(location):
        return None
    path = Path(location).expanduser()
    if domain is not None:
        root = Path(domain).expanduser()
        try:
            if not root.is_dir():
                return None
        except OSError:
            return None
        try:
            root = root.resolve(strict=False)
            path.resolve(strict=False).relative_to(root)
        except ValueError:
            return (
                f"trusted transcript pin {location!r} no longer exists inside its "
                "trusted transcript domain"
            )
        except OSError:
            return None
    try:
        mode = path.stat().st_mode
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return f"trusted transcript pin {location!r} no longer exists on disk"
        # EIO, permission failures, exhausted descriptors and other generic
        # source failures do not prove that the persisted identity is wrong.
        # The source reports those through the ordinary observation grace.
        return None
    if not stat.S_ISREG(mode):
        return f"trusted transcript pin {location!r} is not a readable transcript file"
    return None
