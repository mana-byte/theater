"""Pin Pi's own-scope dependencies to the versions upstream built the release with."""

from __future__ import annotations

import re
from collections.abc import Mapping

_SCOPE = "@earendil-works/"
_SIMPLE_RANGE = re.compile(r"[\^~](\d+\.\d+\.\d+)")


def pinned_specs(dependencies: object) -> list[str]:
    """Return ``name@x.y.z`` for each own-scope dependency whose range is a simple caret/tilde."""
    if not isinstance(dependencies, Mapping):
        return []
    specs = []
    for name, spec in dependencies.items():
        if not (isinstance(name, str) and name.startswith(_SCOPE) and isinstance(spec, str)):
            continue
        if match := _SIMPLE_RANGE.fullmatch(spec.strip()):
            specs.append(f"{name}@{match.group(1)}")
    return sorted(specs)
