"""Paths projected from the file effects real Vibe 3.x writes."""

from theater.harness.builtin.plugins.vibe.unified_projection import project_unified_entry
from theater.harness.contracts.events import EventKind


def test_write_effect_event_carries_the_written_path() -> None:
    # Shape exported by stock Vibe 3.0.2/3.0.3: a generic tool effect whose
    # detail carries the runtime tool name and the written file's path.
    entry = {
        "type": "effect",
        "id": "effect-1",
        "sessionId": "sess-1",
        "turnId": "turn-1",
        "createdAt": 2_100,
        "updatedAt": 2_200,
        "generationStatus": "completed",
        "relatedEntryId": None,
        "title": "file_system.write_file",
        "detail": {
            "kind": "tool",
            "toolName": "file_system.write_file",
            "input": {"path": "/tmp/work/result.txt", "content": "done\n"},
            "display": {
                "summary": "file_system.write_file",
                "verb": "Running",
                "message": "file_system.write_file",
                "settledVerb": "Ran",
                "settledMessage": "file_system.write_file",
                "statusText": "Running file_system.write_file",
            },
        },
        "state": {
            "status": "completed",
            "output": {"bytes_written": 5, "file_existed": False, "path": "/tmp/work/result.txt"},
            "outputText": "",
            "durationMs": 100,
        },
    }
    parsed = project_unified_entry(
        entry, identity="effect-1", index=0, watermark=1, source_sequence=1, cwd="/tmp/work"
    )
    call = parsed.events[0]
    assert call.kind is EventKind.TOOL_CALL
    assert [(path.path, path.mode) for path in call.paths] == [("result.txt", "write")]
