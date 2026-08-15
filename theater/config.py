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

The known-key set is derived from the dataclasses below rather than written out
a second time, so a new setting cannot be added without its validation.

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

import difflib
import math
import re
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, NoReturn, cast

from theater import paths

#: Below this the daemon spends more time waking up than working, and a
#: typo'd 0.0001 would spin a core rather than fail.
MIN_INTERVAL = 0.01


class ConfigError(Exception):
    """The config file exists but cannot be honoured.

    Always fatal at start-up. Carries the file path because the daemon that
    raises it is frequently not the process the user is looking at.
    """


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
    #: Two months. Recall over a job older than that is nearly worthless: the
    #: code has moved, branches are merged or deleted, and the harness
    #: transcript is usually gone from disk already. Jobs are 3.4% of the
    #: file, so this is generous on purpose.
    jobs_days: int = field(default=60, metadata={"min": 1})
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

#: Harness names are a spawn argument, a wire value and part of a tmux window
#: name. Public because a plugin harness names itself in Python and must meet
#: the same rule — one definition, or the two ways of adding a harness disagree.
HARNESS_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

#: Keys are harness names, whose legal set depends on the registry this module
#: cannot see. Kept out of `_SECTIONS` and parsed by `_build_models` instead.
MODELS_SECTION = "models"


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


def _fail(path: Path, message: str) -> NoReturn:
    raise ConfigError(f"{path}: {message}")


def _suggest(name: str, known: list[str]) -> str:
    close = difflib.get_close_matches(name, known, n=1, cutoff=0.6)
    if close:
        return f"did you mean {close[0]!r}?"
    return "known keys: " + ", ".join(sorted(known))


def _check_bool(value: Any) -> bool | None:
    # An integer is not a truth value here, so `bus_visible = 1` is a type
    # error rather than a quiet yes.
    return value if isinstance(value, bool) else None


def _check_int(value: Any) -> int | None:
    # bool is a subclass of int, so `depth_cap = true` would otherwise parse
    # as 1.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _check_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    # TOML distinguishes 1 from 1.0; a whole number means the number.
    if isinstance(value, int | float):
        return float(value)
    return None


def _check_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _check_str_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    return list(value)


#: Dispatch on the written form (annotations are strings under
#: `from __future__ import annotations`). The set of legal field types is
#: small and closed on purpose.
_CHECKERS = {
    "bool": (_check_bool, "true or false"),
    "int": (_check_int, "an integer"),
    "float": (_check_float, "a number"),
    "str": (_check_str, "a string"),
    "str | None": (_check_str, "a string"),
    "list[str]": (_check_str_list, "a list of strings"),
}


def _build_section(path: Path, name: str, cls: type, raw: Any) -> Any:
    if not isinstance(raw, dict):
        _fail(path, f"[{name}] must be a table, got {type(raw).__name__}")

    known = [f.name for f in fields(cls)]
    for key in raw:
        if key not in known:
            _fail(path, f"unknown key '{name}.{key}' ({_suggest(key, known)})")

    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for f in fields(cls):
        if f.name not in raw:
            continue
        dotted = f"{name}.{f.name}"
        # `f.type` is the written annotation, always a string here — see
        # _CHECKERS.
        checker, expected = _CHECKERS[cast(str, f.type)]
        parsed = checker(raw[f.name])
        if parsed is None:
            got = type(raw[f.name]).__name__
            _fail(path, f"'{dotted}' must be {expected}, got {got}")
        minimum = f.metadata.get("min")
        if minimum is not None and parsed < minimum:
            _fail(path, f"'{dotted}' must be >= {minimum}, got {parsed}")
        if isinstance(parsed, float) and not math.isfinite(parsed):
            _fail(path, f"'{dotted}' must be finite, got {parsed}")
        values[f.name] = parsed
        sources[dotted] = "config.toml"

    return cls(**values), sources


def _defaults_for(name: str, cls: type) -> dict[str, str]:
    return {f"{name}.{f.name}": "default" for f in fields(cls)}


