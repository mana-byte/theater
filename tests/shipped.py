"""The shipped adapters, as constructors, for tests that need their own instance.

The adapters live in plugin files loaded by path rather than importable modules
— that is the point of shipping them through the extension point (see
`theater/harness/plugins.py`). So there is no `from theater.harness.vibe import
VibeHarness` to write, and tests that need an instance pointed at a temporary
transcript root have to go through the loader, which is how the daemon gets its
too.

Note that these classes are *not* the ones behind `HARNESSES`: the registry ran
the same files through its own load. Nothing compares adapters by identity, and
the alternative — reaching into `_PLUGINS` — would couple every parser test to
the registry's internals.
"""

from __future__ import annotations

from pathlib import Path

from theater.harness import builtin, loading, plugins
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
from theater.harness.manifests.compiler import compile_manifest


def harness_class(stem: str) -> type:
    """The adapter class loaded for the shipped plugin ``stem``."""
    for found in loading.scan(builtin.plugin_dir(), source=loading.SHIPPED):
        if found.name == stem and found.harness is not None:
            return type(found.harness)
    for found in plugins.scan(builtin.plugin_dir(), source=plugins.SHIPPED):
        if found.path.stem == stem:
            assert found.harness is not None, found.error
            return type(found.harness)
    raise AssertionError(f"no shipped harness plugin named {stem!r}")


def observer_class(stem: str) -> type:
    """The observer class carried by the shipped plugin ``stem``.

    Taken off a default-constructed harness rather than looked up by name in
    the plugin module, because the pairing is the thing under test everywhere
    this is used: whatever `Harness.observer` actually is, is what the daemon
    will watch with. Constructing the harness costs nothing — every shipped
    `__init__` computes paths and touches no disk — and the instance is
    discarded; tests build their own observer with a temporary root.
    """
    return type(harness_class(stem)().observer)


def _claude_harness(root: Path | None = None):
    manifest = CLAUDE_MANIFEST if root is None else claude_manifest_for_root(root)
    return compile_manifest("claude", manifest)


ClaudeCodeHarness = _claude_harness

def CodexHarness(root: Path | None = None):  # noqa: N802
    manifest = CODEX_MANIFEST if root is None else codex_manifest_for_root(root)
    return compile_manifest("codex", manifest)


OpenCodeHarness = harness_class("opencode")
VibeHarness = harness_class("vibe")

ClaudeCodeObserver = _ClaudeCodeObserver
CodexObserver = _CodexObserver
OpenCodeObserver = observer_class("opencode")
VibeObserver = observer_class("vibe")
