"""Every declaration a harness manifest can make, enumerated from its types.

A declaration is a dataclass field (``Type.field``) or one alternative of a field whose type is
a ``Literal`` or an ``Enum`` (``Type.field=VALUE``). The live suite must map each to a scenario
family or to a reasoned exemption, so a new capability cannot be added without a decision.
"""

from __future__ import annotations

import dataclasses
import enum
import typing
from collections import abc
from collections.abc import Iterator
from typing import Any, Literal, get_args, get_origin, get_type_hints

from theater.harness.contracts.manifest import HarnessManifest


def declarations(root: type = HarnessManifest) -> frozenset[str]:
    """Walk ``root`` and every dataclass reachable through its field types, each type once."""
    found: set[str] = set()
    pending: list[type] = [root]
    visited: set[type] = set()
    while pending:
        current = pending.pop()
        if current in visited:
            continue
        visited.add(current)
        hints = get_type_hints(current)
        for field in dataclasses.fields(current):
            if field.name.startswith("_"):
                continue
            key = f"{current.__name__}.{field.name}"
            found.add(key)
            for leaf in _leaves(hints[field.name]):
                found.update(f"{key}={value}" for value in _values(leaf))
                if isinstance(leaf, type) and dataclasses.is_dataclass(leaf):
                    pending.append(leaf)
    return frozenset(found)


def _leaves(annotation: Any, aliases: frozenset[Any] = frozenset()) -> Iterator[Any]:
    """What a field can hold: through unions, ``type`` aliases (once each) and generic arguments.

    A ``Literal`` is a leaf (its values are the declarations); a callable is skipped, because the
    callbacks a manifest takes are declared as a whole, never by what they return.
    """
    if isinstance(annotation, typing.TypeAliasType):
        if annotation not in aliases:
            yield from _leaves(annotation.__value__, aliases | {annotation})
        return
    origin = get_origin(annotation)
    if origin is Literal or origin is None:
        yield annotation
    elif origin is not abc.Callable:
        for argument in get_args(annotation):
            yield from _leaves(argument, aliases)


def _values(leaf: Any) -> Iterator[str]:
    if get_origin(leaf) is Literal:
        for value in get_args(leaf):
            yield repr(value)
    elif isinstance(leaf, type) and issubclass(leaf, enum.Enum):
        for member in leaf:
            yield member.name
