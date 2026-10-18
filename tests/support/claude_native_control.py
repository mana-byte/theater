"""Pinned fixture record and opt-in switches shared by the Claude native-control tests."""

from __future__ import annotations

from tests.support.paths import fixture

FIXTURES = fixture("claude_native_control")
RECORD = FIXTURES / "messaging_conformance.json"
