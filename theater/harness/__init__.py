"""Harness registry.

Every adapter is a plugin file. The ones Theater ships live in
`builtin/plugins/`, the ones a user writes live in `$THEATER_HOME/harnesses/`,
and both are read by `plugins.scan` under the same contract. There is no
built-in tier. Each adapter is two objects: a `Harness` that knows how to launch
the CLI, and the `HarnessObserver` it carries, which knows how to watch it. The
only distinction the system still draws between adapters is
`HarnessObserver.has_transcript`.

`install` turns those files into the live registry. Until it runs the registry
is empty, which is deliberate: a shipped plugin that will not import is fatal,
and the only way past it is `[harness] disabled`, which cannot be read at import
time. Every process that touches the registry installs first — the daemon at
start-up, the CLI before dispatch (`config` excepted, since it is the command
for explaining a broken config file).

Nothing above this package needs to change to add a harness, because nothing
above it sees anything but `Event`.
"""

from __future__ import annotations

import inspect
import logging
import shutil
from pathlib import Path

from theater import paths
from theater.config import Config, ConfigError
from theater.harness import builtin, plugins
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
from theater.harness.observation import (
    HarnessObserver,
    ScreenConfidence,
    ScreenKind,
    ScreenReading,
    TranscriptObserver,
)
from theater.harness.plugins import Plugin, PluginError
from theater.harness.source import (
    Attachment,
    Batch,
    History,
    Source,
    TranscriptSource,
)
from theater.models import BadRequest

logger = logging.getLogger("theater.harness")

#: The live registry. Mutated in place by `install`, never rebound: other
#: modules hold a reference to this exact dict (`from theater.harness import
#: HARNESSES`), and rebinding would leave every one of them reading a stale
#: registry with no symptom but a missing harness.
HARNESSES: dict[str, Harness] = {}

#: Aliases a misreporting agent might send at registration, mapped to the
#: canonical name. Without this, an agent that says `claude_code` registers
#: happily and is then unobservable forever, because the observer looks up
#: `HARNESSES[name]` and misses. Every alias is declared by the plugin that
#: owns it — see `Harness.aliases`.
_ALIASES: dict[str, str] = {}

#: name -> the plugin file it came from. Kept for the collision messages and
#: for the SOURCE column of `theater harnesses`: "why is this harness behaving
#: strangely" is usually answered by naming the file it was loaded from.
_PLUGINS: dict[str, Plugin] = {}

#: Local plugins that would not load. Not an exception — see `install` — but
#: not silent either: they are listed by `theater harnesses` as broken, since a
#: plugin the user believes they installed and cannot find is the failure this
#: release exists to remove.
_BROKEN: list[Plugin] = []


def install(
    config: Config,
    *,
    local_dir: Path | None = None,
    shipped_dir: Path | None = None,
) -> list[str]:
    """Rebuild the registry from the shipped and local plugin directories.

    Called once per process that needs it. Rebuilt rather than extended, so
    calling it twice is the same as calling it once — which is what test
    isolation needs, and what makes `theater restart` a complete answer to a
    config change.

    Local beats shipped. Someone who has written their own `vibe.py` has said
    which one they want, and refusing the collision would leave them no way to
    repair a shipped adapter without waiting for a release. Both paths are
    logged, because a silently shadowed adapter is a long afternoon.

    The two sources fail differently. A broken *shipped* plugin raises: it is a
    bug in Theater, and coming up without an adapter the user has every reason
    to expect would hide it. A broken *local* plugin is skipped with a warning
    and listed as broken by `theater harnesses`: the user wrote it, they can see
    it, and one bad file of their own should not stop the daemon from starting.

    Returns the registered names, sorted.
    """
    # Matched against the file stem before the import and the harness name
    # after it: a plugin too broken to say what it is called still has to be
    # switchable off, and that is exactly when the user needs it to be.
    disabled = set(config.harness.disabled)

    HARNESSES.clear()
    _ALIASES.clear()
    _PLUGINS.clear()
    _BROKEN.clear()

    shipped = plugins.scan(
        shipped_dir if shipped_dir is not None else builtin.plugin_dir(),
        source=plugins.SHIPPED,
        skip=disabled,
    )
    local = plugins.scan(
        local_dir if local_dir is not None else paths.harnesses_dir(),
        source=plugins.LOCAL,
        skip=disabled,
    )

    for found in [*shipped, *local]:
        if found.name in disabled:
            continue
        if found.harness is None:
            _reject(found, config)
            continue
        previous = _PLUGINS.get(found.name)
        if previous is not None and previous.source == found.source:
            raise ConfigError(
                f"two {found.source} plugins both define the harness "
                f"{found.name!r}: {previous.path} and {found.path}"
            )
        if previous is not None:
            logger.info(
                "%s plugin %s overrides the %s %s",
                found.source,
                found.path,
                previous.source,
                previous.path,
            )
        HARNESSES[found.name] = found.harness
        _PLUGINS[found.name] = found
        for alias in found.harness.aliases:
            _claim_alias(alias, found.name, str(found.path))

    return sorted(HARNESSES)


