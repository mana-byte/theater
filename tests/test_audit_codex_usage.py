"""Codex per-response trajectory usage identity with legacy compatibility.

Modern Codex reports one ``token_count`` per provider response inside a turn:
``info.last_token_usage`` is that response's share while
``info.total_token_usage`` is thread-cumulative. Theater used to key usage
records by turn id, so two responses in one turn collapsed to whichever
snapshot the aggregation saw last (200/20 instead of 300/30) while repeated
snapshots risked double counting. The fix derives a per-response identity
from the totals/last pair, keeping turn-level identity for legacy records
that carry no ``last_token_usage``.
"""

from __future__ import annotations

import json

from theater.daemon.trajectory.aggregation import overview_for
from theater.daemon.trajectory.project import fact_to_record
from theater.harness.builtin.plugins.codex.observer import CodexObserver
from theater.harness.builtin.plugins.codex.values import _codex_response_usage_key
from theater.trajectory.enums import TrajectoryKind
from theater.trajectory.requests import requests_for_records


def _record(kind: str, payload: dict) -> dict:
    return {"timestamp": "2026-09-10T14:00:00.000Z", "type": kind, "payload": payload}


def _token_count(*, last: dict, total: dict) -> dict:
    return _record(
        "event_msg",
        {"type": "token_count", "info": {"last_token_usage": last, "total_token_usage": total}},
    )


def _turn_with_usage(turn_id: str, snapshots: list[dict]) -> list[dict]:
    return [
        _record("event_msg", {"type": "task_started", "turn_id": turn_id}),
        *snapshots,
        _record("event_msg", {"type": "task_complete", "turn_id": turn_id}),
    ]


def _usage_facts(records: list[dict]) -> list:
    observer = CodexObserver()
    return [
        fact
        for index, record in enumerate(records)
        for fact in observer.parse_record(json.dumps(record), index).trajectory
        if fact.usage is not None
    ]


def _canonical(records: list[dict]):
    return tuple(
        fact_to_record(fact, participant_id="p", source_epoch="epoch")
        for fact in _usage_facts(records)
    )


def _tokens(records: list[dict]) -> tuple[int, int, int]:
    overview = overview_for(_canonical(records), has_older=False, has_coverage_gaps=False)
    return overview.input_tokens, overview.output_tokens, overview.model_operations


def test_response_usage_key_identifies_the_snapshot_not_the_turn():
    first = _codex_response_usage_key(
        {"last_token_usage": {"input_tokens": 100}, "total_token_usage": {"input_tokens": 100}}
    )
    second = _codex_response_usage_key(
        {"last_token_usage": {"input_tokens": 200}, "total_token_usage": {"input_tokens": 300}}
    )
    repeat_first = _codex_response_usage_key(
        {"last_token_usage": {"input_tokens": 100}, "total_token_usage": {"input_tokens": 100}}
    )

    assert first is not None and second is not None
    assert first != second
    assert first == repeat_first
    # The identity names the provider response, never the turn it arrived in:
    # a cached snapshot re-announced in a later turn reproduces the key.
    assert "turn" not in first
    # Legacy snapshots without last_token_usage have no per-response identity.
    assert _codex_response_usage_key({"total_token_usage": {"input_tokens": 100}}) is None
    assert _codex_response_usage_key(None) is None


def test_two_responses_in_one_turn_sum_usage():
    records = _turn_with_usage(
        "turn-1",
        [
            _token_count(
                last={"input_tokens": 100, "output_tokens": 10},
                total={"input_tokens": 100, "output_tokens": 10},
            ),
            _token_count(
                last={"input_tokens": 200, "output_tokens": 20},
                total={"input_tokens": 300, "output_tokens": 30},
            ),
        ],
    )
    facts = _usage_facts(records)

    assert len(facts) == 2
    assert facts[0].usage is not None and facts[1].usage is not None
    assert facts[0].usage.request_id != facts[1].usage.request_id
    assert facts[0].usage.input_tokens == 100
    assert facts[1].usage.input_tokens == 200

    input_tokens, output_tokens, model_operations = _tokens(records)
    assert input_tokens == 300
    assert output_tokens == 30
    # Request grouping stays per turn: usage facts keep the turn's request id.
    assert all(fact.request_id == "turn-1" for fact in facts)
    assert model_operations == 1