def _build_models(path: Path, raw: Any) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Parse `[models]`, which is keyed by harness name rather than by field.

    It cannot go through `_build_section`: the legal keys are whatever
    harnesses are registered, and this module deliberately knows nothing about
    the registry. So the shape is checked here — name spelling, list of
    strings — and whether the harness exists is left to the daemon, which
    checks it at start-up once the registry is built. That is the same split
    `theater.favourite` already uses, and the reason a `[models]` entry for a
    harness you have not installed yet is not an error at parse time.
    """
    if not isinstance(raw, dict):
        _fail(path, f"[{MODELS_SECTION}] must be a table, got {type(raw).__name__}")

    out: dict[str, list[str]] = {}
    sources: dict[str, str] = {}
    for name, value in raw.items():
        dotted = f"{MODELS_SECTION}.{name}"
        if not HARNESS_NAME.match(name):
            _fail(
                path,
                f"'{dotted}' is not a legal harness name: expected lowercase "
                "letters, digits, '-' or '_', starting with a letter or digit",
            )
        names = _check_str_list(value)
        if names is None:
            got = type(value).__name__
            _fail(path, f"'{dotted}' must be a list of strings, got {got}")
        # A duplicate is a copy-paste artefact, and would show up twice in
        # every list Theater prints.
        if len(set(names)) != len(names):
            dupe = next(n for n in names if names.count(n) > 1)
            _fail(path, f"'{dotted}' lists {dupe!r} more than once")
        out[name] = names
        sources[dotted] = "config.toml"
    return out, sources


def _check_no_declarations(path: Path, raw: Any) -> None:
    """Refuse a `[harness.<name>]` table left over from before v1.4.

    Without this the generic unknown-key check fires and says
    `unknown key 'harness.codex'`, which reads as a typo and sends the user
    looking for the right spelling of a key that no longer exists. The whole
    mechanism was replaced by plugins, and that is what the message has to say.
    """
    if not isinstance(raw, dict):
        return
    declared = [name for name, body in raw.items() if isinstance(body, dict)]
    if declared:
        _fail(
            path,
            f"[harness.{declared[0]}] declares a harness in config, which v1.4 "
            "removed: a declaration could launch a harness but never read its "
            "transcript, so turns ended on a guess. Write a plugin instead — "
            "see docs/harness-plugins.md",
        )


def load(path: Path | None = None) -> Config:
    """Read the config file, or return defaults if it is not there.

    A missing file is the normal case and not an error. A file that exists but
    is malformed, or names a key Theater does not have, raises `ConfigError` —
    see the module docstring for why that is not a warning.
    """
    target = path or paths.config_path()

    sources: dict[str, str] = {}
    for name, cls in _SECTIONS.items():
        sources.update(_defaults_for(name, cls))

    if not target.exists():
        return Config(sources=sources, path=target, exists=False)

    try:
        raw = tomllib.loads(target.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{target}: not valid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{target}: cannot read: {exc}") from exc

    legal = [*_SECTIONS, MODELS_SECTION]
    for key in raw:
        if key not in legal:
            _fail(target, f"unknown section [{key}] ({_suggest(key, legal)})")
    _check_no_declarations(target, raw.get("harness"))

    built: dict[str, Any] = {}
    for name, cls in _SECTIONS.items():
        if name not in raw:
            built[name] = cls()
            continue
        section, section_sources = _build_section(target, name, cls, raw[name])
        built[name] = section
        sources.update(section_sources)

    models: dict[str, list[str]] = {}
    if MODELS_SECTION in raw:
        models, model_sources = _build_models(target, raw[MODELS_SECTION])
        sources.update(model_sources)

    return Config(**built, models=models, sources=sources, path=target, exists=True)


def describe(config: Config) -> list[tuple[str, str, str]]:
    """Every setting as (dotted key, value, source), in file order.

    Rendering lives in the CLI; the ordering lives here because it should match
    the order a user would write the file in.
    """
    rows: list[tuple[str, str, str]] = []
    for name, cls in _SECTIONS.items():
        section = getattr(config, name)
        for f in fields(cls):
            dotted = f"{name}.{f.name}"
            value = getattr(section, f.name)
            shown = "(unset)" if value is None else str(value)
            rows.append((dotted, shown, config.source(dotted)))
    # `[models]` has no fields to enumerate. A harness absent from the file has
    # no row: there is no default, and inventing one per registered harness would
    # depend on the registry.
    for harness in sorted(config.models):
        dotted = f"{MODELS_SECTION}.{harness}"
        rows.append((dotted, str(config.models[harness]), config.source(dotted)))
    return rows
