"""A strict response contract: a resolved schema, its stored envelope, and whole-answer checks."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import unquote

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from theater.constants.response_contract import (
    CONTRACT_ANSWER_MAX_CHARS,
    CONTRACT_DETAIL_MAX_CHARS,
    CONTRACT_DRAFT_URI,
    CONTRACT_ERRORS_MAX,
    CONTRACT_FORBIDDEN_KEYWORDS,
    CONTRACT_PRESET_ANSWER_MAX_CHARS,
    CONTRACT_PRESET_NAMES,
    CONTRACT_SCHEMA_MAX_BYTES,
    CONTRACT_TAG,
)
from theater.models import BadRequest


def _dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


@dataclass(frozen=True, slots=True)
class Verdict:
    """The outcome of checking one final answer; ``reason`` is a bounded machine label."""

    ok: bool
    reason: str = ""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class StrictContract:
    """A resolved strict contract. The job stores :meth:`envelope`, so it snapshots the schema."""

    kind: str
    schema_text: str
    name: str | None = None

    @property
    def label(self) -> str:
        return self.kind if self.name is None else f"{self.kind}:{self.name}"

    @property
    def answer_cap(self) -> int:
        """Preset answers are the caller's whole input, so they stay shorter than custom ones."""
        if self.kind in CONTRACT_PRESET_NAMES:
            return CONTRACT_PRESET_ANSWER_MAX_CHARS
        return CONTRACT_ANSWER_MAX_CHARS

    @property
    def schema(self) -> dict:
        return json.loads(self.schema_text)

    def envelope(self) -> str:
        body: dict[str, object] = {CONTRACT_TAG: self.kind, "schema": self.schema}
        if self.name is not None:
            body["name"] = self.name
        return _dumps(body)

    def validate(self, text: str | None) -> Verdict:
        """Check the WHOLE answer: one bare JSON value that satisfies the schema."""
        if text is None or not text.strip():
            return Verdict(False, "empty", "the final answer was empty")
        if len(text) > self.answer_cap:
            return Verdict(
                False,
                "too_large",
                f"the final answer exceeds {self.answer_cap} characters; shorten it",
            )
        try:
            value = json.loads(text, parse_constant=_reject_constant)
        except (ValueError, RecursionError) as exc:
            detail = f"the final answer is not a single bare JSON value: {_clip(str(exc), 200)}"
            return Verdict(False, "not_json", detail)
        try:
            errors = sorted(
                _validator(self.schema_text).iter_errors(value),
                key=lambda error: [str(part) for part in error.absolute_path],
            )[:CONTRACT_ERRORS_MAX]
        except RecursionError:
            return Verdict(False, "schema", "the answer could not be checked against the schema")
        if not errors:
            return Verdict(True)
        listed = "; ".join(
            f"{'/'.join(str(p) for p in error.absolute_path) or '(root)'}: "
            f"{_clip(error.message, 200)}"
            for error in errors
        )
        return Verdict(False, "schema", _clip(listed, CONTRACT_DETAIL_MAX_CHARS))


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r}")


@lru_cache(maxsize=128)
def _validator(schema_text: str) -> Draft202012Validator:
    return Draft202012Validator(json.loads(schema_text))


@lru_cache(maxsize=256)
def parse_stored(raw: str | None) -> StrictContract | None:
    """The strict contract a job's stored ``response_format`` carries; ``None`` for a hint."""
    if not raw or CONTRACT_TAG not in raw:
        return None
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(body, dict) or not isinstance(body.get(CONTRACT_TAG), str):
        return None
    schema = body.get("schema")
    if not isinstance(schema, dict):
        return None
    name = body.get("name")
    return StrictContract(
        kind=body[CONTRACT_TAG],
        schema_text=_dumps(schema),
        name=name if isinstance(name, str) else None,
    )


# ---- admission ------------------------------------------------------------

_SCHEMA_VALUED = (
    "additionalProperties",
    "additionalItems",
    "items",
    "contains",
    "not",
    "if",
    "then",
    "else",
    "propertyNames",
    "unevaluatedItems",
    "unevaluatedProperties",
    "contentSchema",
)
_SCHEMA_LISTS = ("allOf", "anyOf", "oneOf", "prefixItems")
_SCHEMA_MAPS = ("properties", "$defs", "definitions", "dependentSchemas", "patternProperties")


def _subschemas(schema: dict) -> Iterator[dict]:
    """Every schema position, never a property name or a literal such as ``enum``."""
    stack: list[object] = [schema]
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        yield node
        for key in _SCHEMA_VALUED:
            value = node.get(key)
            stack.extend(value if isinstance(value, list) else [value])
        for key in _SCHEMA_LISTS:
            value = node.get(key)
            if isinstance(value, list):
                stack.extend(value)
        for key in _SCHEMA_MAPS:
            value = node.get(key)
            if isinstance(value, dict):
                stack.extend(value.values())


def _resolves(root: dict, ref: str) -> bool:
    node: object = root
    if ref == "#":
        return True
    for part in ref[2:].split("/"):
        key = unquote(part).replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and key in node:
            node = node[key]
        elif isinstance(node, list) and key.isdigit() and int(key) < len(node):
            node = node[int(key)]
        else:
            return False
    return True


def admit_schema(schema: object) -> str:
    """Canonical text of a schema a strict job may use, or a ``BadRequest`` saying why not."""
    if not isinstance(schema, dict):
        raise BadRequest("a response contract schema must be a JSON object")
    try:
        text = _dumps(schema)
    except (ValueError, TypeError, RecursionError):
        raise BadRequest("a response contract schema must be finite, serializable JSON") from None
    if len(text.encode("utf-8")) > CONTRACT_SCHEMA_MAX_BYTES:
        raise BadRequest(
            f"a response contract schema may not exceed {CONTRACT_SCHEMA_MAX_BYTES} bytes"
        )
    declared = schema.get("$schema")
    if declared is not None and declared != CONTRACT_DRAFT_URI:
        raise BadRequest(
            f"strict schemas are JSON Schema 2020-12; drop $schema or use {CONTRACT_DRAFT_URI}"
        )
    for node in _subschemas(schema):
        forbidden = CONTRACT_FORBIDDEN_KEYWORDS & node.keys()
        if forbidden:
            raise BadRequest(
                f"strict response schemas may not use {sorted(forbidden)}: regexes and "
                "uniqueness scans run on the daemon loop, and identifiers or dynamic "
                "references reach outside the schema"
            )
        ref = node.get("$ref")
        if ref is not None and not (
            isinstance(ref, str) and (ref == "#" or ref.startswith("#/")) and _resolves(schema, ref)
        ):
            raise BadRequest(
                f"unresolvable $ref {ref!r}: only '#' or a '#/json/pointer' into the schema"
            )
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise BadRequest(
            f"response_format schema is not valid JSON Schema 2020-12: {_clip(exc.message, 300)}"
        ) from None
    validator = _validator(text)
    probes: tuple[object, ...] = ({}, [], "", 0, None)
    for probe in probes:
        try:
            next(iter(validator.iter_errors(probe)), None)
        except RecursionError:
            raise BadRequest(
                "response_format schema recurses without consuming the answer"
            ) from None
    return text
