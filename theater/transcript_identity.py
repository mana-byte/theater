"""Shared transcript identity quarantine helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from theater.provenance import is_trusted_provenance

TRANSCRIPT_IDENTITY_LOST_CODE = "transcript_identity_lost"


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
    readable. Non-file locations such as ``opencode://...`` are left to their
    source adapter because their liveness is not represented by the filesystem.
    """
    if not location or not is_trusted_provenance(provenance):
        return None
    if location.startswith("opencode://"):
        return None
    path = Path(location).expanduser()
    if domain is not None:
        try:
            root = Path(domain).expanduser().resolve(strict=False)
            path.resolve(strict=False).relative_to(root)
        except (OSError, ValueError):
            return (
                f"trusted transcript pin {location!r} no longer exists inside its "
                "trusted transcript domain"
            )
    try:
        if not path.exists():
            return f"trusted transcript pin {location!r} no longer exists on disk"
        if not path.is_file():
            return f"trusted transcript pin {location!r} is not a readable transcript file"
        with path.open("rb"):
            pass
    except OSError as exc:
        return f"trusted transcript pin {location!r} is not readable: {exc}"
    return None


def participant_trusted_pin_unavailable_reason(participant: Any) -> str | None:
    """Filesystem loss reason for a participant's trusted pinned transcript."""
    return trusted_location_unavailable_reason(
        location=getattr(participant, "transcript_location", None),
        provenance=getattr(participant, "session_correlation", None),
        domain=getattr(participant, "transcript_domain", None),
    )
