"""Focused tests for extracted normalization helpers."""

from theater.constants.trajectory import TRAJECTORY_IDENTIFIER_MAX_BYTES
from theater.harness.contracts.events import EventPath, TokenUsage
from theater.harness.normalization import (
    assemble_timing,
    content_blocks_text,
    decode_json_record,
    epoch_or_number,
    fact_builder,
    first_key,
    first_key_of,
    lane_for_kind,
    loose_trajectory_text,
    optional_trajectory_detail,
    path_details,
    qualified_model,
    reported_cost,
    revision_from,
    tool_failure,
    trajectory_identifier,
    trajectory_usage_from_token_usage,
)
from theater.trajectory.content import ContentFormat
from theater.trajectory.enums import (
    CostProvenance,
    TimingProvenance,
    TrajectoryFailureCategory,
    TrajectoryKind,
    TrajectoryLane,
    TrajectoryStatus,
)

# ---- item 1: content_blocks_text ----


def test_content_blocks_text_flattens_block_list():
    assert content_blocks_text([{"text": "a"}, {"text": "b"}]) == "ab"


def test_content_blocks_text_passes_through_string():
    assert content_blocks_text("hello") == "hello"


def test_content_blocks_text_falls_back_to_json():
    assert content_blocks_text({"k": "v"}) == '{"k":"v"}'


def test_content_blocks_text_none_returns_empty():
    assert content_blocks_text(None) == ""


# ---- item 2: loose_trajectory_text ----


def test_loose_trajectory_text_json_dumps_dict():
    assert loose_trajectory_text({"b": 2, "a": 1}) == '{"a": 1, "b": 2}'


def test_loose_trajectory_text_json_dumps_list():
    assert loose_trajectory_text([1, 2]) == "[1, 2]"


def test_loose_trajectory_text_string_passthrough():
    assert loose_trajectory_text("hello") == "hello"


def test_loose_trajectory_text_other_types_return_empty():
    assert loose_trajectory_text(42) == ""


# ---- item 3: optional_trajectory_detail ----


def test_optional_trajectory_detail_none_returns_none():
    assert optional_trajectory_detail("x", None) is None


def test_optional_trajectory_detail_empty_returns_none():
    assert optional_trajectory_detail("x", "") is None


def test_optional_trajectory_detail_text():
    field = optional_trajectory_detail("x", "hello")
    assert field is not None
    assert field.preview.text == "hello"


def test_optional_trajectory_detail_dict():
    field = optional_trajectory_detail("x", {"a": 1}, format=ContentFormat.JSON)
    assert field is not None
    assert field.preview.text == '{"a": 1}'


def test_optional_trajectory_detail_int():
    field = optional_trajectory_detail("x", 42)
    assert field is not None
    assert field.preview.text == "42"


# ---- item 4: trajectory_identifier overflow_prefix ----


def test_trajectory_identifier_overflow_prefix_off_drops_to_none():
    big = "x" * (TRAJECTORY_IDENTIFIER_MAX_BYTES + 1)
    assert trajectory_identifier(big) is None


def test_trajectory_identifier_overflow_prefix_on_returns_hash():
    big = "x" * (TRAJECTORY_IDENTIFIER_MAX_BYTES + 1)
    result = trajectory_identifier(big, overflow_prefix="vibe")
    assert result is not None
    assert result.startswith("vibe:")
    assert len(result) > len("vibe:")


def test_trajectory_identifier_under_limit_unchanged():
    assert trajectory_identifier("abc") == "abc"


def test_trajectory_identifier_under_limit_with_prefix_unchanged():
    assert trajectory_identifier("abc", overflow_prefix="vibe") == "abc"


# ---- item 5: decode_json_record ----


def test_decode_json_record_valid():
    assert decode_json_record('{"a": 1}') == {"a": 1}


def test_decode_json_record_strips_whitespace():
    assert decode_json_record('  {"a": 1}  ') == {"a": 1}


def test_decode_json_record_empty_returns_none():
    assert decode_json_record("") is None


def test_decode_json_record_whitespace_only_returns_none():
    assert decode_json_record("   ") is None


