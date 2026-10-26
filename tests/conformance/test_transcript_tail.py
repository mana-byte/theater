"""A failed resume must say where its records sit relative to the launch, in a few hundred bytes."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from tests.support.transcript_tail import transcript_tail

LAUNCHED = datetime(2026, 10, 26, 0, 0, 1, tzinfo=UTC).timestamp()


def _write(path: Path, *lines: object) -> str:
    path.write_text(
        "\n".join(line if isinstance(line, str) else json.dumps(line) for line in lines)
    )
    return str(path)


def test_each_record_shows_its_kind_ids_and_seconds_from_launch(tmp_path: Path) -> None:
    location = _write(
        tmp_path / "s.jsonl",
        {"type": "user", "uuid": "aaaa1111", "timestamp": "2026-10-26T00:00:00.000Z"},
        {
            "type": "system",
            "subtype": "turn_duration",
            "uuid": "bbbb2222",
            "parentUuid": "aaaa1111",
            "timestamp": "2026-10-26T00:00:03.500Z",
        },
    )
    assert transcript_tail(location, launched_at=LAUNCHED) == (
        "2 lines, last 2 records: user aaaa<- @-1.0s | system/turn_duration bbbb<aaaa @+2.5s"
    )


def test_metadata_lines_take_no_slot_and_odd_input_never_hides_the_rest(tmp_path: Path) -> None:
    location = _write(
        tmp_path / "s.jsonl",
        {"type": "user", "uuid": "old"},
        "not json",
        "[1]",
        {"type": "mode", "mode": "normal"},
        {"type": "assistant", "uuid": "cccc3333", "timestamp": "not a time"},
        {"type": "last-prompt", "lastPrompt": "x"},
    )
    assert transcript_tail(location, launched_at=None, records=3) == (
        "6 lines, last 3 records: ? | ? | assistant cccc<-"
    )
    assert transcript_tail(None, launched_at=None) == "no transcript file"
    assert transcript_tail(str(tmp_path / "missing"), launched_at=LAUNCHED) == "no transcript file"
