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
from typing import Any

from theater import paths
from theater.config import Config, ConfigError
from theater.harness import builtin, plugins
from theater.harness.base import (
    APPROVALS,
    MAX_TEXT,
    SERVER_NAME,
    Event,
    EventKind,
    EventPath,
    Harness,
    LaunchPlan,
    NativeChild,
    ResumeLaunchOverlay,
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
    StreamPoint,
    TranscriptSource,
)
from theater.models import BadRequest

logger = logging.getLogger("theater.harness")

#: The live registry. Mutated in place by `install`, never rebound: other
#: modules hold a reference to this exact dict, and rebinding would leave them
#: reading a stale registry with no symptom but a missing harness.
HARNESSES: dict[str, Harness] = {}

#: Aliases a misreporting agent might send at registration, mapped to the
#: canonical name. Without this, an agent that says `claude_code` registers and
#: is then unobservable forever — the observer looks up `HARNESSES[name]` and
#: misses.
_ALIASES: dict[str, str] = {}

#: name -> the plugin file it came from. Kept for collision messages and the
#: SOURCE column of `theater harnesses`.
_PLUGINS: dict[str, Plugin] = {}

#: Binary names claimed by registered harnesses, mapped to a (harness name,
#: claimant path) pair. Two adapters claiming the same binary is silently
#: resolved by iteration order in ``match_binary`` — refusing at load names
#: both files, the same shape as the alias collision guard.
_BINARIES: dict[str, tuple[str, str]] = {}

#: Tmux observation keys — 15-character truncated forms of binary names that
#: tmux's ``pane_current_command`` would report when the kernel truncates a
#: longer process name.  Mapped to a (harness name, claimant path) pair.
#: Two different harnesses claiming the same observation key would be
#: silently resolved by iteration order in the pane matcher — refused at
#: load time with both files named, the same shape as ``_BINARIES``.
_OBSERVATION_KEYS: dict[str, tuple[str, str]] = {}

#: The kernel truncates ``pane_current_command`` at 15 characters.  A binary
#: name longer than that arrives truncated and cannot be matched by exact
#: comparison.  Observation keys are the first 15 characters of every spelling
#: the matcher would accept, so the pane matcher can resolve a truncated pane
#: command to the right harness.
_TMUX_TRUNCATION = 15

