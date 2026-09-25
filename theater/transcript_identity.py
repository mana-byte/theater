"""Shared transcript identity and location-canonicalisation helpers."""

from __future__ import annotations

import errno
import re
import stat
from pathlib import Path

from theater.provenance import is_trusted_provenance

TRANSCRIPT_IDENTITY_LOST_CODE = "transcript_identity_lost"
TRANSCRIPT_SOURCE_UNAVAILABLE_CODE = "transcript_source_unavailable"

#: RFC 3986 scheme grammar + ``://``; opaque — never expanduser/resolve/stat. ``a://b`` also matches
_OPAQUE_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")


def is_opaque_location(value: str) -> bool:
    """True for a scheme-addressed location: an opaque token, never expanduser/resolve/stat it."""
    return bool(_OPAQUE_SCHEME_RE.match(value))


def canonical_location(value: str) -> str:
    """Normalise a transcript location to its canonical spelling.
    Paths are expanduser+resolve'd; ``scheme://`` is opaque. On OSError the original is returned so
    rows persisted before the file vanished still compare.
    """
    if is_opaque_location(value):
        return value
    try:
        return str(Path(value).expanduser().resolve())
    except OSError:
        return value


def same_location(a: str | None, b: str) -> bool:
    """Whether two location strings name the same transcript; ``None`` matches nothing.

    Opaque locations compare literally; paths are canonicalised first, falling back to literal.
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
    """Why a trusted file-backed transcript pin is no longer safe to read; None if fine or not a
    pin.
    Scheme-addressed locations are left to their source adapter.
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
        # Generic source failures (EIO, permissions, fd exhaustion) don't prove identity is wrong.
        return None
    if not stat.S_ISREG(mode):
        return f"trusted transcript pin {location!r} is not a readable transcript file"
    return None
