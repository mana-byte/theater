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
from typing import Any

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
    awaiting_input_timeout: float = field(default=10.0, metadata={"min": MIN_INTERVAL})
    #: How often to look for a transcript not found yet. Slower, because it is
    #: a directory scan rather than a stat.
    search_interval: float = field(default=2.0, metadata={"min": MIN_INTERVAL})
    #: How often to read the screen of a harness that has no transcript at all
    #: (one declared in `[harness.*]`). Separate from awaiting_input_timeout
    #: and much faster, because for those harnesses the screen is not a hint
    #: about a stuck agent — it is the only evidence that a turn ended, and a
    #: caller is blocked on it. A turn is only called finished when two
    #: consecutive polls agree, so this is also half the detection latency.
    screen_interval: float = field(default=1.0, metadata={"min": MIN_INTERVAL})
    #: How often to reconcile the watch tasks against the registry.
    sync_interval: float = field(default=1.0, metadata={"min": MIN_INTERVAL})


@dataclass(frozen=True, slots=True)
class HarnessSpec:
    """A harness declared in config rather than written in Python.

    Covers launching, presence and listing — everything except reading a
    transcript, which is not data and needs a plugin (`docs/harness-plugins.md`).
    A harness declared here is observed from its rendered screen instead; see
    `harness/declared.py` for what that costs.

    Three fields are required with no default, and each for the same reason: a
    missing value would not degrade, it would mislead. Without `binary` there is
    nothing to run; without `approvals` a `yolo` spawn would launch with no
    flags and look safe; without `idle_prompts` no turn ever ends, so every
    caller that sends to this harness waits forever.
    """

    #: Executable to look for on PATH and to run as argv[0].
    binary: str = field(default="", metadata={"required": True})
    #: One character shown before the name in listings. See `Harness.icon` for
    #: why this cannot be an image.
    icon: str = "·"
    #: Other names that should resolve to this one at registration time.
    aliases: list[str] = field(default_factory=list)
    #: Argument template after the binary and the injected flags. `{prompt}`
    #: is the initial prompt; an element that renders empty is dropped.
    argv: list[str] = field(default_factory=lambda: ["{prompt}"])
    #: Extra environment for the pane, templated.
    env: dict[str, str] = field(default_factory=dict)
    #: Per-approval-mode flags, one list per mode in APPROVALS. All three keys
    #: are required — see the class docstring.
    approvals: dict[str, list[str]] = field(
        default_factory=dict, metadata={"required": True}
    )
    #: Screen lines that mean "waiting for you", matched exactly against the
    #: last non-empty line of `capture-pane`.
    idle_prompts: list[str] = field(default_factory=list, metadata={"required": True})
    #: MCP registration, three independent levers because the harnesses that
    #: exist use three different ones: an env var (vibe), a config file passed
    #: by flag (claude), or dotted overrides on the command line (codex).
    #: Whichever are set are all applied.
    mcp_env: dict[str, str] = field(default_factory=dict)
    mcp_argv: list[str] = field(default_factory=list)
    #: Contents to write at `{config_path}` before the window is created.
    mcp_file: str | None = None
    #: Flags pointing the harness at that file. Required with `mcp_file`, and
    #: meaningless without it.
    mcp_file_argv: list[str] = field(default_factory=list)


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


#: Section name in the file -> the dataclass holding it. Drives both parsing
#: and the unknown-section check, so adding a section here is the only edit
#: needed to make it legal.
_SECTIONS: dict[str, type] = {
    "theater": TheaterSection,
    "rails": RailsSection,
    "observer": ObserverSection,
    "regie": RegieSection,
}


#: Table of tables rather than a fixed section: the keys are harness names the
#: user invents, so it cannot live in `_SECTIONS` with the others.
_HARNESS_SECTION = "harness"

#: Harness names are used as a spawn argument, a wire value and part of a tmux
#: window name. Restricting them here means none of those three has to quote.
#: Public because a plugin harness names itself in Python and has to meet the
#: same rule — one definition, or the two ways of adding a harness disagree
#: about what a harness may be called.
HARNESS_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class Config:
    """Resolved settings: file values over defaults, nothing else."""

    theater: TheaterSection = field(default_factory=TheaterSection)
    rails: RailsSection = field(default_factory=RailsSection)
    observer: ObserverSection = field(default_factory=ObserverSection)
    regie: RegieSection = field(default_factory=RegieSection)
    #: Harnesses declared in `[harness.<name>]`, by name. Empty is the normal
    #: case: the built-in adapters are not represented here.
    harnesses: dict[str, HarnessSpec] = field(default_factory=dict)
    #: Dotted key -> "default" | "config.toml". The whole point of
    #: `theater config`: a value alone cannot tell the user whether their edit
    #: took effect, and "it took effect" is the question they are asking.
    sources: dict[str, str] = field(default_factory=dict)
    #: Where the file would be, whether or not it is there.
    path: Path | None = None
    exists: bool = False

    def source(self, dotted: str) -> str:
        return self.sources.get(dotted, "default")


