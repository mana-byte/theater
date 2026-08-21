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
    #: Default harness for `theater spawn` and first in the régie palette.
    #: Type-checked here; existence against the registry at daemon start-up.
    favourite: str | None = None


@dataclass(frozen=True, slots=True)
class RailsSection:
    #: Roots are depth 0. See daemon/rails.py.
    depth_cap: int = field(default=3, metadata={"min": 0})
    #: Maximum participants a single tree may hold.
    budget: int = field(default=20, metadata={"min": 1})


@dataclass(frozen=True, slots=True)
class ObserverSection:
    #: Faster than the reaper: this drives what the régie renders, and a second
    #: of lag on "what is it doing" is visible to a human.
    poll_interval: float = field(default=0.25, metadata={"min": MIN_INTERVAL})
    #: No new bytes before re-locating the transcript. Vibe starts a new session
    #: directory each turn; the observer must re-scan to find it.
    relocate_timeout: float = field(default=5.0, metadata={"min": MIN_INTERVAL})
    #: No transcript growth before checking the screen for a bare prompt.
    #: Tuned long enough that a slow tool call will not trigger, short enough
    #: that a human watching the régie sees the change.
    awaiting_input_timeout: float = field(default=1.5, metadata={"min": MIN_INTERVAL})
    #: Backstop for a turn boundary the parser missed. Without it the caller's
    #: `await_sessions` blocks until its own deadline with no explanation. Much
    #: longer than awaiting_input_timeout: firing early hands back a half-written
    #: answer. A rescued job is marked `turn_end_unseen` so the caller can tell.
    rescue_timeout: float = field(default=60.0, metadata={"min": MIN_INTERVAL})
    #: Slower: a directory scan rather than a stat.
    search_interval: float = field(default=2.0, metadata={"min": MIN_INTERVAL})
    #: For harnesses with no transcript, the screen is the only evidence a turn
    #: ended. A turn is finished only when two consecutive polls agree, so this
    #: is also half the latency.
    screen_interval: float = field(default=1.0, metadata={"min": MIN_INTERVAL})
    #: How often to reconcile watch tasks against the registry.
    sync_interval: float = field(default=1.0, metadata={"min": MIN_INTERVAL})


@dataclass(frozen=True, slots=True)
class RetentionSection:
    #: Bus events are the fire: 94% of the file, 7.1 MB/day. Nothing reads a
    #: week-old bus event — the régie's cursor is forward-only.
    bus_days: int = field(default=7, metadata={"min": 1})
    #: Two weeks. Recall over a job older than that is nearly worthless: the
    #: code has moved, branches are merged or deleted, and the harness
    #: transcript is usually gone from disk already.
    jobs_days: int = field(default=15, metadata={"min": 1})
    #: `send.refused` is the only record of a refused send — `_refuse_send`
    #: writes no job row — so it is exempt from the age TTL and capped by row
    #: count instead. Observed ~3/day; this is a century of headroom, existing
    #: to bound growth rather than because it is expected to bind.
    refused_cap: int = field(default=10000, metadata={"min": 1})
    #: Abandoned running jobs (daemon killed mid-turn) have finished_at = NULL
    #: forever and become immortal. 7 days is orders of magnitude longer than
    #: the observer's 60 s rescue timeout, so it can only ever catch jobs from
    #: a previous daemon lifetime. See gc.py MF1.
    stale_running_days: int = field(default=7, metadata={"min": 1})
    #: Rows per DELETE statement so no single sweep blocks the event loop.
    #: Measured: 32,217 rows in 96 ms.
    batch: int = field(default=5000, metadata={"min": 1})
    #: Seconds between sweeps. The whole point: the database is bounded without
    #: the user having to configure anything.
    interval: float = field(default=3600.0, metadata={"min": MIN_INTERVAL})
    #: Default ON. The whole point is that the database is bounded without the
    #: user having to configure anything.
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class HarnessSection:
    #: A denylist, not an allowlist, so an adapter added in a later release
    #: appears without editing this file. A disabled harness is absent, not
    #: refused — hiding a session that exists would be worse than admitting
    #: Theater cannot read it. Matched against the file stem before import.
    disabled: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RegieSection:
    #: Not validated here: importing Textual's legal-name list would pull the
    #: whole TUI stack into the daemon. The régie validates it at start-up.
    theme: str | None = None
    #: How often to refresh the participant tree.
    tree_interval: float = field(default=1.0, metadata={"min": MIN_INTERVAL})
    #: How often to poll the bus for new events.
    bus_interval: float = field(default=0.4, metadata={"min": MIN_INTERVAL})
    #: Events pulled per bus poll.
    bus_batch: int = field(default=50, metadata={"min": 1})
    #: Trailing cwd segments the tree keeps; the rest is elided with ``…/``.
    #: Applied after ``tilde()``, so ``~`` is a preserved prefix. Minimum 1.
    cwd_segments: int = field(default=2, metadata={"min": 1})
    #: Read once, used twice: the ``#sidebar`` style and ``resize_pane``. If
    #: they disagree, Textual and tmux tear at the boundary. Below 40, depth-3
    #: rails plus a two-segment path no longer fit.
    sidebar_width: int = field(default=52, metadata={"min": 40})
    #: Off by default: the tree is what the régie is for. While hidden the bus
    #: is not polled at all — see `RegieApp._refresh_bus`. The palette toggles
    #: it for the current session; this only decides the open state.
    bus_visible: bool = False
    #: Which cost window the price footer shows: "day", "week", "month", or "year".
    cost_window: str = "day"


#: Section name -> dataclass. Drives both parsing and the unknown-section
#: check, so adding a section here is the only edit needed to make it legal.
_SECTIONS: dict[str, type] = {
    "theater": TheaterSection,
    "rails": RailsSection,
    "observer": ObserverSection,
    "retention": RetentionSection,
    "harness": HarnessSection,
    "regie": RegieSection,
}

#: Keys are harness names, whose legal set depends on the registry this module
#: cannot see. Kept out of `_SECTIONS` and parsed by `_build_models` instead.
MODELS_SECTION = "models"

#: Same shape as `[models]`, keyed by harness name. Kept out of `_SECTIONS`
#: and parsed by `_build_reasoning` for the same reason.
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
    #: Harness name -> models `spawn --model` may name. An allowlist: an absent
    #: or empty list permits no model *selection* — children use the CLI's own
    #: config. See `rails.check_model_allowed`.
    models: dict[str, list[str]] = field(default_factory=dict)
    #: Harness name -> reasoning efforts `spawn --reasoning-effort` may name.
    #: An allowlist, same shape and semantics as `models`.
    reasoning: dict[str, list[str]] = field(default_factory=dict)
    #: Dotted key -> "default" | "config.toml". The whole point of
    #: `theater config`: a value alone cannot tell the user whether their edit
    #: took effect.
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
