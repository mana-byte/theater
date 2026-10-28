"""Paths projected from the file effects real Vibe writes."""

from tests.support.vibe_store import public_effect
from theater.harness.builtin.plugins.vibe.unified_projection import project_unified_entry
from theater.harness.contracts.events import EventKind


def test_write_effect_event_carries_the_written_path() -> None:
    parsed = project_unified_entry(
        public_effect(),
        identity="effect-1",
        index=0,
        watermark=1,
        source_sequence=1,
        cwd="/tmp/work",
    )
    call = parsed.events[0]
    assert call.kind is EventKind.TOOL_CALL
    assert [(path.path, path.mode) for path in call.paths] == [("result.txt", "write")]
