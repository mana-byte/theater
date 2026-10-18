"""Helpers shared across a split test module."""

from __future__ import annotations

import json

from tests.support.paths import fixture

FIXTURE = fixture("claude_hooks", "captured_2.1.220.json")


def _captured() -> dict:
    return json.loads(FIXTURE.read_text())
