"""Which harnesses can be spawned, and under which approval."""

from __future__ import annotations

from regie.spawn import SpawnChoice, spawn_approval


def test_spawn_policy_prefers_manual_or_the_only_advertised_policy() -> None:
    assert spawn_approval(SpawnChoice("codex", True, approvals=("yolo", "manual"))) == "manual"
    assert spawn_approval(SpawnChoice("pi", True, approvals=("yolo",))) == "yolo"
    assert spawn_approval(SpawnChoice("future", True, approvals=("edits", "yolo"))) is None
