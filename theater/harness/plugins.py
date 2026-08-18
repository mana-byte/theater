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

import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import logging
import sys
import types
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from theater.config import HARNESS_NAME, ConfigError
from theater.formatting import display_width as _display_width
from theater.harness.base import Harness
from theater.harness.observation import HarnessObserver

logger = logging.getLogger("theater.harness.plugins")

#: Prefixed so a plugin can never take the import slot of a real module. The
#: source is in the name too, so a local override does not evict the shipped one.
MODULE_PREFIX = "theater_harness_plugin_"

#: Prefixed so a ``_``-helper module can never take the import slot of a real
#: module. Keyed by source and a hash of the directory path, so two same-named
#: helpers in two scanned directories cannot collide — each gets its own
#: mangled name in ``sys.modules``.
HELPER_PREFIX = "theater_harness_helper_"

#: Where a plugin came from. "Why is this harness behaving unexpectedly" is
#: usually answered by "not the file you think".
SHIPPED = "shipped"
LOCAL = "local"


def _helper_module_name(directory: Path, source: str, stem: str) -> str:
    """Mangled name for a ``_``-helper, keyed by source and directory.

    Two same-named helpers in two scanned directories get distinct mangled
    names, so neither evicts the other from ``sys.modules``.
    """
    digest = hashlib.md5(str(directory).encode(), usedforsecurity=False).hexdigest()[:8]
    return f"{HELPER_PREFIX}{source}_{digest}_{stem}"


def _is_our_helper(module: object) -> bool:
    """Whether ``module`` is one of our mangled helper modules.

    Used by ``_HelperFinder.__enter__`` to distinguish a pre-existing helper
    (left from a previous scan, safe to shadow) from a real stdlib module
    like ``_json`` that must be refused.
    """
    name = getattr(module, "__name__", "")
    return isinstance(name, str) and name.startswith(HELPER_PREFIX)


class _HelperFinder(importlib.abc.MetaPathFinder):
    """Resolve ``import _shared`` to a helper beside the plugin, lazily.

    Sits on ``sys.meta_path`` only for the duration of a single plugin's
    ``exec_module``. When the plugin (or a helper) imports a ``_``-prefixed
    name that exists as a ``.py`` in the plugin's directory, the finder loads
    it under a mangled name keyed by source and directory. The import system
    then installs the module under the bare name in ``sys.modules``; ``close``
    removes those bare names afterward so they do not leak across plugins or
    directories.

    Helpers are loaded lazily — only when a plugin or another helper actually
    imports them — so a helper nobody imports is never executed, and a broken
    helper's error surfaces at the import that triggered it, naming the
    helper file.

    Used as a context manager: ``with _HelperFinder(...) as finder: …`` installs
    it on ``sys.meta_path`` and removes it on exit, along with any bare helper
    names it resolved. The resolved names are available as ``finder.resolved``
    for callers that need to know what was loaded.
    """

    def __init__(self, directory: Path, source: str) -> None:
        self._directory = directory
        self._source = source
        self.resolved: set[str] = set()

    def __enter__(self) -> _HelperFinder:
        for helper_path in sorted(self._directory.glob("_*.py")):
            if helper_path.name.startswith("__"):
                continue
            stem = helper_path.stem
            existing = sys.modules.get(stem)
            if existing is not None and not isinstance(existing, types.ModuleType):
                continue
            if existing is not None and not _is_our_helper(existing):
                raise PluginError(
                    f"{helper_path}: helper name {stem!r} is already loaded as a "
                    f"module ({existing.__name__!r}); rename the helper to avoid "
                    "the collision"
                )
        sys.meta_path.insert(0, self)
        return self

    def __exit__(self, *exc: object) -> None:
        sys.meta_path.remove(self)
        for name in self.resolved:
            sys.modules.pop(name, None)

    def find_spec(
        self, fullname: str, path: object, target: object = None
    ) -> importlib.machinery.ModuleSpec | None:
        if not fullname.startswith("_") or fullname.startswith("__"):
            return None
        helper_path = self._directory / f"{fullname}.py"
        if not helper_path.is_file():
            return None
        mangled = _helper_module_name(self._directory, self._source, fullname)
        cached = sys.modules.get(mangled)
        if cached is None:
            spec = importlib.util.spec_from_file_location(mangled, helper_path)
            if spec is None or spec.loader is None:
                return None
            mod = importlib.util.module_from_spec(spec)
            sys.modules[mangled] = mod
            try:
                spec.loader.exec_module(mod)
            except BaseException as exc:
                sys.modules.pop(mangled, None)
                raise PluginError(
                    f"{helper_path}: failed to import helper {fullname!r}: {exc!r}"
                ) from exc
        self.resolved.add(fullname)
        return importlib.machinery.ModuleSpec(
            fullname, _AliasLoader(sys.modules[mangled]), origin=str(helper_path)
        )


