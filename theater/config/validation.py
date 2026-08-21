"""Type, name, and range validation for config sections.

Owns `ConfigError` (the one fatal start-up error the config layer raises) and
the per-field checkers dispatched on the written annotation. The known-key set
is whatever `models._SECTIONS` enumerates plus the two harness-keyed sections,
so a new setting cannot be added without its validation landing here too.

`[models]` and `[reasoning]` are keyed by harness name rather than by field, so
they cannot go through `_build_section`; their shape is checked here
(`_build_models`, `_build_reasoning`) and whether the harness exists is left to
the daemon, which checks it at start-up once the registry is built.
"""

from __future__ import annotations

import difflib
import math
from dataclasses import fields
from pathlib import Path
from typing import Any, NoReturn, cast

from theater.config.models import MODELS_SECTION, REASONING_SECTION
from theater.constants.core import HARNESS_NAME


class ConfigError(Exception):
    """The config file exists but cannot be honoured.

    Always fatal at start-up. Carries the file path because the daemon that
    raises it is frequently not the process the user is looking at.
    """


def _fail(path: Path, message: str) -> NoReturn:
    raise ConfigError(f"{path}: {message}")


def _suggest(name: str, known: list[str]) -> str:
    close = difflib.get_close_matches(name, known, n=1, cutoff=0.6)
    if close:
        return f"did you mean {close[0]!r}?"
    return "known keys: " + ", ".join(sorted(known))


def _check_bool(value: Any) -> bool | None:
    # An integer is not a truth value here; `bus_visible = 1` is a type error.
    return value if isinstance(value, bool) else None


def _check_int(value: Any) -> int | None:
    # bool is a subclass of int, so `depth_cap = true` would otherwise parse as 1.
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


#: Dispatch on the written form; annotations are strings under `from __future__ import annotations`.
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
        # `f.type` is the written annotation, always a string here — see _CHECKERS.
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
        # A duplicate is a copy-paste artefact and would show up twice.
        if len(set(names)) != len(names):
            dupe = next(n for n in names if names.count(n) > 1)
            _fail(path, f"'{dotted}' lists {dupe!r} more than once")
        out[name] = names
        sources[dotted] = "config.toml"
    return out, sources


def _build_reasoning(path: Path, raw: Any) -> tuple[dict[str, list[str]], dict[str, str]]:
    """Parse `[reasoning]`, which has the same shape as `[models]`.

    Kept separate from `_build_models` only so the two config sections can
    appear independently — a user may want to allowlist models without
    allowlisting reasoning efforts, or vice versa.
    """
    if not isinstance(raw, dict):
        _fail(path, f"[{REASONING_SECTION}] must be a table, got {type(raw).__name__}")

    out: dict[str, list[str]] = {}
    sources: dict[str, str] = {}
    for name, value in raw.items():
        dotted = f"{REASONING_SECTION}.{name}"
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
