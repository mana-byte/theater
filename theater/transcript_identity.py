"""Shared transcript identity and location-canonicalisation helpers."""

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


def canonical_location(value: str) -> str:
    """Normalise a transcript location to its canonical spelling.

    A file-backed location is ``expanduser``-ed then ``resolve``-d to an
    absolute path with no ``..`` segments or symlinks, so two spellings of
    the same file compare equal. A ``scheme://`` location is opaque and
    returned unchanged — never ``expanduser``, ``resolve``, or ``stat`` it.

    On ``OSError`` (the path does not exist or the filesystem is
    unreachable) the original value is returned, so a comparison can still
    succeed against a row that was persisted before the file disappeared.
    """
    if is_opaque_location(value):
        return value
    try:
        return str(Path(value).expanduser().resolve())
    except OSError:
        return value


def same_location(a: str | None, b: str) -> bool:
    """Whether two location strings name the same transcript.

    ``None`` is never the same as anything. Opaque locations are compared
    literally (byte-for-byte) without filesystem treatment. File-backed
    locations are canonicalised via :func:`canonical_location` first, so
    ``~/t.jsonl`` and ``/Users/me/t.jsonl`` agree. On ``OSError`` the
    fallback is literal comparison, matching the contract that a row
    persisted by an older daemon may hold a non-canonical string.
    """
    if a is None:
        return False
    if is_opaque_location(a) or is_opaque_location(b):
        return a == b
    return canonical_location(a) == canonical_location(b)


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
