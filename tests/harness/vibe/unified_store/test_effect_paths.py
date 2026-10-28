"""Paths projected from the file effects real Vibe writes."""

from theater.harness.builtin.plugins.vibe.unified_projection import project_unified_entry
from theater.harness.contracts.events import EventKind


def test_write_effect_event_carries_the_written_path() -> None:
    # Real stores persist builtin calls as generic tool effects: only the input
    # carries the written file's path.
    entry = {
        "type": "effect",
        "detail": {
            "kind": "tool",
            "toolName": "file_system.write_file",
            "input": {"path": "/tmp/work/result.txt", "content": "done\n"},
        },
    }
    parsed = project_unified_entry(
        entry, identity="effect-1", index=0, watermark=1, source_sequence=1, cwd="/tmp/work"
    )
    (call,) = parsed.events
    assert call.kind is EventKind.TOOL_CALL
    assert [(path.path, path.mode) for path in call.paths] == [("result.txt", "write")]
