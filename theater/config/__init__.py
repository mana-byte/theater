"""User config ``$THEATER_HOME/config.toml``: read-only, machine-scoped, read once at start.
Unknown keys are fatal (an ignored typo is worse than an abort); ``[models]`` is checked by the
daemon. The default approval mode is deliberately not settable.
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
    RetentionSection,
    ScratchpadSection,
    SkillsSection,
    TerminalsSection,
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
    "RetentionSection",
    "ScratchpadSection",
    "SkillsSection",
    "TerminalsSection",
    "TheaterSection",
    "describe",
    "load",
]
