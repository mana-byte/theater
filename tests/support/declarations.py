"""Every declaration a harness manifest can make, enumerated from its types.

A declaration is a dataclass field (``Type.field``) or one alternative of a field whose type is
a ``Literal`` or an ``Enum`` (``Type.field=VALUE``). The live suite must map each to a scenario
family or to a reasoned exemption, so a new capability cannot be added without a decision.
"""

from __future__ import annotations

import dataclasses
import enum
import types
import typing
from collections import abc
from collections.abc import Iterator
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

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
            for alternative in _alternatives(hints[field.name]):
                found.update(f"{key}={value}" for value in _values(alternative))
                pending.extend(_dataclasses(alternative))
    return frozenset(found)


def _alternatives(annotation: Any) -> Iterator[Any]:
    """The members of a union (recursively, through ``type`` aliases), or the annotation itself."""
    if isinstance(annotation, typing.TypeAliasType):
        yield from _alternatives(annotation.__value__)
    elif get_origin(annotation) in (Union, types.UnionType):
        for member in get_args(annotation):
            yield from _alternatives(member)
    else:
        yield annotation


def _values(alternative: Any) -> Iterator[str]:
    if get_origin(alternative) is Literal:
        for value in get_args(alternative):
            yield repr(value)
    elif isinstance(alternative, type) and issubclass(alternative, enum.Enum):
        for member in alternative:
            yield member.name


def _dataclasses(annotation: Any) -> Iterator[type]:
    """Dataclasses named in an annotation, through unions and generics but not callables."""
    for alternative in _alternatives(annotation):
        origin = get_origin(alternative)
        if isinstance(alternative, type) and dataclasses.is_dataclass(alternative):
            yield alternative
        elif origin is not None and origin is not Literal and origin is not abc.Callable:
            for argument in get_args(alternative):
                yield from _dataclasses(argument)
