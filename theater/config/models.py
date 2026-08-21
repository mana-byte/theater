"""Configuration dataclasses and user-facing defaults.

The shape of every section, the field defaults a user sees when they run
`theater config` with no file, and the section registry (`_SECTIONS`) that
drives both parsing and the unknown-section check. Adding a section here is
the only edit needed to make it legal; `_SECTIONS` is derived from these
dataclasses rather than written out a second time, so a new setting cannot be
added without its validation.

`[models]` and `[reasoning]` are kept out of `_SECTIONS` and named by
`MODELS_SECTION` / `REASONING_SECTION`: their keys are harness names, so the
legal set is whatever is registered rather than anything this module can
enumerate. Their shape is validated in `validation.py`, and the names they list
are checked against the registry by the daemon at start-up — the same split
`theater.favourite` uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from theater.constants.limits import MIN_INTERVAL


@dataclass(frozen=True, slots=True)
class TheaterSection:
    #: Default harness for `theater spawn`; existence checked at daemon start-up.
    favourite: str | None = None


@dataclass(frozen=True, slots=True)
class RailsSection:
    #: Roots are depth 0. See daemon/rails.py.
    depth_cap: int = field(default=3, metadata={"min": 0})
    #: Maximum participants a single tree may hold.
    budget: int = field(default=20, metadata={"min": 1})


@dataclass(frozen=True, slots=True)
class ObserverSection:
    #: Faster than the reaper: drives régie rendering, where a second of lag is visible.
    poll_interval: float = field(default=0.25, metadata={"min": MIN_INTERVAL})
    #: No new bytes before re-locating the transcript. Vibe rotates its session dir per turn.
    relocate_timeout: float = field(default=5.0, metadata={"min": MIN_INTERVAL})
    #: No transcript growth before checking the screen for a bare prompt.
    awaiting_input_timeout: float = field(default=1.5, metadata={"min": MIN_INTERVAL})
    #: Backstop for a missed turn boundary; much longer than awaiting_input.
    rescue_timeout: float = field(default=60.0, metadata={"min": MIN_INTERVAL})
    #: Slower: a directory scan rather than a stat.
    search_interval: float = field(default=2.0, metadata={"min": MIN_INTERVAL})
    #: For no-transcript harnesses, the screen is the only turn-end evidence.
    screen_interval: float = field(default=1.0, metadata={"min": MIN_INTERVAL})
    #: How often to reconcile watch tasks against the registry.
    sync_interval: float = field(default=1.0, metadata={"min": MIN_INTERVAL})


@dataclass(frozen=True, slots=True)
class RetentionSection:
    #: Bus events are the fire: 94% of the file, 7.1 MB/day; nothing reads a week-old bus event.
    bus_days: int = field(default=7, metadata={"min": 1})
    #: Two weeks. Beyond that the code has moved and the transcript is off disk.
    jobs_days: int = field(default=15, metadata={"min": 1})
    #: `send.refused` is the only record of a refused send, so it is capped by count, not aged out.
    refused_cap: int = field(default=10000, metadata={"min": 1})
    #: Abandoned running jobs (finished_at = NULL) become immortal; 7d catches prev-daemon jobs.
    stale_running_days: int = field(default=7, metadata={"min": 1})
    #: Rows per DELETE statement so no single sweep blocks the event loop.
    batch: int = field(default=5000, metadata={"min": 1})
    #: Seconds between sweeps; the database is bounded without the user configuring anything.
    interval: float = field(default=3600.0, metadata={"min": MIN_INTERVAL})
    #: Default ON. The database is bounded without the user configuring anything.
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class HarnessSection:
    #: A denylist, not an allowlist. Matched against the file stem before import.
    disabled: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RegieSection:
    #: Not validated here: importing Textual's legal names would pull the TUI stack in.
    theme: str | None = None
    #: How often to refresh the participant tree.
    tree_interval: float = field(default=1.0, metadata={"min": MIN_INTERVAL})
    #: How often to poll the bus for new events.
    bus_interval: float = field(default=0.4, metadata={"min": MIN_INTERVAL})
    #: Events pulled per bus poll.
    bus_batch: int = field(default=50, metadata={"min": 1})
    #: Trailing cwd segments the tree keeps; applied after ``tilde()``. Minimum 1.
    cwd_segments: int = field(default=2, metadata={"min": 1})
    #: Read once, used twice (#sidebar style and resize_pane); below 40 they don't fit.
    sidebar_width: int = field(default=52, metadata={"min": 40})
    #: Off by default: the tree is what the régie is for. While hidden the bus is not polled at all.
    bus_visible: bool = False
    #: Which cost window the price footer shows: "day", "week", "month", or "year".
    cost_window: str = "day"


#: Section name -> dataclass. Drives parsing and the unknown-section check.
_SECTIONS: dict[str, type] = {
    "theater": TheaterSection,
    "rails": RailsSection,
    "observer": ObserverSection,
    "retention": RetentionSection,
    "harness": HarnessSection,
    "regie": RegieSection,
}

#: Keys are harness names; the legal set depends on the registry. Parsed by `_build_models`.
MODELS_SECTION = "models"

#: Same shape as `[models]`, keyed by harness name. Parsed by `_build_reasoning`.
REASONING_SECTION = "reasoning"


@dataclass(frozen=True, slots=True)
class Config:
    """Resolved settings: file values over defaults, nothing else."""

    theater: TheaterSection = field(default_factory=TheaterSection)
    rails: RailsSection = field(default_factory=RailsSection)
    observer: ObserverSection = field(default_factory=ObserverSection)
    retention: RetentionSection = field(default_factory=RetentionSection)
    harness: HarnessSection = field(default_factory=HarnessSection)
    regie: RegieSection = field(default_factory=RegieSection)
    #: Harness name -> models `spawn --model` may name. An allowlist; empty means no selection.
    models: dict[str, list[str]] = field(default_factory=dict)
    #: Harness name -> reasoning efforts `spawn --reasoning-effort` may name. An allowlist.
    reasoning: dict[str, list[str]] = field(default_factory=dict)
    #: Dotted key -> "default" | "config.toml". Did the edit take effect?
    sources: dict[str, str] = field(default_factory=dict)
    #: Where the file would be, whether or not it is there.
    path: Path | None = None
    exists: bool = False

    def source(self, dotted: str) -> str:
        return self.sources.get(dotted, "default")

    def models_for(self, harness: str) -> list[str]:
        """The allowlist for one harness. Empty means no model may be named."""
        return self.models.get(harness, [])

    def reasoning_for(self, harness: str) -> list[str]:
        """The reasoning-effort allowlist for one harness. Empty means none."""
        return self.reasoning.get(harness, [])