class _AliasLoader(importlib.abc.Loader):
    """Return an already-loaded mangled helper for a bare import name.

    ``exec_module`` is a no-op — the module is already fully loaded under its
    mangled name. ``create_module`` returns the module so the import system
    installs it in the importing module's namespace.

    A side effect of returning an already-executed module is that the import
    system rewrites the cached module's ``__spec__.name`` to the bare name
    and its loader to this class, while ``__name__`` and the durable
    ``sys.modules`` key stay mangled. After the bare key is popped by
    ``_HelperFinder.__exit__``, ``importlib.reload(helper)`` would raise
    ``ImportError`` because the bare name is no longer in ``sys.modules``.
    Nothing in Theater reloads a helper, so this is not a live defect, but
    it is a known limitation of this approach.
    """

    def __init__(self, module: object) -> None:
        self._module = module

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> types.ModuleType:
        assert isinstance(self._module, types.ModuleType)
        return self._module

    def exec_module(self, module: object) -> None:
        pass


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
    says which two files. `source` travels with it for the same reason.

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
    never written one. Files starting with `_` or `.` are skipped, which keeps
    a shared helper module from being mistaken for a plugin. A ``_``-prefixed
    helper beside a plugin is importable (``import _shared``): while the
    plugin loads, a meta path finder resolves ``_``-prefixed imports to
    ``.py`` files in the plugin's directory, loading each under a mangled name
    keyed by source and directory. Helpers are loaded lazily — a helper nobody
    imports is never executed. After the plugin loads, the finder and any bare
    helper names it resolved in ``sys.modules`` are removed, so two same-named
    helpers in two scanned directories cannot collide. Helper modules are
    cached in ``sys.modules`` under their mangled names for the life of the
    process, so a rescan reuses them rather than re-executing.

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
        except (PluginError, SystemExit) as exc:
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
    # A ``_``-prefixed helper beside the plugin is importable (``import _shared``)
    # via a meta path finder that loads it lazily under a mangled name keyed by
    # source and directory. No bare helper name survives in ``sys.modules``
    # after the plugin loads — the context manager removes the finder and any
    # bare names it resolved. Helpers are loaded lazily: a helper nobody
    # imports is never executed, and a broken helper's error names the helper
    # file. A helper may import another helper in the same directory.
    with _HelperFinder(path.parent, source):
        try:
            spec.loader.exec_module(module)
        except (Exception, SystemExit) as exc:
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
    if not isinstance(icon, str) or not icon or not icon.isprintable():
        raise PluginError(
            f"{path}: harness {name!r} has icon {icon!r}; it must contain only "
            "printable codepoints, since listings align on it"
        )
    width = _display_width(icon)
    if width != 1:
        raise PluginError(
            f"{path}: harness {name!r} has icon {icon!r} with an estimated display "
            f"width of {width} terminal cells; an icon must occupy exactly one "
            "cell so every column of `theater harnesses` lines up. Use a narrow "
            "glyph (one cell wide), not a wide emoji or a multi-character string."
        )
    _check_aliases(path, harness, name)
    _check_binaries(path, harness, name)


def _check_aliases(path: Path, harness: Harness, name: str) -> None:
    """Validate and normalise ``harness.aliases`` into a ``tuple[str, ...]``."""
    aliases: object = getattr(harness, "aliases", None)
    if isinstance(aliases, (str, bytes)):
        raise PluginError(
            f"{path}: harness {name!r} has aliases of type {type(aliases).__name__}; "
            "a string is an iterable of characters, not a list of names. "
            'Use `aliases = ("name", ...)`'
        )
    try:
        normalised: tuple[str, ...] = tuple(aliases)  # type: ignore[arg-type]
    except TypeError:
        raise PluginError(
            f"{path}: harness {name!r} has aliases of type "
            f"{type(aliases).__name__}, which is not iterable. "
            'Use `aliases = ("name", ...)`'
        ) from None
    except Exception as exc:
        raise PluginError(
            f"{path}: harness {name!r} aliases could not be consumed: {exc!r}"
        ) from exc
    for alias in normalised:
        if not isinstance(alias, str) or not alias:
            raise PluginError(f"{path}: harness {name!r} has an empty alias")
    try:
        harness.aliases = normalised
    except Exception as exc:
        raise PluginError(f"{path}: harness {name!r} aliases could not be set: {exc!r}") from exc


def _check_binaries(path: Path, harness: Harness, name: str) -> None:
    """Validate and normalise ``harness.binaries`` into a ``frozenset[str]``."""
    binaries: object = getattr(harness, "binaries", frozenset())
    if isinstance(binaries, (str, bytes)):
        raise PluginError(
            f"{path}: harness {name!r} has binaries of type {type(binaries).__name__}; "
            "a string is an iterable of characters, not a set of names. "
            'Use `binaries = frozenset({"name", ...})`'
        )
    try:
        normalised: frozenset[str] = frozenset(binaries)  # type: ignore[call-overload]
    except TypeError as exc:
        if "unhashable" in str(exc).lower():
            raise PluginError(
                f"{path}: harness {name!r} has a binaries element that is not "
                f'hashable: {exc!r}. Use `binaries = frozenset({{"name", ...}})`'
            ) from exc
        raise PluginError(
            f"{path}: harness {name!r} has binaries of type "
            f"{type(binaries).__name__}, which is not iterable. "
            'Use `binaries = frozenset({"name", ...})`'
        ) from None
    except Exception as exc:
        raise PluginError(
            f"{path}: harness {name!r} binaries could not be consumed: {exc!r}"
        ) from exc
    for binary_name in normalised:
        if not isinstance(binary_name, str) or not binary_name:
            raise PluginError(f"{path}: harness {name!r} has an empty binary name")
    try:
        harness.binaries = normalised
    except Exception as exc:
        raise PluginError(f"{path}: harness {name!r} binaries could not be set: {exc!r}") from exc
