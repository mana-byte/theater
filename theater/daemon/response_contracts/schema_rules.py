"""The schema subset a strict contract may use, and the checks that keep validation bounded.

With no references and no branching, every answer value meets exactly one schema node, so the
work is the answer's size times the per-node caps — never exponential, never a regex.
"""

from __future__ import annotations

from theater.constants.response_contract import (
    CONTRACT_ALLOWED_KEYWORDS,
    CONTRACT_SCHEMA_MAX_LIST,
)

_EXCLUDED = (
    (
        frozenset(
            {
                "$ref",
                "$dynamicRef",
                "$recursiveRef",
                "$anchor",
                "$dynamicAnchor",
                "$id",
                "$defs",
                "definitions",
            }
        ),
        "references and identifiers can recurse or reach outside the schema",
    ),
    (
        frozenset({"anyOf", "oneOf", "allOf", "not", "if", "then", "else", "dependentSchemas"}),
        "branching lets one answer value meet many subschemas",
    ),
    (
        frozenset(
            {
                "pattern",
                "patternProperties",
                "uniqueItems",
                "contains",
                "minContains",
                "maxContains",
                "propertyNames",
                "unevaluatedItems",
                "unevaluatedProperties",
                "additionalItems",
            }
        ),
        "regexes and multi-visit scans run on the daemon loop",
    ),
)
_ALLOWED_LISTS = ("enum", "required", "prefixItems")


def nesting_depth(value: object) -> int:
    """JSON nesting depth of ``value``, counted without recursion."""
    deepest = 0
    stack: list[tuple[object, int]] = [(value, 1)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            children: list[object] = list(node.values())
        elif isinstance(node, list):
            children = list(node)
        else:
            continue
        deepest = max(deepest, depth)
        stack.extend((child, depth + 1) for child in children)
    return deepest


def value_count(value: object, limit: int) -> int:
    """Values in a parsed answer, stopping once ``limit`` is exceeded."""
    count = 0
    stack: list[object] = [value]
    while stack:
        node = stack.pop()
        count += 1
        if count > limit:
            return count
        if isinstance(node, dict):
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return count


def _children(node: dict) -> list[object]:
    found: list[object] = []
    properties = node.get("properties")
    if isinstance(properties, dict):
        found.extend(properties.values())
    for key in ("additionalProperties", "items"):
        found.append(node.get(key))
    prefix = node.get("prefixItems")
    if isinstance(prefix, list):
        found.extend(prefix)
    return found


def schema_problem(schema: dict) -> str | None:
    """Why ``schema`` is outside the strict subset, or ``None`` when it is inside."""
    stack: list[object] = [schema]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        allowed = (
            CONTRACT_ALLOWED_KEYWORDS | {"$schema"} if node is schema else CONTRACT_ALLOWED_KEYWORDS
        )
        unsupported = sorted(node.keys() - allowed)
        if unsupported:
            reasons = sorted({why for keys, why in _EXCLUDED if keys & set(unsupported)}) or [
                "it is not part of the supported subset"
            ]
            return (
                f"strict response schemas may not use {unsupported}: {'; '.join(reasons)}. "
                f"They allow only {sorted(CONTRACT_ALLOWED_KEYWORDS)}"
            )
        for key in _ALLOWED_LISTS:
            listed = node.get(key)
            if isinstance(listed, list) and len(listed) > CONTRACT_SCHEMA_MAX_LIST:
                return f"{key!r} may list at most {CONTRACT_SCHEMA_MAX_LIST} entries"
        properties = node.get("properties")
        if isinstance(properties, dict) and len(properties) > CONTRACT_SCHEMA_MAX_LIST:
            return f"'properties' may name at most {CONTRACT_SCHEMA_MAX_LIST} properties"
        stack.extend(_children(node))
    return None
