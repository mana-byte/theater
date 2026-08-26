"""Link metadata for styled trajectory detail content."""

from __future__ import annotations

from theater.trajectory import LinkDirection, ParticipantLink

DETAIL_PARTICIPANT_META = "trajectory_detail_participant"
DETAIL_PARTICIPANT_RELATION_META = "trajectory_detail_participant_relation"
DETAIL_PARTICIPANT_DIRECTION_META = "trajectory_detail_participant_direction"
DETAIL_PARTICIPANT_TARGET_META = "trajectory_detail_participant_target"
DETAIL_PARTICIPANT_CORRELATION_TYPE_META = "trajectory_detail_participant_correlation_type"
DETAIL_PARTICIPANT_CORRELATION_KEY_META = "trajectory_detail_participant_correlation_key"
DETAIL_PARTICIPANT_EXACT_META = "trajectory_detail_participant_exact"
DETAIL_PARTICIPANT_UNRESOLVED_META = "trajectory_detail_participant_unresolved"
DETAIL_RECORD_TARGET_META = "trajectory_detail_record_target"


def participant_link_meta(link: ParticipantLink) -> dict[str, str]:
    meta = {
        DETAIL_PARTICIPANT_META: link.participant_id,
        DETAIL_PARTICIPANT_RELATION_META: link.relation,
        DETAIL_PARTICIPANT_DIRECTION_META: link.direction.value,
        DETAIL_PARTICIPANT_EXACT_META: "1" if link.target_record_id is not None else "0",
        DETAIL_PARTICIPANT_UNRESOLVED_META: "0",
    }
    if link.target_record_id is not None:
        meta[DETAIL_PARTICIPANT_TARGET_META] = link.target_record_id
    if link.correlation_type is not None:
        meta[DETAIL_PARTICIPANT_CORRELATION_TYPE_META] = link.correlation_type
        assert link.correlation_key is not None
        meta[DETAIL_PARTICIPANT_CORRELATION_KEY_META] = link.correlation_key
    return meta


def participant_link_from_meta(meta: dict[str, object]) -> ParticipantLink | None:
    participant_id = meta.get(DETAIL_PARTICIPANT_META)
    relation = meta.get(DETAIL_PARTICIPANT_RELATION_META)
    direction = meta.get(DETAIL_PARTICIPANT_DIRECTION_META)
    if (
        not isinstance(participant_id, str)
        or not isinstance(relation, str)
        or not isinstance(direction, str)
    ):
        return None
    target_record_id = meta.get(DETAIL_PARTICIPANT_TARGET_META)
    correlation_type = meta.get(DETAIL_PARTICIPANT_CORRELATION_TYPE_META)
    correlation_key = meta.get(DETAIL_PARTICIPANT_CORRELATION_KEY_META)
    if target_record_id is not None and not isinstance(target_record_id, str):
        return None
    if correlation_type is not None and not isinstance(correlation_type, str):
        return None
    if correlation_key is not None and not isinstance(correlation_key, str):
        return None
    try:
        return ParticipantLink(
            participant_id,
            relation,
            LinkDirection(direction),
            target_record_id=target_record_id,
            correlation_type=correlation_type,
            correlation_key=correlation_key,
        )
    except ValueError:
        return None


__all__ = [
    "DETAIL_PARTICIPANT_CORRELATION_KEY_META",
    "DETAIL_PARTICIPANT_CORRELATION_TYPE_META",
    "DETAIL_PARTICIPANT_DIRECTION_META",
    "DETAIL_PARTICIPANT_EXACT_META",
    "DETAIL_PARTICIPANT_META",
    "DETAIL_PARTICIPANT_RELATION_META",
    "DETAIL_PARTICIPANT_TARGET_META",
    "DETAIL_PARTICIPANT_UNRESOLVED_META",
    "DETAIL_RECORD_TARGET_META",
    "participant_link_from_meta",
    "participant_link_meta",
]
