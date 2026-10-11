"""A strict response contract: a resolved schema, its stored envelope, and whole-answer checks."""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from itertools import islice

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from theater.constants.response_contract import (
    CONTRACT_ANSWER_MAX_CHARS,
    CONTRACT_ANSWER_MAX_VALUES,
    CONTRACT_DETAIL_MAX_CHARS,
    CONTRACT_DRAFT_URI,
    CONTRACT_ERRORS_MAX,
    CONTRACT_PRESET_ANSWER_MAX_CHARS,
    CONTRACT_PRESET_NAMES,
    CONTRACT_SCHEMA_MAX_BYTES,
    CONTRACT_SCHEMA_MAX_DEPTH,
    CONTRACT_TAG,
)
from theater.daemon.response_contracts.schema_rules import (
    nesting_depth,
    schema_problem,
    value_count,
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
        compiled = _compiled(self.schema_text)
        if isinstance(compiled, str):
            return Verdict(
                False,
                "contract_unavailable",
                f"the stored schema cannot be evaluated: {compiled}",
            )
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
        if value_count(value, CONTRACT_ANSWER_MAX_VALUES) > CONTRACT_ANSWER_MAX_VALUES:
            return Verdict(
                False,
                "too_large",
                f"the final answer holds more than {CONTRACT_ANSWER_MAX_VALUES} values; shorten it",
            )
        try:
            found = list(islice(compiled.iter_errors(value), _ERRORS_SCANNED))
        except Exception:  # a schema the validator cannot evaluate must never strand the job
            return Verdict(
                False, "contract_unavailable", "the answer could not be checked against the schema"
            )
        errors = sorted(found, key=lambda error: [str(part) for part in error.absolute_path])
        if not errors:
            return Verdict(True)
        listed = "; ".join(
            f"{'/'.join(str(p) for p in error.absolute_path) or '(root)'}: "
            f"{_clip(error.message, 200)}"
            for error in errors[:CONTRACT_ERRORS_MAX]
        )
        return Verdict(False, "schema", _clip(listed, CONTRACT_DETAIL_MAX_CHARS))


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r}")


#: Errors read before sorting; enough to quote the first few, never all of a hostile answer's.
_ERRORS_SCANNED = 50


@lru_cache(maxsize=128)
def _compiled(schema_text: str) -> Draft202012Validator | str:
    """The validator for a schema inside the strict subset, or the reason it is not."""
    schema = json.loads(schema_text)
    problem = schema_problem(schema) if isinstance(schema, dict) else "the schema is not an object"
    return problem if problem is not None else Draft202012Validator(schema)


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


def admit_schema(schema: object) -> str:
    """Canonical text of a schema a strict job may use, or a ``BadRequest`` saying why not."""
    if not isinstance(schema, dict):
        raise BadRequest("a response contract schema must be a JSON object")
    if nesting_depth(schema) > CONTRACT_SCHEMA_MAX_DEPTH:
        raise BadRequest(
            f"a response contract schema may nest at most {CONTRACT_SCHEMA_MAX_DEPTH} levels"
        )
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
    problem = schema_problem(schema)
    if problem is not None:
        raise BadRequest(problem)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise BadRequest(
            f"response_format schema is not valid JSON Schema 2020-12: {_clip(exc.message, 300)}"
        ) from None
    except RecursionError:
        raise BadRequest("response_format schema nests too deeply to check") from None
    return text