def _reject(found: Plugin, config: Config) -> None:
    """A plugin that would not load: fatal if we shipped it, logged if not."""
    if found.source == plugins.SHIPPED:
        where = config.path or paths.config_path()
        raise PluginError(
            f"{found.error}\n  This is an adapter Theater ships, so this is a "
            f"bug. To start without it, put\n    [harness]\n    disabled = "
            f'["{found.name}"]\n  in {where}'
        )
    logger.warning("skipping harness plugin %s: %s", found.path, found.error)
    _BROKEN.append(found)


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


def supports_model(harness: Harness) -> bool:
    """Whether this adapter accepts a `model` in `plan_launch`.

    Read off the signature rather than declared as a class attribute, because
    the signature is the thing that is actually true: an adapter that says it
    supports models and does not take the parameter would fail at the call,
    which is the failure this check exists to prevent.
    """
    return "model" in inspect.signature(harness.plan_launch).parameters


def check_model(harness: str, model: str | None) -> None:
    """Raise if this harness cannot honour a model request.

    Pure and cheap, so a caller can gate on it *before* creating anything. The
    spawner does exactly that: `plan_launch` runs after the participant and its
    worktree exist, and a refusal there would leave both behind.
    """
    if model is not None and not supports_model(get(harness)):
        raise BadRequest(f"harness {harness!r} does not support model selection")


def plan_launch(
    harness: str,
    *,
    participant_id: str,
    prompt: str,
    config_path: Path,
    approval: str,
    model: str | None = None,
) -> LaunchPlan:
    """The one funnel every spawn goes through, and so the one compat seam.

    `model` is only forwarded when the caller named one. That is what keeps a
    third-party adapter written against the older signature working: it is
    never called with a keyword it does not accept, and the only launches it
    cannot serve are the ones that ask for something it has no way to deliver.
    Those are refused here, with a message that names the harness, rather than
    surfacing as a `TypeError` from inside the plugin or — far worse — as a
    child that silently came up on the wrong model.
    """
    found = get(harness)
    check_model(harness, model)
    extra = {"model": model} if model is not None else {}
    return found.plan_launch(
        participant_id=participant_id,
        prompt=prompt,
        config_path=config_path,
        approval=approval,
        **extra,
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

    Broken local plugins come last, with `error` set and no usable binary.
    Listing them is the point — the user's question is "where did my harness
    go", and the answer is a parse error in a file they wrote. Every consumer
    that spawns must therefore skip rows carrying an `error`.
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
                "source": _PLUGINS[name].source,
                "error": None,
            }
        )
    for found in sorted(_BROKEN, key=lambda p: p.name):
        rows.append(
            {
                "name": found.name,
                "icon": UNKNOWN_ICON,
                "binary": "",
                "installed": False,
                "path": str(found.path),
                "source": found.source,
                "error": found.error,
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
    "Attachment",
    "Batch",
    "Event",
    "EventKind",
    "Harness",
    "HarnessObserver",
    "History",
    "LaunchPlan",
    "NativeChild",
    "Plugin",
    "PluginError",
    "ScreenConfidence",
    "ScreenKind",
    "ScreenReading",
    "Source",
    "TranscriptObserver",
    "TranscriptSource",
    "check_model",
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
    "supports_model",
    "theater_binary",
]
