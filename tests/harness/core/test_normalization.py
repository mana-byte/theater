"""Harness-name and trajectory normalization helpers."""

import json

from tests.support.tables import eq_row, is_row, run_rows
from theater.constants.trajectory import TRAJECTORY_IDENTIFIER_MAX_BYTES
from theater.harness import HARNESSES, UNKNOWN_ICON, harness_icon, normalize
from theater.harness.contracts.events import EventPath, TokenUsage
from theater.harness.normalization import (
    assemble_timing,
    content_blocks_text,
    decode_json_record,
    epoch_or_number,
    fact_builder,
    finite_float,
    first_key,
    first_key_of,
    iso_epoch,
    json_container_format,
    lane_for_kind,
    loose_trajectory_text,
    nonnegative_int,
    optional_trajectory_detail,
    path_details,
    qualified_model,
    reported_cost,
    revision_from,
    safe_trajectory_text,
    stable_json,
    tool_failure,
    trajectory_detail,
    trajectory_identifier,
    trajectory_status,
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


def test_claude_code_maps_to_claude():
    assert normalize("claude_code") == "claude"


def test_unknown_name_passes_through():
    """A genuinely unknown harness is not an error — just unobservable."""
    assert normalize("cursor") == "cursor"
    assert normalize("aider") == "aider"


def test_icons_are_distinct_between_harnesses():
    """Two harnesses drawn with the same mark would defeat the point."""
    icons = [h.icon for h in HARNESSES.values()]
    assert len(set(icons)) == len(icons)


def test_an_alias_gets_the_canonical_glyph():
    """The icon rides on normalize, so registered-as names work too."""
    assert harness_icon("claude-code") == harness_icon("claude")
    assert harness_icon("mistral_vibe") == harness_icon("vibe")


def test_an_unknown_harness_falls_back_rather_than_raising():
    assert harness_icon("cursor") == UNKNOWN_ICON


def test_a_missing_harness_name_falls_back():
    """External participants may have no harness recorded at all."""
    assert harness_icon(None) == UNKNOWN_ICON
    assert harness_icon("") == UNKNOWN_ICON


def test_safe_trajectory_text_replaces_invalid_surrogates():
    assert safe_trajectory_text("bad\ud800text") == "bad?text"
    assert safe_trajectory_text(None) == ""


def test_trajectory_identifier_enforces_utf8_controls_and_byte_limit():
    """Identifiers keep clean UTF-8; control chars, surrogates and overflow drop."""
    limit = TRAJECTORY_IDENTIFIER_MAX_BYTES
    run_rows(
        [
            eq_row("plain", lambda: trajectory_identifier("identifier"), "identifier"),
            eq_row("empty", lambda: trajectory_identifier(""), None),
            eq_row("nul", lambda: trajectory_identifier("bad\x00"), None),
            eq_row("del", lambda: trajectory_identifier("bad\x7f"), None),
            eq_row("c1", lambda: trajectory_identifier("bad\x9f"), None),
            eq_row("surrogate", lambda: trajectory_identifier("bad\ud800"), None),
            eq_row("at_limit", lambda: trajectory_identifier("x" * limit), "x" * limit),
            eq_row("over_limit", lambda: trajectory_identifier("x" * (limit + 1)), None),
        ]
    )


def test_stable_json_is_compact_deterministic_and_safe_on_fallback():
    value = {"z": "é", "a": [2, 1]}
    assert stable_json(value) == '{"a":[2,1],"z":"é"}'
    mixed_keys = {1: "one", "a": "two"}
    assert stable_json(mixed_keys) == json.dumps(str(mixed_keys), ensure_ascii=True)


def test_trajectory_detail_uses_safe_text_or_stable_json():
    text = trajectory_detail("text", "bad\ud800", format=ContentFormat.TEXT)
    payload = trajectory_detail("data", {"b": 2, "a": 1}, format=ContentFormat.JSON)
    assert (text.preview.text, text.format) == ("bad?", ContentFormat.TEXT)
    assert (payload.preview.text, payload.format) == ('{"a":1,"b":2}', ContentFormat.JSON)


def test_json_container_format_rejects_json_scalars():
    """Containers and JSON text parse as JSON; scalars and prose stay TEXT."""
    run_rows(
        [
            is_row("dict", lambda: json_container_format({"ok": True}), ContentFormat.JSON),
            is_row("list", lambda: json_container_format([1, 2]), ContentFormat.JSON),
            is_row("object_text", lambda: json_container_format('{"ok":true}'), ContentFormat.JSON),
            is_row("array_text", lambda: json_container_format("[1,2]"), ContentFormat.JSON),
            is_row("scalar_text", lambda: json_container_format('"text"'), ContentFormat.TEXT),
            is_row("number_text", lambda: json_container_format("42"), ContentFormat.TEXT),
            is_row("plain_text", lambda: json_container_format("plain text"), ContentFormat.TEXT),
        ]
    )


def test_nonnegative_int_rejects_booleans_negative_and_nonintegral_values():
    """Only nonnegative integral values survive; bools and junk clamp to zero."""
    run_rows(
        [
            eq_row("zero", lambda: nonnegative_int(0), 0),
            eq_row("int", lambda: nonnegative_int(3), 3),
            eq_row("whole_float", lambda: nonnegative_int(3.0), 3),
            eq_row("fractional", lambda: nonnegative_int(3.5), 0),
            eq_row("negative_int", lambda: nonnegative_int(-1), 0),
            eq_row("negative_float", lambda: nonnegative_int(-1.0), 0),
            eq_row("boolean", lambda: nonnegative_int(True), 0),
            eq_row("nan", lambda: nonnegative_int(float("nan")), 0),
            eq_row("inf", lambda: nonnegative_int(float("inf")), 0),
        ]
    )


def test_finite_float_rejects_booleans_nonfinite_and_overflow():
    """Finite numbers pass; bools, nan, inf and overflowing ints drop to None."""
    run_rows(
        [
            eq_row("zero", lambda: finite_float(0), 0.0),
            eq_row("float", lambda: finite_float(3.5), 3.5),
            eq_row("boolean", lambda: finite_float(True), None),
            eq_row("nan", lambda: finite_float(float("nan")), None),
            eq_row("inf", lambda: finite_float(float("inf")), None),
            eq_row("overflow", lambda: finite_float(10**1000), None),
        ]
    )


def test_trajectory_status_normalizes_aliases_and_retains_default():
    """Aliases map to canonical statuses; unknowns and None retain the default."""
    run_rows(
        [
            is_row(
                "success",
                lambda: trajectory_status("success", TrajectoryStatus.UNKNOWN),
                TrajectoryStatus.COMPLETED,
            ),
            is_row(
                "in_progress",
                lambda: trajectory_status("in-progress", TrajectoryStatus.UNKNOWN),
                TrajectoryStatus.RUNNING,
            ),
            is_row(
                "canceled",
                lambda: trajectory_status("canceled", TrajectoryStatus.UNKNOWN),
                TrajectoryStatus.CANCELLED,
            ),
            is_row(
                "unknown_keeps_default",
                lambda: trajectory_status("missing", TrajectoryStatus.PARTIAL),
                TrajectoryStatus.PARTIAL,
            ),
            is_row(
                "none_keeps_default",
                lambda: trajectory_status(None, TrajectoryStatus.PARTIAL),
                TrajectoryStatus.PARTIAL,
            ),
        ]
    )


def test_iso_epoch_parses_shared_iso_timestamps():
    """Z and offset forms parse to the same epoch; junk and None drop to None."""
    run_rows(
        [
            eq_row("zulu", lambda: iso_epoch("2026-08-27T12:00:00Z"), 1_787_832_000.0),
            eq_row("offset", lambda: iso_epoch("2026-08-27T14:00:00+02:00"), 1_787_832_000.0),
            eq_row("not_a_time", lambda: iso_epoch("not-a-time"), None),
            eq_row("none", lambda: iso_epoch(None), None),
        ]
    )


def test_content_blocks_text():
    """Flatten block lists, pass strings through, JSON-fallback dicts, None → empty."""
    run_rows(
        [
            eq_row(
                "flattens_block_list",
                lambda: content_blocks_text([{"text": "a"}, {"text": "b"}]),
                "ab",
            ),
            eq_row("passes_through_string", lambda: content_blocks_text("hello"), "hello"),
            eq_row("falls_back_to_json", lambda: content_blocks_text({"k": "v"}), '{"k":"v"}'),
            eq_row("none_returns_empty", lambda: content_blocks_text(None), ""),
        ]
    )


def test_loose_trajectory_text():
    """Dicts and lists JSON-dump (keys sorted); strings pass through; others → empty."""
    run_rows(
        [
            eq_row(
                "json_dumps_dict",
                lambda: loose_trajectory_text({"b": 2, "a": 1}),
                '{"a": 1, "b": 2}',
            ),
            eq_row("json_dumps_list", lambda: loose_trajectory_text([1, 2]), "[1, 2]"),
            eq_row("string_passthrough", lambda: loose_trajectory_text("hello"), "hello"),
            eq_row("other_types_return_empty", lambda: loose_trajectory_text(42), ""),
        ]
    )


def test_optional_trajectory_detail():
    """None and empty → None; text, dict and int values carry a preview."""

    def text_value() -> None:
        field = optional_trajectory_detail("x", "hello")
        assert field is not None
        assert field.preview.text == "hello"

    def dict_value() -> None:
        field = optional_trajectory_detail("x", {"a": 1}, format=ContentFormat.JSON)
        assert field is not None
        assert field.preview.text == '{"a": 1}'

    def int_value() -> None:
        field = optional_trajectory_detail("x", 42)
        assert field is not None
        assert field.preview.text == "42"

    run_rows(
        [
            is_row("none_returns_none", lambda: optional_trajectory_detail("x", None), None),
            is_row("empty_returns_none", lambda: optional_trajectory_detail("x", ""), None),
            ("text", text_value),
            ("dict", dict_value),
            ("int", int_value),
        ]
    )


def test_trajectory_identifier():
    """Overflow drops to None without a prefix, hashes with one; under-limit unchanged."""
    big = "x" * (TRAJECTORY_IDENTIFIER_MAX_BYTES + 1)

    def overflow_prefix_on() -> None:
        result = trajectory_identifier(big, overflow_prefix="vibe")
        assert result is not None
        assert result.startswith("vibe:")
        assert len(result) > len("vibe:")

    run_rows(
        [
            is_row("overflow_prefix_off_drops_to_none", lambda: trajectory_identifier(big), None),
            ("overflow_prefix_on_returns_hash", overflow_prefix_on),
            eq_row("under_limit_unchanged", lambda: trajectory_identifier("abc"), "abc"),
            eq_row(
                "under_limit_with_prefix_unchanged",
                lambda: trajectory_identifier("abc", overflow_prefix="vibe"),
                "abc",
            ),
        ]
    )


def test_decode_json_record():
    """Valid, whitespace-stripped and bytes decode; empty/invalid/non-dict → None."""
    run_rows(
        [
            eq_row("valid", lambda: decode_json_record('{"a": 1}'), {"a": 1}),
            eq_row("strips_whitespace", lambda: decode_json_record('  {"a": 1}  '), {"a": 1}),
            is_row("empty_returns_none", lambda: decode_json_record(""), None),
            is_row("whitespace_only_returns_none", lambda: decode_json_record("   "), None),
            is_row("invalid_json_returns_none", lambda: decode_json_record("not json"), None),
            is_row("non_dict_returns_none", lambda: decode_json_record("[1, 2]"), None),
            eq_row("bytes", lambda: decode_json_record(b'{"a": 1}'), {"a": 1}),
        ]
    )


def test_first_key():
    """First present key wins; absent → None; coerce applies; None values skipped."""
    run_rows(
        [
            eq_row("returns_first_present", lambda: first_key({"b": 2, "a": 1}, "a", "b"), 1),
            is_row("returns_none_when_absent", lambda: first_key({}, "a"), None),
            eq_row("with_coerce", lambda: first_key({"a": "3"}, "a", coerce=int), 3),
            eq_row("skips_none_values", lambda: first_key({"a": None, "b": 2}, "a", "b"), 2),
        ]
    )


def test_first_key_of():
    """Mappings are searched in order; every mapping absent → None."""
    run_rows(
        [
            eq_row(
                "searches_multiple_mappings", lambda: first_key_of(({"x": 1}, {"y": 2}), ("y",)), 2
            ),
            is_row("returns_none_when_all_absent", lambda: first_key_of(({"a": 1},), ("b",)), None),
        ]
    )


def test_revision_from():
    """Revision wins over version; first mapping wins; absent or explicit zero stays zero."""
    run_rows(
        [
            eq_row("finds_revision", lambda: revision_from({"revision": 3}), 3),
            eq_row("finds_version", lambda: revision_from({"version": 5}), 5),
            eq_row(
                "prefers_first_mapping", lambda: revision_from({"revision": 1}, {"revision": 2}), 1
            ),
            eq_row("returns_zero_when_absent", lambda: revision_from({}), 0),
            eq_row("accepts_zero", lambda: revision_from({"revision": 0}), 0),
        ]
    )


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


def test_reported_cost():
    """Strict-positive drops zero; negatives and non-numerics always drop to UNKNOWN."""
    run_rows(
        [
            eq_row(
                "strict_positive_drops_zero",
                lambda: reported_cost(0.0, strict_positive=True),
                (None, CostProvenance.UNKNOWN),
            ),
            eq_row(
                "strict_positive_accepts_positive",
                lambda: reported_cost(1.5, strict_positive=True),
                (1.5, CostProvenance.REPORTED),
            ),
            eq_row(
                "not_strict_positive_accepts_zero",
                lambda: reported_cost(0.0, strict_positive=False),
                (0.0, CostProvenance.REPORTED),
            ),
            eq_row(
                "drops_negative",
                lambda: reported_cost(-1.0, strict_positive=False),
                (None, CostProvenance.UNKNOWN),
            ),
            eq_row(
                "drops_non_numeric",
                lambda: reported_cost("free", strict_positive=True),
                (None, CostProvenance.UNKNOWN),
            ),
        ]
    )


def test_qualified_model():
    """Provider joins the model; missing provider passes through; both empty → None."""

    def empty_returns_none() -> None:
        assert qualified_model(None, None) is None
        assert qualified_model("", "") is None

    run_rows(
        [
            eq_row(
                "joins_provider_and_model",
                lambda: qualified_model("openai", "gpt-4"),
                "openai/gpt-4",
            ),
            eq_row("model_only", lambda: qualified_model(None, "gpt-4"), "gpt-4"),
            ("empty_returns_none", empty_returns_none),
        ]
    )


def test_epoch_or_number():
    """ISO strings parse to epochs; numbers pass through; junk and None → None."""
    run_rows(
        [
            eq_row("iso_string", lambda: epoch_or_number("2026-08-27T12:00:00Z"), 1787832000.0),
            eq_row("float", lambda: epoch_or_number(1000.5), 1000.5),
            eq_row("int", lambda: epoch_or_number(42), 42.0),
            is_row("invalid_string", lambda: epoch_or_number("not-a-time"), None),
            is_row("none", lambda: epoch_or_number(None), None),
        ]
    )


def test_assemble_timing():
    """All-None → None; ordering and duration fill rules; first-token bounds."""

    def end_before_start_drops_end() -> None:
        result = assemble_timing(10.0, 5.0, None, provenance=TimingProvenance.SOURCE)
        assert result is not None
        assert result.start == 10.0
        assert result.end is None

    def fills_missing_end_from_duration() -> None:
        result = assemble_timing(10.0, None, 5000.0, provenance=TimingProvenance.SOURCE)
        assert result is not None
        assert result.start == 10.0
        assert result.end == 15.0
        assert result.duration_ms == 5000.0

    def fills_missing_start_from_duration() -> None:
        result = assemble_timing(None, 15.0, 5000.0, provenance=TimingProvenance.SOURCE)
        assert result is not None
        assert result.start == 10.0
        assert result.end == 15.0
        assert result.duration_ms == 5000.0

    def fills_missing_duration() -> None:
        result = assemble_timing(10.0, 15.0, None, provenance=TimingProvenance.SOURCE)
        assert result is not None
        assert result.duration_ms == 5000.0

    def first_token_after_end_dropped() -> None:
        result = assemble_timing(
            10.0, 15.0, None, first_token=20.0, provenance=TimingProvenance.SOURCE
        )
        assert result is not None
        assert result.first_token is None

    def first_token_within_bounds() -> None:
        result = assemble_timing(
            10.0, 15.0, None, first_token=12.0, provenance=TimingProvenance.SOURCE
        )
        assert result is not None
        assert result.first_token == 12.0

    run_rows(
        [
            is_row(
                "all_none_returns_none",
                lambda: assemble_timing(None, None, None, provenance=TimingProvenance.SOURCE),
                None,
            ),
            ("end_before_start_drops_end", end_before_start_drops_end),
            ("fills_missing_end_from_duration", fills_missing_end_from_duration),
            ("fills_missing_start_from_duration", fills_missing_start_from_duration),
            ("fills_missing_duration", fills_missing_duration),
            ("first_token_after_end_dropped", first_token_after_end_dropped),
            ("first_token_within_bounds", first_token_within_bounds),
        ]
    )


def test_lane_for_kind():
    """ERROR routes to the theater lane; the kind table has no accidents."""
    run_rows(
        [
            is_row(
                "error_routes_to_theater",
                lambda: lane_for_kind(TrajectoryKind.ERROR),
                TrajectoryLane.THEATER,
            ),
            is_row(
                "user_routes_to_input",
                lambda: lane_for_kind(TrajectoryKind.USER),
                TrajectoryLane.INPUT,
            ),
            is_row(
                "tool_call_routes_to_tools",
                lambda: lane_for_kind(TrajectoryKind.TOOL_CALL),
                TrajectoryLane.TOOLS,
            ),
            is_row(
                "tool_result_routes_to_tools",
                lambda: lane_for_kind(TrajectoryKind.TOOL_RESULT),
                TrajectoryLane.TOOLS,
            ),
            is_row(
                "assistant_routes_to_model",
                lambda: lane_for_kind(TrajectoryKind.ASSISTANT),
                TrajectoryLane.MODEL,
            ),
            is_row(
                "usage_routes_to_model",
                lambda: lane_for_kind(TrajectoryKind.USAGE),
                TrajectoryLane.MODEL,
            ),
        ]
    )


def test_tool_failure():
    """Non-error statuses carry no failure; errors do, with the detail verbatim."""

    def failure_when_error() -> None:
        result = tool_failure(TrajectoryStatus.ERROR, "boom")
        assert result is not None
        assert result.category is TrajectoryFailureCategory.TOOL
        assert result.detail == "boom"

    run_rows(
        [
            is_row(
                "returns_none_when_not_error",
                lambda: tool_failure(TrajectoryStatus.COMPLETED, "ok"),
                None,
            ),
            ("returns_failure_when_error", failure_when_error),
        ]
    )


def test_path_details():
    """Event paths become named PATH-format fields; empty stays empty."""

    def builds_path_fields() -> None:
        paths = (EventPath(path="src/main.py", mode="write"),)
        result = path_details(paths)
        assert len(result) == 1
        assert result[0].name == "path.write"
        assert result[0].format is ContentFormat.PATH

    run_rows(
        [
            ("builds_path_fields", builds_path_fields),
            eq_row("empty", lambda: path_details(()), ()),
        ]
    )


def _identity(value):
    return value if isinstance(value, str) and value else None


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