#: Local plugins that would not load. Listed by `theater harnesses` as broken —
#: a plugin the user believes they installed and cannot find is the failure this
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
    config change. Plugin modules are re-executed on every call; ``_``-prefixed
    helper modules are cached in ``sys.modules`` under mangled names for the
    life of the process, so a rescan reuses the first call's helpers rather
    than re-executing them. Each call site runs once per process, so this is a
    documentation-honesty note rather than an observable difference.

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
    # Matched against the file stem before import and the harness name after: a
    # plugin too broken to say what it is called still has to be switchable off.
    disabled = set(config.harness.disabled)

    HARNESSES.clear()
    _ALIASES.clear()
    _PLUGINS.clear()
    _BINARIES.clear()
    _OBSERVATION_KEYS.clear()
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
            if previous.harness is not None:
                _release_claims(previous.harness, previous.name)
        _claim_name(found.name, str(found.path))
        HARNESSES[found.name] = found.harness
        _PLUGINS[found.name] = found
        for alias in found.harness.aliases:
            _claim_alias(alias, found.name, str(found.path))
        _claim_binary(found.harness.binary, found.name, str(found.path))
        for extra in found.harness.binaries:
            _claim_binary(extra, found.name, str(found.path))
        _claim_observation_keys(found.harness.binary, found.name, str(found.path))
        for extra in found.harness.binaries:
            _claim_observation_keys(extra, found.name, str(found.path))

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
    collision is refused rather than resolved by load order. The error names
    both files so the user can find the conflict.
    """
    target = _ALIASES.get(alias)
    if target is not None and target != owner:
        prev_plugin = _PLUGINS.get(target)
        prev_path = str(prev_plugin.path) if prev_plugin is not None else "(unknown)"
        raise ConfigError(
            f"{claimant} claims alias {alias!r}, which already resolves to {target!r} ({prev_path})"
        )
    if alias in HARNESSES and alias != owner:
        prev_plugin = _PLUGINS.get(alias)
        prev_path = str(prev_plugin.path) if prev_plugin is not None else "(unknown)"
        raise ConfigError(
            f"{claimant} claims alias {alias!r}, which is the name of another harness ({prev_path})"
        )
    _ALIASES[alias] = owner


def _claim_name(name: str, claimant: str) -> None:
    """Guard a primary name against an alias some earlier plugin already claimed.

    The mirror of ``_claim_alias``'s second guard: an alias that shadows a
    harness name is caught there, but a harness *name* that shadows an
    already-claimed alias is caught here. Without it, ``HARNESSES`` gains the
    key but ``normalize`` still routes to the alias owner — registration and
    adoption disagree, and load order silently decides which wins.
    """
    owner = _ALIASES.get(name)
    if owner is not None and owner != name:
        raise ConfigError(
            f"{claimant} registers harness {name!r}, which is already an alias of {owner!r}"
        )


def _unwrap_binary(binary: str) -> str:
    """Strip nixpkgs makeWrapper affixes, matching ``harness_detect._unwrap``.

    Duplicated rather than imported to avoid a circular dependency:
    ``harness_detect`` imports ``HARNESSES`` from this module. The function is
    four lines and stable — drift here would mean the claim guard and the
    matcher disagree, which is the exact bug R5-2 exists to close.
    """
    name = binary.rsplit("/", 1)[-1]
    if name.startswith("."):
        name = name[1:]
    if name.endswith("-wrapped"):
        name = name[: -len("-wrapped")]
    return name


def _claim_binary(binary: str, owner: str, claimant: str) -> None:
    """Claim a binary name for ``owner``, unless another harness already owns it.

    The same class of bug as an alias collision: ``match_binary`` walks
    ``harnesses.values()`` and returns the first adapter whose binary set
    contains the name, so two adapters claiming the same binary are silently
    resolved by iteration order — adoption records the wrong harness, and
    stale-pane verification can report a false match. Refused at load time
    with both files named, the same shape as the alias guard.

    The claim key is the same key the matcher uses: the raw binary, the
    unwrapped basename (leading ``.`` stripped, trailing ``-wrapped``
    stripped), and the basename of a path-shaped declaration. Two harnesses
    whose binaries normalise to the same name collide here, not at match
    time.
    """
    for key in _binary_claim_keys(binary):
        existing = _BINARIES.get(key)
        if existing is not None and existing[0] != owner:
            prev_owner, prev_path = existing
            raise ConfigError(
                f"{claimant} claims binary {key!r} for harness {owner!r}, which is "
                f"already claimed by harness {prev_owner!r} ({prev_path})"
            )
        _BINARIES[key] = (owner, claimant)


def _binary_claim_keys(binary: str) -> set[str]:
    """Every key ``match_binary`` could match ``binary`` under.

    ``match_binary`` tests three forms of the command: the basename, the
    unwrapped basename, and the raw command. A declared binary can be
    matched under any of those, so the claim must cover all of them. This
    is the set of keys that, if any other harness also produces one, means
    ``match_binary`` could return either harness for the same pane.
    """
    basename = binary.rsplit("/", 1)[-1]
    unwrapped = _unwrap_binary(binary)
    keys = {binary, basename, unwrapped}
    keys.discard("")
    return keys


def _release_claims(harness: Harness, name: str) -> None:
    """Remove a superseded harness's binary and observation-key claims.

    When a local plugin overrides a shipped one of the same name, the shipped
    harness's claims must be released before the new ones are recorded.
    Otherwise a binary the override drops stays claimed by a harness that no
    longer wants it, and a later plugin claiming it is refused for no reason a
    user can see.

    Alias claims are not released: an alias resolves to a harness *name*, and
    the override has the same name, so the alias still resolves correctly. The
    override re-claims its own aliases via ``_claim_alias``, which allows
    re-claiming an alias that already points to the same owner.
    """
    for key in _binary_claim_keys(harness.binary):
        existing = _BINARIES.get(key)
        if existing is not None and existing[0] == name:
            del _BINARIES[key]
    for extra in harness.binaries:
        for key in _binary_claim_keys(extra):
            existing = _BINARIES.get(key)
            if existing is not None and existing[0] == name:
                del _BINARIES[key]
    for key in _observation_keys_for(harness.binary):
        existing = _OBSERVATION_KEYS.get(key)
        if existing is not None and existing[0] == name:
            del _OBSERVATION_KEYS[key]
    for extra in harness.binaries:
        for key in _observation_keys_for(extra):
            existing = _OBSERVATION_KEYS.get(key)
            if existing is not None and existing[0] == name:
                del _OBSERVATION_KEYS[key]


def _observation_keys_for(binary: str) -> set[str]:
    """Every 15-character observation key tmux could report for ``binary``.

    tmux truncates ``pane_current_command`` to 15 characters.  For each spelling
    the matcher would accept (the raw binary, its basename, the unwrapped
    basename, and the implicit makeWrapper spellings ``name-wrapped`` and
    ``.name-wrapped``), if that spelling is longer than 15 characters, its first
    15 characters are a potential tmux observation.  An exact 15-character
    spelling is also a key — it is what tmux would report unchanged.

    The makeWrapper spellings are derived because a plugin declares only the
    primary ``binary`` (e.g. ``opencode``), never the wrapper names; the wrapper
    is a nixpkgs convention that adds ``.`` prefix and ``-wrapped`` suffix
    outside the plugin's knowledge.
    """
    basename = binary.rsplit("/", 1)[-1]
    unwrapped = _unwrap_binary(binary)
    # All spellings the matcher would accept, INCLUDING the implicit
    # makeWrapper forms that a plugin never declares but nixpkgs generates.
    spellings: set[str] = {binary, basename, unwrapped}
    spellings.add(f"{unwrapped}-wrapped")
    spellings.add(f".{unwrapped}-wrapped")
    keys: set[str] = set()
    for spelling in spellings:
        base = spelling.rsplit("/", 1)[-1]  # tmux reports the basename, not a path
        if len(base) >= _TMUX_TRUNCATION:
            keys.add(base[:_TMUX_TRUNCATION])
    keys.discard("")
    return keys


def _claim_observation_keys(binary: str, owner: str, claimant: str) -> None:
    """Claim every 15-character observation key for ``owner``.

    Two different harnesses whose binaries truncate to the same 15-character
    form would be silently resolved by iteration order in the pane matcher —
    refused at load time with both files named, the same shape as
    ``_claim_binary``.  An exact 15-character binary name colliding with
    another harness's truncated form is also refused: a 15-character tmux
    observation is genuinely ambiguous and preferring one silently misidentifies
    the other.
    """
    for key in _observation_keys_for(binary):
        existing = _OBSERVATION_KEYS.get(key)
        if existing is not None and existing[0] != owner:
            prev_owner, prev_path = existing
            raise ConfigError(
                f"{claimant} claims observation key {key!r} for harness {owner!r} "
                f"(truncated from {binary!r}), which is already claimed by harness "
                f"{prev_owner!r} ({prev_path}) — tmux truncates pane_current_command "
                f"to 15 characters, so both binaries would appear identical in tmux"
            )
        _OBSERVATION_KEYS[key] = (owner, claimant)


def observation_lookup(key: str) -> str | None:
    """Resolve a 15-character observation to a harness name, or None.

    Called by ``match_binary`` when the observed basename is exactly 15
    characters long — the truncation length shared by tmux's
    ``pane_current_command`` and Linux's ``/proc/<pid>/comm``
    (``TASK_COMM_LEN``).  Both channels can deliver a truncated name:
    tmux truncates the pane command, and ``ps -o comm=`` on Linux reads the
    kernel's 15-character comm value.  So the lookup applies to pane
    commands, root ``ps`` comms, and descendant ``ps`` comms alike.

    The lookup is exact: the key must match a pre-claimed 15-character
    observation key — not a prefix scan.  A false positive requires an
    unrelated process whose (possibly truncated) name is exactly a claimed
    key.  That is narrow but not impossible, and the descendant walk
    examines many processes, widening the exposure.
    """
    entry = _OBSERVATION_KEYS.get(key)
    return entry[0] if entry is not None else None


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


def supports_reasoning(harness: Harness) -> bool:
    """Whether this adapter accepts `reasoning_effort` in `plan_launch`.

    Read off the signature for the same reason `supports_model` is: the
    signature is the thing that is actually true.
    """
    return "reasoning_effort" in inspect.signature(harness.plan_launch).parameters


def check_reasoning(harness: str, reasoning_effort: str | None) -> None:
    """Raise if this harness cannot honour a reasoning-effort request.

    Pure and cheap, so a caller can gate on it *before* creating anything —
    the same contract `check_model` offers.
    """
    if reasoning_effort is not None and not supports_reasoning(get(harness)):
        raise BadRequest(f"harness {harness!r} does not support reasoning effort selection")


def supports_resume(harness: Harness) -> bool:
    """Whether this adapter accepts a `resume` in `plan_launch`.

    Read off the signature rather than declared as a class attribute, for the
    same reason `supports_model` is: the signature is the thing that is
    actually true. An adapter that says it supports resume and does not take
    the parameter would fail at the call, which is the failure this check
    exists to prevent.
    """
    return "resume" in inspect.signature(harness.plan_launch).parameters


def check_resume(harness: str, resume: str | None) -> None:
    """Raise if this harness cannot honour a resume request.

    Pure and cheap, so a caller can gate on it *before* creating anything —
    the same contract `check_model` offers. A resume asked of a harness whose
    `plan_launch` has no `resume` parameter is refused here, by name, rather
    than surfacing as a `TypeError` from inside the plugin.
    """
    if resume is not None and not supports_resume(get(harness)):
        raise BadRequest(f"harness {harness!r} does not support resume")


def plan_launch(
    harness: str,
    *,
    participant_id: str,
    prompt: str,
    config_path: Path,
    approval: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
    resume: str | None = None,
) -> LaunchPlan:
    """The one funnel every spawn goes through, and so the one compat seam.

    `model`, `reasoning_effort`, and `resume` are each forwarded only when the
    caller named one. That is what keeps a third-party adapter written against
    the older signature working: it is never called with a keyword it does not
    accept, and the only launches it cannot serve are the ones that ask for
    something it has no way to deliver. Those are refused here, with a message
    that names the harness, rather than surfacing as a `TypeError` from inside
    the plugin or — far worse — as a child that silently came up on the wrong
    model or dropped the prompt.
    """
    found = get(harness)
    check_model(harness, model)
    check_reasoning(harness, reasoning_effort)
    check_resume(harness, resume)
    extra: dict[str, Any] = {}
    if model is not None:
        extra["model"] = model
    if reasoning_effort is not None:
        extra["reasoning_effort"] = reasoning_effort
    if resume is not None:
        extra["resume"] = resume
    return found.plan_launch(
        participant_id=participant_id,
        prompt=prompt,
        config_path=config_path,
        approval=approval,
        **extra,
    )


#: Shown for a participant whose harness has no adapter — an unmanaged pane
#: or an unrecognised name.
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
    so it should be surfaced rather than invisible. Includes plugin-declared
    ``binaries`` aliases (e.g. wrapper-renamed binaries) when set.
    """
    result: set[str] = set()
    for h in HARNESSES.values():
        result.add(h.binary)
        result |= h.binaries
    return result


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
    "EventPath",
    "Harness",
    "HarnessObserver",
    "History",
    "LaunchPlan",
    "NativeChild",
    "Plugin",
    "PluginError",
    "ResumeLaunchOverlay",
    "ScreenConfidence",
    "ScreenKind",
    "ScreenReading",
    "Source",
    "StreamPoint",
    "TranscriptObserver",
    "TranscriptSource",
    "check_model",
    "check_reasoning",
    "check_resume",
    "clip",
    "clipper",
    "describe",
    "get",
    "harness_icon",
    "install",
    "known_binaries",
    "last_screen_line",
    "normalize",
    "observation_lookup",
    "plan_launch",
    "status_after",
    "supports_model",
    "supports_reasoning",
    "supports_resume",
    "theater_binary",
]