def test_decode_json_record_invalid_json_returns_none():
    assert decode_json_record("not json") is None


def test_decode_json_record_non_dict_returns_none():
    assert decode_json_record("[1, 2]") is None


def test_decode_json_record_bytes():
    assert decode_json_record(b'{"a": 1}') == {"a": 1}


# ---- item 6: first_key / first_key_of ----


def test_first_key_returns_first_present():
    d = {"b": 2, "a": 1}
    assert first_key(d, "a", "b") == 1


def test_first_key_returns_none_when_absent():
    assert first_key({}, "a") is None


def test_first_key_with_coerce():
    d = {"a": "3"}
    assert first_key(d, "a", coerce=int) == 3


def test_first_key_skips_none_values():
    d = {"a": None, "b": 2}
    assert first_key(d, "a", "b") == 2


def test_first_key_of_searches_multiple_mappings():
    d1 = {"x": 1}
    d2 = {"y": 2}
    assert first_key_of((d1, d2), ("y",)) == 2


def test_first_key_of_returns_none_when_all_absent():
    assert first_key_of(({"a": 1},), ("b",)) is None


# ---- item 7: revision_from ----


def test_revision_from_finds_revision():
    assert revision_from({"revision": 3}) == 3


def test_revision_from_finds_version():
    assert revision_from({"version": 5}) == 5


def test_revision_from_prefers_first_mapping():
    assert revision_from({"revision": 1}, {"revision": 2}) == 1


def test_revision_from_returns_zero_when_absent():
    assert revision_from({}) == 0


def test_revision_from_accepts_zero():
    assert revision_from({"revision": 0}) == 0


# ---- item 8: trajectory_usage_from_token_usage ----


def test_trajectory_usage_from_token_usage_basic():
    usage = TokenUsage(
        model="gpt-4",
        provider="openai",
        input_tokens=100,
        output_tokens=50,
        cache_read_input_tokens=10,
        cache_creation_input_tokens=5,
        reasoning_output_tokens=3,
        cost_usd=0.01,
        cost_provenance=CostProvenance.REPORTED,
        idempotency_key="key1",
    )
    result = trajectory_usage_from_token_usage(usage)
    assert result.model == "gpt-4"
    assert result.provider == "openai"
    assert result.request_id == "key1"
    assert result.input_tokens == 100
    assert result.output_tokens == 50
    assert result.cache_read_tokens == 10
    assert result.cache_write_tokens == 5
    assert result.reasoning_tokens == 3
    assert result.cost_usd == 0.01
    assert result.cost_provenance is CostProvenance.REPORTED


def test_trajectory_usage_from_token_usage_drops_negative_cost():
    usage = TokenUsage(cost_usd=-1.0, cost_provenance=CostProvenance.REPORTED)
    result = trajectory_usage_from_token_usage(usage, validate_cost=True, force_unknown_cost=True)
    assert result.cost_usd is None
    assert result.cost_provenance is CostProvenance.UNKNOWN


def test_trajectory_usage_from_token_usage_preserves_cost_provenance_by_default():
    usage = TokenUsage(cost_usd=0.0, cost_provenance=CostProvenance.REPORTED)
    result = trajectory_usage_from_token_usage(usage)
    assert result.cost_usd == 0.0
    assert result.cost_provenance is CostProvenance.REPORTED


def test_trajectory_usage_from_token_usage_no_force_unknown_cost_by_default():
    usage = TokenUsage(cost_usd=None, cost_provenance=CostProvenance.UNKNOWN)
    result = trajectory_usage_from_token_usage(usage)
    assert result.cost_usd is None
    assert result.cost_provenance is CostProvenance.UNKNOWN


def test_trajectory_usage_from_token_usage_force_unknown_cost():
    usage = TokenUsage(cost_usd=None, cost_provenance=CostProvenance.ESTIMATED)
    result = trajectory_usage_from_token_usage(usage, force_unknown_cost=True)
    assert result.cost_provenance is CostProvenance.UNKNOWN


