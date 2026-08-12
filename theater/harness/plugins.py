"""Harness adapters written in Python and dropped into `$THEATER_HOME/harnesses`.

A config declaration (`[harness.*]`) can describe how to *launch* a harness, but
not how to read what it writes: it has no transcript parser, so the observer
falls back to reading the rendered screen and a job finishes on a guess rather
than on a record. A plugin is the escape hatch for anyone who needs the real
thing — the full `Harness` ABC, including `parse`.

Loading is by path, not by package. The alternative — putting the directory on
`sys.path` and importing by name — makes every file in it shadow a top-level
module for the whole process, so a user's `json.py` would break Theater in a way
that names neither the file nor the plugin system. Each file is loaded under a
prefixed synthetic module name instead, which collides with nothing.

Failure is loud, for the same reason a bad config key is: a plugin the user
believes they installed but which is silently absent is the exact defect this
release exists to remove. A plugin that raises on import, defines no `HARNESS`,
or defines one that is not a `Harness` stops start-up with the file path in the
message.

Trust: a plugin is arbitrary Python executed by the daemon, at the privileges of
the user who started it. That is the same trust level as `~/.bashrc`, and the
directory is under `$THEATER_HOME` where nothing else writes. Theater does not
sandbox it and does not pretend to.
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

from theater.config import HARNESS_NAME, ConfigError
from theater.harness.base import Harness

logger = logging.getLogger("theater.harness.plugins")

#: Synthetic module names are prefixed so a plugin can never take the import
#: slot of a real module — see the loading note in the module docstring.
MODULE_PREFIX = "theater_harness_plugin_"


class PluginError(ConfigError):
    """A plugin file that cannot be turned into a harness.

    A subclass of `ConfigError` so the CLI and daemon keep one catch site: from
    the user's side "my config declaration is wrong" and "my plugin is wrong"
    are the same event — Theater refusing to start with an explanation.
    """


def load(directory: Path) -> list[tuple[Path, Harness]]:
    """Every plugin in `directory`, in filename order, paired with its file.

    The path travels with the harness because the registry needs it for the
    collision messages: "two definitions of `codex`" is only actionable if it
    says which two files.

    A missing directory is not an error: the common case is a user who has
    never written one. Files starting with `_` or `.` are skipped, which is
    what makes a shared helper module possible next to the plugins that use it.
    """
    if not directory.is_dir():
        return []

    found: list[tuple[Path, Harness]] = []
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith(("_", ".")):
            continue
        harness = _load_one(path)
        logger.info("loaded harness plugin %r from %s", harness.name, path)
        found.append((path, harness))
    return found


def _load_one(path: Path) -> Harness:
    module_name = MODULE_PREFIX + path.stem
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PluginError(f"{path}: not loadable as a Python module")

    module = importlib.util.module_from_spec(spec)
    # Registered before execution, not after: dataclasses and
    # `typing.get_type_hints` resolve annotations by looking the module up in
    # sys.modules, and a plugin that uses either would fail on its own import
    # line with an error naming neither.
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
    return harness


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
