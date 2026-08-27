from __future__ import annotations

import asyncio
import json
from pathlib import Path

from theater.harness.builtin.plugins.vibe.source import _VibeSource
from theater.harness.source import Batch, Source, TranscriptSource
from theater.trajectory.enums import CostProvenance, TrajectoryKind


class FakeTranscriptSource(Source):
    def __init__(self, path: Path):
        self.path = path
        self.collision_domain = str(path.parent)
        self.batch = Batch()
        self.commits = 0

    async def read(self) -> Batch:
        return self.batch

    def commit_attachment(self) -> None:
        self.commits += 1

    def discard_attachment(self) -> None:
        pass

    def revoke_attachment(self) -> None:
        self.path = None

    def admit_exact_location(self, *, location: str, session_id: str):
        self.path = Path(location)
        return "staged"


def _write_meta(path: Path, prompt: object, completion: object, cached: object, **extra) -> None:
    stats = {
        "session_prompt_tokens": prompt,
        "session_completion_tokens": completion,
        "session_cached_tokens": cached,
        **extra.pop("stats", {}),
    }
    path.write_text(json.dumps({"stats": stats, **extra}))


def _source(tmp_path: Path, *, cold: bool = False):
    session = tmp_path / "session"
    session.mkdir()
    messages = session / "messages.jsonl"
    messages.write_text("")
    inner = FakeTranscriptSource(messages)
    source = _VibeSource(
        inner,  # type: ignore[arg-type]
        after=0.0 if cold else None,
        session_id=None,
        known_location=None,
    )
    return source, inner, session / "meta.json"


def _usage(source: _VibeSource):
    events = asyncio.run(source.read()).events
    return events[0].usage if events else None


def test_resume_baselines_then_emits_only_monotonic_delta(tmp_path):
    source, _inner, meta = _source(tmp_path)
    _write_meta(meta, 10, 3, 4)
    assert _usage(source) is None

    _write_meta(meta, 17, 8, 6)
    usage = _usage(source)

    assert usage is not None
    assert usage.input_tokens == 5
    assert usage.output_tokens == 5
    assert usage.cache_read_input_tokens == 2
    assert usage.idempotency_key == "vibe:10:3:4->17:8:6"


def test_new_launch_counts_initial_usage_and_prices_like_vibe(tmp_path):
    source, _inner, meta = _source(tmp_path, cold=True)
    _write_meta(
        meta,
        10,
        3,
        4,
        stats={
            "input_price_per_million": 2.0,
            "output_price_per_million": 5.0,
            "cached_input_price_per_million": None,
        },
        config={
            "active_model": "FAST",
            "models": [{"alias": "fast", "name": "gpt-5", "provider": "openai"}],
        },
    )

    usage = _usage(source)

    assert usage is not None
    assert usage.model == "openai/gpt-5"
    assert usage.provider == "openai"
    assert usage.input_tokens == 6
    assert usage.cache_read_input_tokens == 4
    assert usage.cost_usd == 35 / 1_000_000
    assert usage.cost_provenance is CostProvenance.ESTIMATED


def test_usage_delta_is_also_available_to_trajectory(tmp_path):
    source, _inner, meta = _source(tmp_path, cold=True)
    _write_meta(
        meta,
        10,
        3,
        4,
        config={"active_model": "gpt-5"},
    )

    batch = asyncio.run(source.read())

    assert len(batch.trajectory) == 1
    fact = batch.trajectory[0]
    assert fact.kind is TrajectoryKind.USAGE
    assert fact.request_id is None
    assert fact.usage is not None
    assert fact.usage.request_id == "vibe:0:0:0->10:3:4"
    assert fact.usage.model == "gpt-5"
    assert fact.usage.input_tokens == 6
    assert fact.usage.output_tokens == 3
    assert fact.usage.cache_read_tokens == 4


def test_cached_tokens_are_clamped_to_prompt_tokens(tmp_path):
    source, _inner, meta = _source(tmp_path, cold=True)
    _write_meta(meta, 2, 1, 5)

    usage = _usage(source)

    assert usage is not None
    assert usage.input_tokens == 0
    assert usage.cache_read_input_tokens == 2


def test_malformed_meta_and_counters_do_not_move_the_baseline(tmp_path):
    source, _inner, meta = _source(tmp_path)
    _write_meta(meta, 10, 3, 4)
    assert _usage(source) is None

    meta.write_text("{")
    assert _usage(source) is None
    _write_meta(meta, 12, "partial", 5)
    assert _usage(source) is None
    _write_meta(meta, 12, 4, 5)

    usage = _usage(source)

    assert usage is not None
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_input_tokens) == (1, 1, 1)


def test_counter_regression_rebaselines_without_emitting(tmp_path):
    source, _inner, meta = _source(tmp_path)
    _write_meta(meta, 10, 3, 4)
    assert _usage(source) is None
    _write_meta(meta, 2, 1, 0)
    assert _usage(source) is None
    _write_meta(meta, 5, 2, 1)

    usage = _usage(source)

    assert usage is not None
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_input_tokens) == (2, 1, 1)


def test_accepted_rotation_preserves_cumulative_baseline(tmp_path):
    source, inner, meta = _source(tmp_path)
    _write_meta(meta, 10, 3, 4)
    assert _usage(source) is None

    rotated = tmp_path / "rotated"
    rotated.mkdir()
    inner.path = rotated / "messages.jsonl"
    inner.path.write_text("")
    rotated_meta = rotated / "meta.json"
    _write_meta(rotated_meta, 12, 4, 5)
    source.commit_attachment()

    usage = _usage(source)

    assert inner.commits == 1
    assert usage is not None
    assert usage.idempotency_key == "vibe:10:3:4->12:4:5"


def test_invalid_native_rates_fall_back_to_theater_pricing(tmp_path):
    source, _inner, meta = _source(tmp_path, cold=True)
    _write_meta(
        meta,
        1,
        1,
        0,
        stats={"input_price_per_million": 0, "output_price_per_million": 5},
        config={"active_model": "gpt-5"},
    )

    usage = _usage(source)

    assert usage is not None
    assert usage.cost_usd is None
    assert usage.model == "gpt-5"


def test_explicit_zero_cached_rate_means_free_cache(tmp_path):
    source, _inner, meta = _source(tmp_path, cold=True)
    _write_meta(
        meta,
        10,
        3,
        4,
        stats={
            "input_price_per_million": 2.0,
            "output_price_per_million": 5.0,
            "cached_input_price_per_million": 0,
        },
    )

    usage = _usage(source)

    assert usage is not None
    assert usage.input_tokens == 6
    assert usage.cache_read_input_tokens == 4
    assert usage.cost_usd == 27 / 1_000_000


def test_wrapper_preserves_the_public_transcript_source_surface(tmp_path):
    source, inner, _meta = _source(tmp_path)
    public = {name for name in dir(TranscriptSource) if not name.startswith("_")}
    # ``path`` is public instance state, so class introspection cannot find it.
    public.add("path")
    # TranscriptSource assigns collision_domain once. The wrapper safely copies
    # that immutable value instead of forwarding it dynamically.
    copied_not_delegated = {"collision_domain"}

    missing = {name for name in public - copied_not_delegated if not hasattr(source, name)}
    assert not missing
    assert source.collision_domain == inner.collision_domain