def test_trajectory_usage_from_token_usage_validate_cost_drops_negative():
    usage = TokenUsage(cost_usd=1.0, cost_provenance=CostProvenance.REPORTED)
    result = trajectory_usage_from_token_usage(usage, validate_cost=True)
    assert result.cost_usd == 1.0
    assert result.cost_provenance is CostProvenance.REPORTED


def test_trajectory_usage_from_token_usage_no_validate_cost_passes_through():
    usage = TokenUsage(cost_usd=0.0, cost_provenance=CostProvenance.REPORTED)
    result = trajectory_usage_from_token_usage(usage, validate_cost=False)
    assert result.cost_usd == 0.0
    assert result.cost_provenance is CostProvenance.REPORTED


# ---- item 9: reported_cost strict_positive split ----


def test_reported_cost_strict_positive_drops_zero():
    cost, prov = reported_cost(0.0, strict_positive=True)
    assert cost is None
    assert prov is CostProvenance.UNKNOWN


def test_reported_cost_strict_positive_accepts_positive():
    cost, prov = reported_cost(1.5, strict_positive=True)
    assert cost == 1.5
    assert prov is CostProvenance.REPORTED


def test_reported_cost_not_strict_positive_accepts_zero():
    cost, prov = reported_cost(0.0, strict_positive=False)
    assert cost == 0.0
    assert prov is CostProvenance.REPORTED


def test_reported_cost_drops_negative():
    cost, prov = reported_cost(-1.0, strict_positive=False)
    assert cost is None
    assert prov is CostProvenance.UNKNOWN


def test_reported_cost_drops_non_numeric():
    cost, prov = reported_cost("free", strict_positive=True)
    assert cost is None
    assert prov is CostProvenance.UNKNOWN


# ---- item 10: qualified_model ----


def test_qualified_model_joins_provider_and_model():
    assert qualified_model("openai", "gpt-4") == "openai/gpt-4"


def test_qualified_model_model_only():
    assert qualified_model(None, "gpt-4") == "gpt-4"


def test_qualified_model_empty_returns_none():
    assert qualified_model(None, None) is None
    assert qualified_model("", "") is None


# ---- item 11: epoch_or_number ----


def test_epoch_or_number_iso_string():
    assert epoch_or_number("2026-08-27T12:00:00Z") == 1787832000.0


def test_epoch_or_number_float():
    assert epoch_or_number(1000.5) == 1000.5


def test_epoch_or_number_int():
    assert epoch_or_number(42) == 42.0


def test_epoch_or_number_invalid_string():
    assert epoch_or_number("not-a-time") is None


def test_epoch_or_number_none():
    assert epoch_or_number(None) is None


# ---- item 12: assemble_timing invariants ----


def test_assemble_timing_all_none_returns_none():
    assert assemble_timing(None, None, None, provenance=TimingProvenance.SOURCE) is None


def test_assemble_timing_end_before_start_drops_end():
    result = assemble_timing(10.0, 5.0, None, provenance=TimingProvenance.SOURCE)
    assert result is not None
    assert result.start == 10.0
    assert result.end is None


def test_assemble_timing_fills_missing_end_from_duration():
    result = assemble_timing(10.0, None, 5000.0, provenance=TimingProvenance.SOURCE)
    assert result is not None
    assert result.start == 10.0
    assert result.end == 15.0
    assert result.duration_ms == 5000.0


def test_assemble_timing_fills_missing_start_from_duration():
    result = assemble_timing(None, 15.0, 5000.0, provenance=TimingProvenance.SOURCE)
    assert result is not None
    assert result.start == 10.0
    assert result.end == 15.0
    assert result.duration_ms == 5000.0


def test_assemble_timing_fills_missing_duration():
    result = assemble_timing(10.0, 15.0, None, provenance=TimingProvenance.SOURCE)
    assert result is not None
    assert result.duration_ms == 5000.0


def test_assemble_timing_first_token_after_end_dropped():
    result = assemble_timing(10.0, 15.0, None, first_token=20.0, provenance=TimingProvenance.SOURCE)
    assert result is not None
    assert result.first_token is None