def test_repeated_snapshots_of_one_response_do_not_sum():
    snapshot = _token_count(
        last={"input_tokens": 200, "output_tokens": 20},
        total={"input_tokens": 300, "output_tokens": 30},
    )
    records = _turn_with_usage("turn-1", [snapshot, dict(snapshot)])

    facts = _usage_facts(records)
    assert len(facts) == 2
    assert facts[0].usage is not None and facts[1].usage is not None
    assert facts[0].usage.request_id == facts[1].usage.request_id

    input_tokens, output_tokens, _ = _tokens(records)
    assert input_tokens == 200
    assert output_tokens == 20


def test_responses_across_turns_keep_distinct_identities():
    records = [
        *_turn_with_usage(
            "turn-1",
            [
                _token_count(
                    last={"input_tokens": 100, "output_tokens": 10},
                    total={"input_tokens": 100, "output_tokens": 10},
                )
            ],
        ),
        *_turn_with_usage(
            "turn-2",
            [
                _token_count(
                    last={"input_tokens": 50, "output_tokens": 5},
                    total={"input_tokens": 150, "output_tokens": 15},
                )
            ],
        ),
    ]
    facts = _usage_facts(records)
    assert len(facts) == 2
    assert facts[0].usage is not None and facts[1].usage is not None
    assert facts[0].usage.request_id != facts[1].usage.request_id

    input_tokens, output_tokens, model_operations = _tokens(records)
    assert input_tokens == 150
    assert output_tokens == 15
    assert model_operations == 2


def test_legacy_totals_only_snapshots_keep_turn_identity():
    """Old codex token_counts carry only thread-cumulative totals."""
    observer = CodexObserver()
    records = _turn_with_usage(
        "turn-1",
        [
            _record(
                "event_msg",
                {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100}}},
            ),
            _record(
                "event_msg",
                {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 250}}},
            ),
        ],
    )
    facts = [
        fact
        for index, record in enumerate(records)
        for fact in observer.parse_record(json.dumps(record), index).trajectory
        if fact.usage is not None
    ]
    assert len(facts) == 2
    # Turn-level identity keeps the dedupe-and-keep-latest behaviour.
    assert facts[0].usage is not None and facts[1].usage is not None
    assert facts[0].usage.request_id == facts[1].usage.request_id == "turn-1"
    input_tokens, _, _ = _tokens(records)
    assert input_tokens == 250


def test_usage_event_idempotency_still_dedupes_snapshots():
    """The control-side usage Event keeps its totals+latest idempotency key."""
    observer = CodexObserver()
    snapshot = _token_count(
        last={"input_tokens": 200, "output_tokens": 20},
        total={"input_tokens": 300, "output_tokens": 30},
    )
    line = json.dumps(snapshot)
    first = [event for event in observer.parse(line, 0) if event.usage is not None]
    second = [event for event in observer.parse(line, 1) if event.usage is not None]

    assert len(first) == 1 and len(second) == 1
    assert first[0].usage is not None and second[0].usage is not None
    assert first[0].usage.idempotency_key == second[0].usage.idempotency_key
    assert first[0].usage.idempotency_key is not None

    other = _token_count(
        last={"input_tokens": 100, "output_tokens": 10},
        total={"input_tokens": 100, "output_tokens": 10},
    )
    third = [event for event in observer.parse(json.dumps(other), 2) if event.usage is not None]
    assert third[0].usage is not None
    assert third[0].usage.idempotency_key != first[0].usage.idempotency_key

    zero = _token_count(last={"input_tokens": 0}, total={"input_tokens": 300})
    assert observer.parse(json.dumps(zero), 3) == []


