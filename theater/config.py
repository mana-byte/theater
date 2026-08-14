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

#: Smallest interval any poll loop may be configured to. Below this the daemon
#: spends more time waking up than working, and a typo'd `0.0001` would spin a
#: core rather than fail. Not a preference — a floor.
MIN_INTERVAL = 0.01


class ConfigError(Exception):
    """The config file exists but cannot be honoured.

    Always fatal at start-up. Carries the file path because the daemon that
    raises it is frequently not the process the user is looking at.
    """


@dataclass(frozen=True, slots=True)
class TheaterSection:
    #: Harness used when `theater spawn` is given no harness, and sorted first
    #: in the régie palette. Validated for *type* here; whether the name is a
    #: real harness can only be checked against the registry, which the daemon
    #: does at start-up once the registry exists.
    favourite: str | None = None


@dataclass(frozen=True, slots=True)
class RailsSection:
    #: Maximum depth of a spawn tree. Roots are depth 0. See daemon/rails.py.
    depth_cap: int = field(default=3, metadata={"min": 0})
    #: Participants a single tree may hold before further spawns are refused.
    budget: int = field(default=20, metadata={"min": 1})


@dataclass(frozen=True, slots=True)
class ObserverSection:
    #: How often to check a known transcript for new bytes. Faster than the
    #: reaper because this drives what the régie renders, and a second of lag
    #: on "what is it doing right now" is visible to a human watching.
    poll_interval: float = field(default=0.25, metadata={"min": MIN_INTERVAL})
    #: How long to wait with no new bytes before re-locating the transcript.
    #: Vibe starts a new session directory on each turn; if the observer is
    #: locked onto the old file, it needs to re-scan to find the new one.
    relocate_timeout: float = field(default=5.0, metadata={"min": MIN_INTERVAL})
    #: How long to wait with no transcript growth before checking the screen
    #: for a bare prompt. If the transcript says WORKING but the screen shows a
    #: prompt, the agent is AWAITING_INPUT. Tuned to avoid false positives:
    #: long enough that a slow tool call will not trigger it, short enough that
    #: a human watching the régie sees the change before getting bored.
    awaiting_input_timeout: float = field(default=1.5, metadata={"min": MIN_INTERVAL})
    #: How long a job may stay running after its target has gone quiet *and* is
    #: showing a prompt, before the observer gives up waiting for a turn-end it
    #: is never going to read and finishes the job anyway.
    #:
    #: This is the backstop for a turn boundary the parser missed — a harness
    #: release changing a discriminator, a transcript rotating at exactly the
    #: wrong moment. Without it the caller's `await_sessions` blocks until its
    #: own deadline with no explanation, which is the failure mode that is
    #: hardest to diagnose from the outside.
    #:
    #: Much longer than awaiting_input_timeout on purpose. That one only paints
    #: a status and can afford to be wrong for a second; this one resolves a
    #: promise made to another agent, and firing early would hand back a
    #: half-written answer. A rescued job is marked `turn_end_unseen` so the
    #: caller can tell a real reply from a salvaged one.
    rescue_timeout: float = field(default=60.0, metadata={"min": MIN_INTERVAL})
    #: How often to look for a transcript not found yet. Slower, because it is
    #: a directory scan rather than a stat.
    search_interval: float = field(default=2.0, metadata={"min": MIN_INTERVAL})
    #: How often to read the screen of a harness with no transcript at all —
    #: one whose adapter reports `has_transcript = False`. Separate from
    #: awaiting_input_timeout and much faster, because for those harnesses the
    #: screen is not a hint about a stuck agent — it is the only evidence that a
    #: turn ended, and a caller is blocked on it. A turn is only called finished
    #: when two consecutive polls agree, so this is also half the latency.
    screen_interval: float = field(default=1.0, metadata={"min": MIN_INTERVAL})
    #: How often to reconcile the watch tasks against the registry.
    sync_interval: float = field(default=1.0, metadata={"min": MIN_INTERVAL})


