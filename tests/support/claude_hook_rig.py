"""The captured Claude hook payloads shared by the hook decode and identity tests."""

from __future__ import annotations

import json

from tests.support.paths import fixture

FIXTURE = fixture("claude_hooks", "captured_2.1.220.json")


def captured_hooks() -> dict:
    return json.loads(FIXTURE.read_text())