def test_usage_facts_group_with_the_turn_for_requests():
    records = _turn_with_usage(
        "turn-1",
        [
            _token_count(
                last={"input_tokens": 100, "output_tokens": 10},
                total={"input_tokens": 100, "output_tokens": 10},
            ),
            _token_count(
                last={"input_tokens": 200, "output_tokens": 20},
                total={"input_tokens": 300, "output_tokens": 30},
            ),
        ],
    )
    observer = CodexObserver()
    facts = [
        fact
        for index, record in enumerate(records)
        for fact in observer.parse_record(json.dumps(record), index).trajectory
    ]
    canonical = tuple(
        fact_to_record(fact, participant_id="p", source_epoch="epoch") for fact in facts
    )
    requests = requests_for_records(canonical)
    assert [request.source_request_id for request in requests] == ["turn-1"]
    usage_records = [record for record in canonical if record.usage is not None]
    assert all(record.kind is TrajectoryKind.USAGE for record in usage_records)


def test_cross_turn_cached_repeat_counts_once():
    """A cached snapshot re-announced in the next turn must not double.

    Codex's ``update_rate_limits`` re-sends token_count without updating
    ``token_info``, then re-announces the cached info under the current
    ``TurnContext`` — so a rollout can show turn one's snapshot again at the
    start of turn two. The response identity is the totals/last pair, which
    the repeat reproduces, so it dedupes instead of counting twice.
    """
    cached = _token_count(
        last={"input_tokens": 100, "output_tokens": 10},
        total={"input_tokens": 100, "output_tokens": 10},
    )
    records = [
        *_turn_with_usage("turn-1", [cached]),
        *_turn_with_usage(
            "turn-2",
            [
                dict(cached),  # rate-limit-only re-announcement of the same info
                _token_count(
                    last={"input_tokens": 50, "output_tokens": 5},
                    total={"input_tokens": 150, "output_tokens": 15},
                ),
            ],
        ),
    ]
    facts = _usage_facts(records)
    assert len(facts) == 3
    assert facts[0].usage is not None and facts[1].usage is not None
    assert facts[0].usage.request_id == facts[1].usage.request_id

    input_tokens, output_tokens, _ = _tokens(records)
    assert input_tokens == 150
    assert output_tokens == 15


def test_rate_limit_only_updates_do_not_change_totals():
    """A rate-limit token_count repeats the previous info verbatim."""
    snapshot = _token_count(
        last={"input_tokens": 100, "output_tokens": 10},
        total={"input_tokens": 100, "output_tokens": 10},
    )
    records = _turn_with_usage("turn-1", [snapshot, dict(snapshot), dict(snapshot)])
    input_tokens, output_tokens, model_operations = _tokens(records)
    assert input_tokens == 100
    assert output_tokens == 10
    assert model_operations == 1


def test_token_count_turn_id_does_not_override_response_identity():
    """An explicit ``turn_id`` on the payload must not steal the response id.

    The native token_count event may carry the turn it was announced under;
    that turn is correct for request grouping but must not overwrite the
    per-response identity (it is exactly the cached-re-announcement shape).
    """
    info = {
        "last_token_usage": {"input_tokens": 100, "output_tokens": 10},
        "total_token_usage": {"input_tokens": 100, "output_tokens": 10},
    }
    response_key = _codex_response_usage_key(info)
    assert response_key is not None
    records = _turn_with_usage(
        "turn-1",
        [
            {
                "timestamp": "2026-09-10T14:00:00.000Z",
                "type": "event_msg",
                "payload": {"type": "token_count", "turn_id": "turn-9", "info": info},
            }
        ],
    )
    facts = _usage_facts(records)
    assert len(facts) == 1
    assert facts[0].usage is not None
    assert facts[0].usage.request_id == response_key
    # The payload's own turn still names the record's group (turn-based
    # request grouping is unchanged); only the usage identity is per-response.
    assert facts[0].request_id == "turn-9"
    assert facts[0].turn_id == "turn-9"
