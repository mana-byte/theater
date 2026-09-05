"""User configuration: `$THEATER_HOME/config.toml`.

Read-only, machine-scoped, read once at daemon start. Each of those three is a
decision rather than an omission:

**Read-only.** Theater parses this file and never writes it. Round-tripping
TOML needs a third-party serializer, and a config the tool rewrites is a config
whose comments and key ordering do not survive first contact. `theater config`
therefore *shows* the resolved values and points at the file; the user edits it.

**Machine-scoped.** There is no project-local `.theater/config.toml`. One
daemon per machine holds one registry, so a per-project file would make the
harness set depend on which directory the daemon happened to start in — not a
property the daemon has.

**No hot reload.** `theater restart` applies changes. Watching the file would
mean a harness set that mutates under a running observer, and the failure mode
of a stale value is a puzzle rather than an error.

Unknown keys are a hard failure
-------------------------------
A misspelled key that is silently ignored leaves the user believing they
configured something the daemon never saw. That is the same defect class as the
v1.2 schema no-op, and it is worth an abort at start-up: `load()` raises
`ConfigError` naming the file, the offending key, and the closest legal
spelling.

The known-key set is derived from the dataclasses in this package rather than
written out a second time, so a new setting cannot be added without its
validation.

`[models]` is the one exception, and has to be: its keys are harness names, so
the legal set is whatever is registered rather than anything this module can
enumerate. Its shape is validated by `_build_models`, and the names it lists are
checked against the registry by the daemon at start-up — the same split
`theater.favourite` uses.

What is deliberately not settable
---------------------------------
The default approval mode. See `harness/base.py:80`: there is no default
anywhere because the choice is the whole safety story for a child nobody is
watching. A key pinning it to `yolo` once and forever removes that by design.
"""

from __future__ import annotations

from theater.config.describe import describe
from theater.config.load import load
from theater.config.models import (
    _SECTIONS,
    MCP_SECTION,
    MODELS_SECTION,
    REASONING_SECTION,
    Config,
    HarnessSection,
    McpSection,
    ObservabilitySection,
    ObserverSection,
    RailsSection,
    RegieSection,
    RetentionSection,
    SkillsSection,
    TheaterSection,
)
from theater.config.validation import ConfigError
from theater.constants import HARNESS_NAME, MIN_INTERVAL

__all__ = [
    "HARNESS_NAME",
    "MCP_SECTION",
    "MIN_INTERVAL",
    "MODELS_SECTION",
    "REASONING_SECTION",
    "_SECTIONS",
    "Config",
    "ConfigError",
    "HarnessSection",
    "McpSection",
    "ObservabilitySection",
    "ObserverSection",
    "RailsSection",
    "RegieSection",
    "RetentionSection",
    "SkillsSection",
    "TheaterSection",
    "describe",
    "load",
]
