"""TOML/path loading and per-key source tracking.

`load()` reads the file (or returns defaults when it is absent) and threads
each parsed value back into a `Config` with a `sources` map that records
whether each dotted key came from `"default"` or `"config.toml"`. The
per-section building and rejection live in `validation.py`; this module owns
the file-shaped half: locating the path, reading TOML, and assembling the
resolved `Config`.
"""

from __future__ import annotations

import tomllib
from dataclasses import fields
from pathlib import Path
from typing import Any

from theater import paths
from theater.config.models import (
    _SECTIONS,
    MCP_SECTION,
    MODELS_SECTION,
    REASONING_SECTION,
    Config,
)
from theater.config.validation import (
    ConfigError,
    _build_mcp,
    _build_models,
    _build_reasoning,
    _build_section,
    _check_no_declarations,
    _fail,
    _suggest,
)


def _defaults_for(name: str, cls: type) -> dict[str, str]:
    return {f"{name}.{f.name}": "default" for f in fields(cls)}


def load(path: Path | None = None) -> Config:
    """Read the config file, or return defaults if it is not there.

    A missing file is the normal case and not an error. A file that exists but
    is malformed, or names a key Theater does not have, raises `ConfigError` —
    see the package docstring for why that is not a warning.
    """
    target = path or paths.config_path()

    sources: dict[str, str] = {}
    for name, cls in _SECTIONS.items():
        sources.update(_defaults_for(name, cls))
    sources[f"{MCP_SECTION}.enabled"] = "default"

    if not target.exists():
        return Config(sources=sources, path=target, exists=False)

    try:
        raw = tomllib.loads(target.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{target}: not valid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{target}: cannot read: {exc}") from exc

    legal = [*_SECTIONS, MCP_SECTION, MODELS_SECTION, REASONING_SECTION]
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

    reasoning: dict[str, list[str]] = {}
    if REASONING_SECTION in raw:
        reasoning, reasoning_sources = _build_reasoning(target, raw[REASONING_SECTION])
        sources.update(reasoning_sources)

    if MCP_SECTION in raw:
        mcp, mcp_sources = _build_mcp(target, raw[MCP_SECTION])
        sources.update(mcp_sources)
        built[MCP_SECTION] = mcp

    return Config(
        **built,
        models=models,
        reasoning=reasoning,
        sources=sources,
        path=target,
        exists=True,
    )
