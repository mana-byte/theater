"""Test factories for shipped package manifests."""

from __future__ import annotations

from pathlib import Path

from theater.harness.builtin.plugins.claude.manifest import (
    MANIFEST as CLAUDE_MANIFEST,
)
from theater.harness.builtin.plugins.claude.manifest import (
    manifest_for_root as claude_manifest_for_root,
)
from theater.harness.builtin.plugins.claude.observer import (
    ClaudeCodeObserver as _ClaudeCodeObserver,
)
from theater.harness.builtin.plugins.codex.manifest import MANIFEST as CODEX_MANIFEST
from theater.harness.builtin.plugins.codex.manifest import (
    manifest_for_root as codex_manifest_for_root,
)
from theater.harness.builtin.plugins.codex.observer import CodexObserver as _CodexObserver
from theater.harness.builtin.plugins.opencode.manifest import manifest_for_paths
from theater.harness.builtin.plugins.opencode.observer import OpenCodeObserver as _OpenCodeObserver
from theater.harness.builtin.plugins.vibe.manifest import manifest_for_roots
from theater.harness.builtin.plugins.vibe.observer import VibeObserver as _VibeObserver
from theater.harness.contracts.harness import Harness
from theater.harness.manifests import compile_manifest


def _claude_harness(root: Path | None = None) -> Harness:
    manifest = CLAUDE_MANIFEST if root is None else claude_manifest_for_root(root)
    return compile_manifest("claude", manifest)


def _codex_harness(root: Path | None = None) -> Harness:
    manifest = CODEX_MANIFEST if root is None else codex_manifest_for_root(root)
    return compile_manifest("codex", manifest)


def _opencode_harness(
    db: Path | None = None,
    correlation_dir: Path | None = None,
) -> Harness:
    return compile_manifest("opencode", manifest_for_paths(db=db, correlation_dir=correlation_dir))


def _vibe_harness(
    root: Path | None = None,
    correlation_root: Path | None = None,
    *,
    isolated: bool = False,
) -> Harness:
    return compile_manifest(
        "vibe",
        manifest_for_roots(root, correlation_root, isolated=isolated),
    )


ClaudeCodeHarness = _claude_harness
ClaudeCodeObserver = _ClaudeCodeObserver
CodexHarness = _codex_harness
CodexObserver = _CodexObserver
OpenCodeHarness = _opencode_harness
OpenCodeHarness.resume_takes_prompt = False  # type: ignore[attr-defined]
OpenCodeObserver = _OpenCodeObserver
VibeHarness = _vibe_harness
VibeHarness.resume_strategy = "continue"  # type: ignore[attr-defined]
VibeObserver = _VibeObserver
