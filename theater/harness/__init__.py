"""Harness registry.

One module per harness, one instance each. The instances are stateless apart
from the transcript root they read, which is constructor-injected so tests can
point them at a temporary directory instead of the user's real ~/.claude.

Adding a harness is: write the module, add it here. Nothing above this package
needs to change, because nothing above it sees anything but `Event`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from theater.harness.base import (
    APPROVALS,
    MAX_TEXT,
    SERVER_NAME,
    Event,
    EventKind,
    Harness,
    LaunchPlan,
    NativeChild,
    clip,
    clipper,
    last_screen_line,
    status_after,
    theater_binary,
)
from theater import paths
from theater.config import Config, ConfigError
from theater.harness import plugins
from theater.harness.claude_code import ClaudeCodeHarness
from theater.harness.declared import DeclaredHarness
from theater.harness.plugins import PluginError
from theater.harness.vibe import VibeHarness
from theater.models import BadRequest

#: The adapters written in Python. Snapshotted separately from HARNESSES so
#: that installing config-declared harnesses is idempotent and reversible: the
#: registry is rebuilt from this each time rather than accumulated.
_BUILTIN_HARNESSES: dict[str, Harness] = {
    h.name: h for h in (ClaudeCodeHarness(), VibeHarness())
}

#: Aliases that a misreporting agent might send at registration. The canonical
#: name is what the observer needs to match it to a harness adapter; without
#: normalization, `claude_code` or `Claude` registers happily and is then
#: unobservable forever, because the observer looks up `HARNESSES[name]` and
#: misses.
_BUILTIN_ALIASES: dict[str, str] = {
    "claude_code": "claude",
    "claude-code": "claude",
    "Claude": "claude",
    "ClaudeCode": "claude",
    "vibe": "vibe",
    "Vibe": "vibe",
    "mistral-vibe": "vibe",
    "mistral_vibe": "vibe",
}

#: The live registry. Mutated in place by `install`, never rebound: other
#: modules hold a reference to this exact dict (`from theater.harness import
#: HARNESSES`), and rebinding would leave every one of them reading a stale
#: registry with no symptom but a missing harness.
HARNESSES: dict[str, Harness] = dict(_BUILTIN_HARNESSES)
_ALIASES: dict[str, str] = dict(_BUILTIN_ALIASES)


def install(config: Config, *, plugin_dir: Path | None = None) -> list[str]:
    """Rebuild the registry: built-ins, then plugins, then config declarations.

    Called once per process that needs the full set — the daemon at start-up,
    the CLI before dispatch. Rebuilt rather than extended, so calling it twice
    is the same as calling it once and `install(Config())` restores the
    built-ins, which is what test isolation needs.

    The order is the precedence. A plugin may replace a built-in, because a
    plugin is a full adapter and can do everything the built-in did; a
    declaration may replace neither, because it cannot read a transcript and
    the loss would be silent.

    Returns the names that were added or replaced, for logging. Raises
    `ConfigError` for anything that cannot be honoured: a harness the user
    believes they installed but which is silently absent is the failure mode
    this whole release is built to avoid.
    """
    HARNESSES.clear()
    HARNESSES.update(_BUILTIN_HARNESSES)
    _ALIASES.clear()
    _ALIASES.update(_BUILTIN_ALIASES)

    added: list[str] = []
    from_plugin: dict[str, Path] = {}
    directory = plugin_dir if plugin_dir is not None else paths.harnesses_dir()
    for path, harness in plugins.load(directory):
        if harness.name in from_plugin:
            raise ConfigError(
                f"two plugins both define the harness {harness.name!r}: "
                f"{from_plugin[harness.name]} and {path}"
            )
        from_plugin[harness.name] = path
        HARNESSES[harness.name] = harness
        added.append(harness.name)
        for alias in harness.aliases:
            _claim_alias(alias, harness.name, str(path))

    for name, spec in config.harnesses.items():
        if name in _BUILTIN_HARNESSES:
            raise ConfigError(
                f"[harness.{name}] would replace the built-in {name!r} adapter, "
                "which can read its transcript — a declared harness cannot. "
                "Pick another name, or write a plugin to override it."
            )
        if name in from_plugin:
            raise ConfigError(
                f"[harness.{name}] and the plugin {from_plugin[name]} both "
                f"define {name!r}. Two definitions of one harness, and nothing "
                "here can know which you meant — delete one."
            )
        missing = sorted(set(APPROVALS) - set(spec.approvals))
        if missing:
            raise ConfigError(
                f"'harness.{name}.approvals' is missing {', '.join(missing)} — "
                "every mode needs its flags spelled out, including the empty "
                "list, so that no mode launches with flags nobody chose"
            )
        unknown = sorted(set(spec.approvals) - set(APPROVALS))
        if unknown:
            raise ConfigError(
                f"'harness.{name}.approvals' has unknown mode(s) "
                f"{', '.join(unknown)}; known: {', '.join(APPROVALS)}"
            )
        HARNESSES[name] = DeclaredHarness(name, spec)
        added.append(name)

    for name, spec in config.harnesses.items():
        for alias in [name, *spec.aliases]:
            _claim_alias(alias, name, f"'harness.{name}'")
    return added


def _claim_alias(alias: str, owner: str, claimant: str) -> None:
    """Point `alias` at `owner`, unless something else already owns it.

    An alias that shadows another harness makes that harness unreachable at
    registration, and the symptom — an agent observed as the wrong harness, or
    not observed at all — points nowhere near the file that caused it. So a
    collision is refused rather than resolved by load order.
    """
    target = _ALIASES.get(alias)
    if target is not None and target != owner:
        raise ConfigError(
            f"{claimant} claims alias {alias!r}, which already resolves to "
            f"{target!r}"
        )
    if alias in HARNESSES and alias != owner:
        raise ConfigError(
            f"{claimant} claims alias {alias!r}, which is the name of another "
            "harness"
        )
    _ALIASES[alias] = owner


def normalize(name: str) -> str:
    """Map a harness name as an agent might report it to the canonical key.

    Unknown names are returned unchanged so the caller can decide whether to
    reject or accept as-is — `register` accepts and warns, because a genuinely
    unknown harness is not an error at first contact, just an unobservable one.
    """
    return _ALIASES.get(name, name)


def get(name: str) -> Harness:
    harness = HARNESSES.get(name)
    if harness is None:
        known = ", ".join(sorted(HARNESSES))
        raise BadRequest(f"unknown harness {name!r}; known: {known}")
    return harness


def plan_launch(
    harness: str,
    *,
    participant_id: str,
    prompt: str,
    config_path: Path,
    approval: str,
) -> LaunchPlan:
    return get(harness).plan_launch(
        participant_id=participant_id,
        prompt=prompt,
        config_path=config_path,
        approval=approval,
    )


#: Shown for a participant whose harness has no adapter — an unmanaged pane,
#: or an agent that registered under a name we do not recognise.
UNKNOWN_ICON = "?"


def harness_icon(name: str | None) -> str:
    """The one-character mark for a harness name, as reported by a participant.

    Normalizes first, so an agent that registered as `claude-code` still gets
    the Claude glyph. Unknown names are not an error here: an external
    participant may be running something Theater has never heard of, and a
    listing should say so rather than refuse to draw the row.
    """
    harness = HARNESSES.get(normalize(name or ""))
    return harness.icon if harness else UNKNOWN_ICON


def describe() -> list[dict]:
    """Every registered harness as plain data, sorted by name.

    One builder for three consumers — the `harnesses` RPC, `theater harnesses`
    and the régie's palette — because they were drifting: each formatted the
    registry its own way, so a field added for one silently missed the others.
    Plain dicts rather than the `Harness` objects, since two of the three read
    this over a socket.

    `installed` is resolved here, so it describes the PATH of whichever process
    called. That is the honest answer for all three: the daemon spawns the
    binary, and the CLI and régie run on the same machine.
    """
    rows = []
    for name in sorted(HARNESSES):
        harness = HARNESSES[name]
        path = shutil.which(harness.binary)
        rows.append(
            {
                "name": name,
                "icon": harness.icon,
                "binary": harness.binary,
                "installed": path is not None,
                "path": path,
            }
        )
    return rows


def known_binaries() -> set[str]:
    """Every binary name the registered harnesses look for on PATH.

    Used by the unmanaged-pane sweep: a pane whose current command matches one
    of these is a harness the daemon can observe if only it knew the session,
    so it should be surfaced rather than invisible.
    """
    return {h.binary for h in HARNESSES.values()}


__all__ = [
    "APPROVALS",
    "HARNESSES",
    "MAX_TEXT",
    "SERVER_NAME",
    "UNKNOWN_ICON",
    "ClaudeCodeHarness",
    "DeclaredHarness",
    "Event",
    "EventKind",
    "Harness",
    "LaunchPlan",
    "NativeChild",
    "PluginError",
    "VibeHarness",
    "clip",
    "clipper",
    "describe",
    "get",
    "harness_icon",
    "install",
    "known_binaries",
    "last_screen_line",
    "normalize",
    "plan_launch",
    "status_after",
    "theater_binary",
]