def test_assemble_timing_first_token_within_bounds():
    result = assemble_timing(10.0, 15.0, None, first_token=12.0, provenance=TimingProvenance.SOURCE)
    assert result is not None
    assert result.first_token == 12.0


# ---- item 13: lane_for_kind (BUG fix) ----


def test_lane_for_kind_error_routes_to_theater():
    assert lane_for_kind(TrajectoryKind.ERROR) is TrajectoryLane.THEATER


def test_lane_for_kind_user_routes_to_input():
    assert lane_for_kind(TrajectoryKind.USER) is TrajectoryLane.INPUT


def test_lane_for_kind_tool_call_routes_to_tools():
    assert lane_for_kind(TrajectoryKind.TOOL_CALL) is TrajectoryLane.TOOLS


def test_lane_for_kind_tool_result_routes_to_tools():
    assert lane_for_kind(TrajectoryKind.TOOL_RESULT) is TrajectoryLane.TOOLS


def test_lane_for_kind_assistant_routes_to_model():
    assert lane_for_kind(TrajectoryKind.ASSISTANT) is TrajectoryLane.MODEL


def test_lane_for_kind_usage_routes_to_model():
    assert lane_for_kind(TrajectoryKind.USAGE) is TrajectoryLane.MODEL


# ---- item 14: tool_failure ----


def test_tool_failure_returns_none_when_not_error():
    assert tool_failure(TrajectoryStatus.COMPLETED, "ok") is None


def test_tool_failure_returns_failure_when_error():
    result = tool_failure(TrajectoryStatus.ERROR, "boom")
    assert result is not None
    assert result.category is TrajectoryFailureCategory.TOOL
    assert result.detail == "boom"


# ---- item 15: path_details ----


def test_path_details_builds_path_fields():
    paths = (EventPath(path="src/main.py", mode="write"),)
    result = path_details(paths)
    assert len(result) == 1
    assert result[0].name == "path.write"
    assert result[0].format is ContentFormat.PATH


def test_path_details_empty():
    assert path_details(()) == ()


# ---- item 16: fact_builder ----


def _identity(v):
    return v if isinstance(v, str) and v else None


def test_fact_builder_clamps_ids():
    build = fact_builder(source="test", identifier=_identity)
    fact = build(
        kind=TrajectoryKind.ASSISTANT,
        summary="hello",
        native_id="abc",
        turn_id="turn1",
        call_id=None,
    )
    assert fact.source == "test"
    assert fact.native_id == "abc"
    assert fact.turn_id == "turn1"
    assert fact.call_id is None


def test_fact_builder_clamps_negative_indices():
    build = fact_builder(source="test", identifier=_identity)
    fact = build(
        kind=TrajectoryKind.ASSISTANT,
        summary="hello",
        raw_index=-1,
        event_ordinal=-5,
    )
    assert fact.raw_index == 0
    assert fact.event_ordinal == 0


def test_fact_builder_fallback_id():
    build = fact_builder(source="test", identifier=_identity)
    fact = build(
        kind=TrajectoryKind.ASSISTANT,
        summary="hello",
        native_id=None,
        fallback_id="fallback-1",
    )
    assert fact.native_id == "fallback-1"


def test_fact_builder_lane_override():
    build = fact_builder(source="test", identifier=_identity)
    fact = build(
        kind=TrajectoryKind.ASSISTANT,
        summary="hello",
        lane_override=TrajectoryLane.THEATER,
    )
    assert fact.lane is TrajectoryLane.THEATER


def test_fact_builder_lane_default():
    build = fact_builder(source="test", identifier=_identity)
    fact = build(kind=TrajectoryKind.USER, summary="hello")
    assert fact.lane is TrajectoryLane.INPUT


def test_fact_builder_details_tupled():
    build = fact_builder(source="test", identifier=_identity)
    detail = optional_trajectory_detail("x", "val")
    assert detail is not None
    fact = build(kind=TrajectoryKind.ASSISTANT, summary="hello", details=[detail])
    assert isinstance(fact.details, tuple)
    assert len(fact.details) == 1
