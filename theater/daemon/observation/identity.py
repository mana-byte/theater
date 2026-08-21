"""Ambiguity and ownership predicates for transcript correlation.

Pure functions that consult the registry and store to determine whether a
history read or an attachment candidate is uniquely attributable to one
participant. No mutable state — the reducer and attachment modules hold the
live collision tables.
"""

from __future__ import annotations

from pathlib import Path

from theater.daemon.registry import Registry
from theater.harness import normalize as normalize_harness
from theater.harness.source import History
from theater.models import Status
from theater.provenance import is_trusted_provenance
from theater.transcript_identity import canonical_location, same_location


def history_correlation_is_ambiguous(registry: Registry, pid: str, history: History) -> bool:
    """Whether a history read could belong to another retained participant.

    History is not a live control decision: dead rows matter too, because their
    transcript files remain on disk. A reducer-accepted pin prevents rescanning
    but does not become exact evidence: duplicate pins and post-epoch missing
    pins still refuse. Pre-epoch NULLs are an explicit compatibility allowance
    for installations where Theater had not begun recording locations yet.
    """
    if is_trusted_provenance(history.correlation):
        return False
    if history.location is None:
        # Nothing can be misattributed when no content was found.
        return False
    participant = registry.store.get_participant(pid)
    if participant is None or not participant.cwd:
        return False
    cwd = Path(participant.cwd).resolve()
    domain = history.collision_domain or participant.transcript_domain
    raw_epoch = registry.store.get_meta("transcript_location_epoch")
    try:
        location_epoch = float(raw_epoch) if raw_epoch is not None else None
    except ValueError:
        location_epoch = None
    for other in registry.list(include_dead=True):
        if (
            other.id == pid
            or normalize_harness(other.harness) != normalize_harness(participant.harness)
            or not other.cwd
            or Path(other.cwd).resolve() != cwd
        ):
            continue
        if (
            domain is not None
            and other.transcript_domain is not None
            and domain != other.transcript_domain
        ):
            continue
        if other.transcript_location is not None:
            if history.location is not None and not same_location(
                other.transcript_location, history.location
            ):
                continue
            return True
        if (
            other.status is Status.DEAD
            and location_epoch is not None
            and other.last_activity < location_epoch
        ):
            # Predates location collection; a bounded upgrade-policy choice.
            continue
        return True
    return False


def location_bound_to_another_live(
    pid: str,
    location: str,
    bound_transcripts: dict[str, str],
    store,
    registry: Registry,
) -> bool:
    """Whether *location* is claimed by a different live participant."""
    owner = bound_transcripts.get(canonical_location(location))
    if owner is not None and owner != pid:
        holder = store.get_participant(owner)
        if holder is not None and holder.status is not Status.DEAD:
            return True
    for other in registry.list():
        if other.id == pid or other.status is Status.DEAD:
            continue
        if same_location(other.transcript_location, location):
            return True
    return False


def session_id_bound_to_another_live(
    pid: str,
    session_id: str | None,
    bound_transcripts: dict[str, str],
    binding_sessions: dict[str, str | None],
    store,
    registry: Registry,
) -> bool:
    """Whether *session_id* is claimed by a different live participant."""
    if session_id is None:
        return False
    for other in registry.list():
        if other.id == pid or other.status is Status.DEAD:
            continue
        if session_id == other.session_id and other.session_id is not None:
            return True
    for loc, sid in binding_sessions.items():
        if sid != session_id:
            continue
        bound_pid = bound_transcripts.get(loc)
        if bound_pid is not None and bound_pid != pid:
            holder = store.get_participant(bound_pid)
            if holder is not None and holder.status is not Status.DEAD:
                return True
    return False


def evidence_is_bound_to_another_live(
    pid: str,
    evidence_location: str,
    evidence_session_id: str | None,
    bound_transcripts: dict[str, str],
    binding_sessions: dict[str, str | None],
    store,
    registry: Registry,
) -> bool:
    """Whether loss evidence names a transcript another live participant owns."""
    if location_bound_to_another_live(pid, evidence_location, bound_transcripts, store, registry):
        return True
    return session_id_bound_to_another_live(
        pid, evidence_session_id, bound_transcripts, binding_sessions, store, registry
    )


def has_cwd_competitor(
    pid: str,
    collision_domain: str | None,
    store,
    registry: Registry,
    sources: dict,
) -> bool:
    """Whether another live participant shares this one's cwd and harness."""
    participant = store.get_participant(pid)
    if participant is None or not participant.cwd:
        return False
    cwd = Path(participant.cwd).resolve()
    for other in registry.list():
        if (
            other.id == pid
            or other.status is Status.DEAD
            or normalize_harness(other.harness) != normalize_harness(participant.harness)
            or not other.cwd
            or Path(other.cwd).resolve() != cwd
        ):
            continue
        other_domain = other.transcript_domain
        if other_domain is None:
            other_source = sources.get(other.id)
            other_domain = other_source.collision_domain if other_source is not None else None
        # Distinct declared roots cannot contain the same transcript.
        if (
            collision_domain is not None
            and other_domain is not None
            and collision_domain != other_domain
        ):
            continue
        return True
    return False


def trusted_dead_owner_blocks(pid: str, attached, store, registry: Registry) -> bool:
    """Dead trusted owners keep their transcript unless this is their successor."""
    import logging

    from theater.provenance import is_trusted_provenance
    from theater.transcript_identity import same_location

    _logger = logging.getLogger("theater.observer")
    for other in registry.list(include_dead=True):
        if (
            other.id == pid
            or other.status is not Status.DEAD
            or not same_location(other.transcript_location, attached.location)
            or not is_trusted_provenance(other.session_correlation)
        ):
            continue
        same_session = (
            attached.session_id is not None
            and other.session_id is not None
            and attached.session_id == other.session_id
        )
        if same_session and is_trusted_provenance(attached.correlation):
            return False
        _logger.warning(
            "transcript %s belongs to dead participant %s (%s); refusing %s (%s)",
            attached.location,
            other.id,
            other.session_correlation,
            pid,
            attached.correlation,
        )
        return True
    return False
