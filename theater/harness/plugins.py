"""Harness adapters loaded from Python files, by path.

Two directories feed the registry, read by this one loader: the plugins Theater
ships (`theater/harness/builtin/plugins/`) and the ones a user writes
(`$THEATER_HOME/harnesses/`). There is no built-in tier. Shipping the default
adapters through the extension point is what keeps the extension point honest —
otherwise it is exercised only by the people who have already committed to it,
and the first time it is short of something is the first time anyone finds out.

Loading is by path, not by package. The alternative — putting the directory on
`sys.path` and importing by name — makes every file in it shadow a top-level
module for the whole process, so a user's `json.py` would break Theater in a way
that names neither the file nor the plugin system. Each file is loaded under a
prefixed synthetic module name instead, which collides with nothing.

Failure is reported, not raised. `scan` never throws: a file that will not load
comes back as a `Plugin` with `error` set and `harness` None. The two sources
have different failure policies — a broken shipped plugin is fatal, a broken
local one is skipped with a warning — and choosing between them is the
registry's job, not the loader's.

Trust: a plugin is arbitrary Python executed by the daemon, at the privileges of
the user who started it. That is the same trust level as `~/.bashrc`, and the
directory is under `$THEATER_HOME` where nothing else writes. Theater does not
sandbox it and does not pretend to.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from theater.config import HARNESS_NAME, ConfigError
from theater.harness.base import Harness
from theater.harness.observation import HarnessObserver

logger = logging.getLogger("theater.harness.plugins")

#: Prefixed so a plugin can never take the import slot of a real module. The
#: source is in the name too, so a local override does not evict the shipped one.
MODULE_PREFIX = "theater_harness_plugin_"

#: Where a plugin came from. "Why is this harness behaving unexpectedly" is
#: usually answered by "not the file you think".
SHIPPED = "shipped"
LOCAL = "local"


class PluginError(ConfigError):
    """A plugin file that cannot be turned into a harness.

    A subclass of `ConfigError` so the CLI and daemon keep one catch site: from
    the user's side "my config declaration is wrong" and "my plugin is wrong"
    are the same event — Theater refusing to start with an explanation.
    """


@dataclass(frozen=True, slots=True)
class Plugin:
    """One plugin file, loaded or not.

    The path travels with the harness because the registry needs it for the
    collision messages: "two definitions of `codex`" is only actionable if it
    says which two files. `source` travels with it for the same reason —
    "which two" is usually one shipped and one local.

    `name` is the harness's own name once it loads, and the file stem before
    that. A file that raises on import cannot be asked what it is called, and
    the whole point of `[harness] disabled` is to be able to switch off the one
    that is breaking start-up.
    """

    path: Path
    source: str
    name: str
    harness: Harness | None = None
    error: str | None = None


def scan(directory: Path, *, source: str, skip: Iterable[str] = ()) -> list[Plugin]:
    """Every plugin in `directory`, in filename order. Never raises.

    A missing directory is not an error: the common case is a user who has
    never written one. Files starting with `_` or `.` are skipped, which is
    what makes a shared helper module possible next to the plugins that use it.

    `skip` holds file stems to not even import — disabling a plugin has to work
    when the reason for disabling it is that importing it is what breaks.
    """
    skipped = set(skip)
    if not directory.is_dir():
        return []

    found: list[Plugin] = []
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith(("_", ".")) or path.stem in skipped:
            continue
        try:
            harness = _load_one(path, source)
        except PluginError as exc:
            found.append(Plugin(path=path, source=source, name=path.stem, error=str(exc)))
            continue
        logger.info("loaded %s harness plugin %r from %s", source, harness.name, path)
        found.append(Plugin(path=path, source=source, name=harness.name, harness=harness))
    return found


def _load_one(path: Path, source: str) -> Harness:
    module_name = f"{MODULE_PREFIX}{source}_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PluginError(f"{path}: not loadable as a Python module")

    module = importlib.util.module_from_spec(spec)
    # Registered before execution: dataclasses and `typing.get_type_hints`
    # resolve annotations via sys.modules, and a plugin using either would fail
    # on its own import line otherwise.
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(module_name, None)
        raise PluginError(f"{path}: failed to import: {exc!r}") from exc

    harness = getattr(module, "HARNESS", None)
    if harness is None:
        raise PluginError(
            f"{path}: defines no HARNESS. A plugin must end with "
            "`HARNESS = MyHarness()` — see docs/harness-plugins.md"
        )
    if isinstance(harness, type):
        raise PluginError(
            f"{path}: HARNESS is the class {harness.__name__}, not an instance "
            f"of it. Use `HARNESS = {harness.__name__}()`"
        )
    if not isinstance(harness, Harness):
        raise PluginError(
            f"{path}: HARNESS is a {type(harness).__name__}, which does not "
            "subclass theater.harness.Harness"
        )
    _check_identity(path, harness)
    _check_observer(path, harness)
    return harness


def _check_observer(path: Path, harness: Harness) -> None:
    """The harness must carry the object that knows how to watch it.

    `Harness.observer` is an annotation rather than an abstract property, so
    Python will not refuse to instantiate a harness that forgot it — the
    omission would instead surface as an `AttributeError` inside the daemon's
    watch loop, minutes later, blamed on the reducer. Declaring it abstract was
    the alternative and it costs every plugin four lines of property ceremony
    to return a value the constructor already has; checking it here, beside the
    other attributes every consumer reads without asking, costs the plugin
    nothing and fails at load with the file name in hand.
    """
    observer = getattr(harness, "observer", None)
    if observer is None:
        raise PluginError(
            f"{path}: harness {harness.name!r} sets no observer. A harness "
            "must assign one in __init__ — `self.observer = MyObserver()`; "
            "see docs/harness-plugins.md"
        )
    if isinstance(observer, type):
        raise PluginError(
            f"{path}: harness {harness.name!r} sets observer to the class "
            f"{observer.__name__}, not an instance of it"
        )
    if not isinstance(observer, HarnessObserver):
        raise PluginError(
            f"{path}: harness {harness.name!r} has a "
            f"{type(observer).__name__} observer, which does not subclass "
            "theater.harness.HarnessObserver"
        )


def _check_identity(path: Path, harness: Harness) -> None:
    """The three attributes every consumer reads without asking first.

    Checked here rather than trusted, because each one fails far from the
    plugin: an empty `name` makes a harness nothing can spawn, a missing
    `binary` makes `theater harnesses` claim it is not installed, and a
    multi-character `icon` shifts every column of a listing by one.
    """
    name = getattr(harness, "name", None)
    if not isinstance(name, str) or not HARNESS_NAME.match(name):
        raise PluginError(
            f"{path}: harness name {name!r} must be lowercase letters, digits, "
            "'-' or '_', starting with a letter or digit"
        )
    binary = getattr(harness, "binary", None)
    if not isinstance(binary, str) or not binary:
        raise PluginError(f"{path}: harness {name!r} sets no binary to look for")
    icon = getattr(harness, "icon", "")
    if not isinstance(icon, str) or len(icon) != 1:
        raise PluginError(
            f"{path}: harness {name!r} has icon {icon!r}; it must be exactly "
            "one character, since listings align on it"
        )
    for alias in harness.aliases:
        if not isinstance(alias, str) or not alias:
            raise PluginError(f"{path}: harness {name!r} has an empty alias")
