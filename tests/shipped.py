"""The shipped harness adapters, as classes, for tests that need their own.

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

from theater.harness import builtin, plugins


def harness_class(stem: str) -> type:
    """The adapter class defined by the shipped plugin file `<stem>.py`."""
    for found in plugins.scan(builtin.plugin_dir(), source=plugins.SHIPPED):
        if found.path.stem == stem:
            assert found.harness is not None, found.error
            return type(found.harness)
    raise AssertionError(f"no shipped harness plugin named {stem!r}")


ClaudeCodeHarness = harness_class("claude")
CodexHarness = harness_class("codex")
VibeHarness = harness_class("vibe")