def _fail(path: Path, message: str) -> None:
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


def _check_str_map(value: Any) -> dict[str, str] | None:
    if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
        return None
    return dict(value)


def _check_str_list_map(value: Any) -> dict[str, list[str]] | None:
    if not isinstance(value, dict):
        return None
    out: dict[str, list[str]] = {}
    for key, raw in value.items():
        parsed = _check_str_list(raw)
        if parsed is None:
            return None
        out[key] = parsed
    return out


#: Annotations are strings under `from __future__ import annotations`, so
#: dispatch on the written form. Explicit beats get_type_hints() here: the set
#: of legal field types is small and closed on purpose.
_CHECKERS = {
    "int": (_check_int, "an integer"),
    "float": (_check_float, "a number"),
    "str": (_check_str, "a string"),
    "str | None": (_check_str, "a string"),
    "list[str]": (_check_str_list, "a list of strings"),
    "dict[str, str]": (_check_str_map, "a table of strings"),
    "dict[str, list[str]]": (_check_str_list_map, "a table of string lists"),
}


def _build_section(path: Path, name: str, cls: type, raw: Any) -> Any:
    if not isinstance(raw, dict):
        _fail(path, f"[{name}] must be a table, got {type(raw).__name__}")

    known = [f.name for f in fields(cls)]
    for key in raw:
        if key not in known:
            _fail(path, f"unknown key '{name}.{key}' ({_suggest(key, known)})")
    for f in fields(cls):
        # A dataclass default exists for every field so the type stays simple,
        # but some of those defaults are not usable values — see HarnessSpec.
        if f.metadata.get("required") and f.name not in raw:
            _fail(path, f"[{name}] is missing required key '{f.name}'")

    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for f in fields(cls):
        if f.name not in raw:
            continue
        dotted = f"{name}.{f.name}"
        checker, expected = _CHECKERS[f.type]
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


def _build_harnesses(
    path: Path, raw: Any
) -> tuple[dict[str, HarnessSpec], dict[str, str]]:
    """Parse `[harness.<name>]` tables into specs.

    Only syntax is checked here. Whether the approval keys are the modes
    Theater actually has is a harness question, answered where the registry is
    built — config has no business importing the harness package, and doing so
    would close an import cycle.
    """
    if not isinstance(raw, dict):
        _fail(path, f"[{_HARNESS_SECTION}] must be a table of tables")

    specs: dict[str, HarnessSpec] = {}
    sources: dict[str, str] = {}
    for name, body in raw.items():
        if not HARNESS_NAME.match(name):
            _fail(
                path,
                f"harness name {name!r} must be lowercase letters, digits, "
                "'-' or '_', starting with a letter or digit",
            )
        dotted = f"{_HARNESS_SECTION}.{name}"
        spec, spec_sources = _build_section(path, dotted, HarnessSpec, body)
        if spec.mcp_file is not None and not spec.mcp_file_argv:
            _fail(
                path,
                f"'{dotted}.mcp_file' is set but 'mcp_file_argv' is not — the "
                "file would be written and never passed to the harness",
            )
        if spec.mcp_file_argv and spec.mcp_file is None:
            _fail(
                path,
                f"'{dotted}.mcp_file_argv' is set but 'mcp_file' is not — "
                "there would be no file at that path",
            )
        if not spec.idle_prompts:
            _fail(
                path,
                f"'{dotted}.idle_prompts' must list at least one prompt: with "
                "no transcript to read, it is the only way a turn can end",
            )
        specs[name] = spec
        sources.update(spec_sources)
        # Every field of a declared harness is reportable, so the ones left at
        # their default are still listed rather than silently absent.
        for f in fields(HarnessSpec):
            sources.setdefault(f"{dotted}.{f.name}", "default")
    return specs, sources


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

    legal = [*_SECTIONS, _HARNESS_SECTION]
    for key in raw:
        if key not in legal:
            _fail(target, f"unknown section [{key}] ({_suggest(key, legal)})")

    built: dict[str, Any] = {}
    for name, cls in _SECTIONS.items():
        if name not in raw:
            built[name] = cls()
            continue
        section, section_sources = _build_section(target, name, cls, raw[name])
        built[name] = section
        sources.update(section_sources)

    harnesses: dict[str, HarnessSpec] = {}
    if _HARNESS_SECTION in raw:
        harnesses, harness_sources = _build_harnesses(target, raw[_HARNESS_SECTION])
        sources.update(harness_sources)

    return Config(
        **built, harnesses=harnesses, sources=sources, path=target, exists=True
    )


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
    # Declared harnesses last, and only the ones that exist: unlike the fixed
    # sections there is no default set to show when nobody declared any.
    for name in sorted(config.harnesses):
        spec = config.harnesses[name]
        for f in fields(HarnessSpec):
            dotted = f"{_HARNESS_SECTION}.{name}.{f.name}"
            value = getattr(spec, f.name)
            shown = "(unset)" if value is None else str(value)
            rows.append((dotted, shown, config.source(dotted)))
    return rows