@dataclass(frozen=True, slots=True)
class HarnessSection:
    #: Harness plugins to leave out of the registry, by name. A denylist rather
    #: than an allowlist so that an adapter added in a later release appears
    #: without anyone editing this file — the opposite choice would make every
    #: new harness invisible to every existing install.
    #:
    #: A disabled harness is absent, not refused: it cannot be spawned, is not
    #: offered by the palette, and is not looked for in unmanaged panes. An
    #: agent that registers under the name still appears in the tree, drawn with
    #: the unknown icon. Hiding a session that exists would be worse than
    #: admitting Theater cannot read it.
    #:
    #: Matched against the plugin's file stem before it is imported, so a plugin
    #: that fails on import — the case where this is most needed — can still be
    #: switched off.
    disabled: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RegieSection:
    #: Textual theme name. Not validated here: the list of legal names lives in
    #: Textual, and importing it to check a string would pull the whole TUI
    #: stack into the daemon. The régie validates it at start-up, where it can
    #: list the real alternatives in the error.
    theme: str | None = None
    #: How often to refresh the participant tree.
    tree_interval: float = field(default=1.0, metadata={"min": MIN_INTERVAL})
    #: How often to poll the bus for new events.
    bus_interval: float = field(default=0.4, metadata={"min": MIN_INTERVAL})
    #: How many bus events to pull per poll.
    bus_batch: int = field(default=50, metadata={"min": 1})
    #: How many trailing segments of a participant's cwd the tree keeps;
    #: the rest is elided with ``…/``. Applied after ``tilde()``, so ``~`` is
    #: a preserved prefix rather than a counted segment. A leaf with no
    #: directory at all is a different feature, so the minimum is 1.
    cwd_segments: int = field(default=2, metadata={"min": 1})
    #: Column width of the sidebar in the régie. Read once, used twice: the
    #: ``#sidebar`` Textual style and the ``resize_pane`` call. If the two
    #: disagree, Textual and tmux tear at the boundary. Below 40, depth-3
    #: rails plus a two-segment path no longer fit — so the feature the
    #: width exists for is already broken. No ceiling: tmux refuses anything
    #: the window cannot honour.
    sidebar_width: int = field(default=60, metadata={"min": 40})


#: Section name in the file -> the dataclass holding it. Drives both parsing
#: and the unknown-section check, so adding a section here is the only edit
#: needed to make it legal.
_SECTIONS: dict[str, type] = {
    "theater": TheaterSection,
    "rails": RailsSection,
    "observer": ObserverSection,
    "harness": HarnessSection,
    "regie": RegieSection,
}

#: Harness names are used as a spawn argument, a wire value and part of a tmux
#: window name. Restricting them here means none of those three has to quote.
#: Public because a plugin harness names itself in Python and has to meet the
#: same rule — one definition, or the two ways of adding a harness disagree
#: about what a harness may be called.
HARNESS_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

#: The one section whose keys are not a fixed field set: `[models]` is keyed by
#: harness name, and which names are legal depends on the registry, which this
#: module cannot see. Kept out of `_SECTIONS` for that reason and parsed by
#: `_build_models` instead.
MODELS_SECTION = "models"


@dataclass(frozen=True, slots=True)
class Config:
    """Resolved settings: file values over defaults, nothing else."""

    theater: TheaterSection = field(default_factory=TheaterSection)
    rails: RailsSection = field(default_factory=RailsSection)
    observer: ObserverSection = field(default_factory=ObserverSection)
    harness: HarnessSection = field(default_factory=HarnessSection)
    regie: RegieSection = field(default_factory=RegieSection)
    #: Harness name -> the models `spawn --model` may name for it. An allowlist,
    #: and the absent case is the common one: a harness with no entry (or an
    #: empty list) permits no model *selection* at all, so its children come up
    #: on whatever that CLI's own config says. See `rails.check_model_allowed`.
    models: dict[str, list[str]] = field(default_factory=dict)
    #: Dotted key -> "default" | "config.toml". The whole point of
    #: `theater config`: a value alone cannot tell the user whether their edit
    #: took effect, and "it took effect" is the question they are asking.
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


def _check_int(value: Any) -> int | None:
    # bool is a subclass of int, so `depth_cap = true` would otherwise parse as
    # 1 and configure a working cap out of a nonsense value.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _check_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    # TOML distinguishes 1 from 1.0; a user writing an interval as a whole
    # number means the number, not a type error.
    if isinstance(value, int | float):
        return float(value)
    return None


def _check_str(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _check_str_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None
    return list(value)


#: Annotations are strings under `from __future__ import annotations`, so
#: dispatch on the written form. Explicit beats get_type_hints() here: the set
#: of legal field types is small and closed on purpose.
_CHECKERS = {
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
        # `f.type` is the written annotation, always a string here: see
        # _CHECKERS. Typeshed allows a type object too, which we never see.
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
        # A model named twice is a copy-paste artefact, not a second model, and
        # it would show up twice in every list Theater prints.
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
    # `[models]` has no fields to enumerate, so only what the user wrote can be
    # listed. A harness absent from the file has no row rather than an empty
    # one: there is no default to show, and inventing a row per registered
    # harness would make this depend on the registry.
    for harness in sorted(config.models):
        dotted = f"{MODELS_SECTION}.{harness}"
        rows.append((dotted, str(config.models[harness]), config.source(dotted)))
    return rows
