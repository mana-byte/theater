"""Admission: turn the one ``response_format`` argument into the string a job stores."""

from __future__ import annotations

import json
from typing import Any

from theater.constants.response_contract import (
    CONTRACT_KIND_CUSTOM,
    CONTRACT_MAX_CUSTOM_PER_CREATOR,
    CONTRACT_NAME,
    CONTRACT_PRESET_NAMES,
    CONTRACT_TAG,
)
from theater.daemon.response_contracts.contract import StrictContract, admit_schema
from theater.daemon.response_contracts.presets import PRESET_SCHEMAS
from theater.harness import HARNESSES, normalize
from theater.models import BadRequest, Status, now

_CUSTOM_KEYS = frozenset({CONTRACT_TAG, "name", "schema"})


def resolve_response_format(store: Any, raw: object, *, creator_id: str | None) -> str | None:
    """Validate ``raw`` and return the serialized form the job snapshots.

    A bare dict stays a prompt-only hint. A ``{"$theater": ...}`` dict is a strict contract:
    a preset, or a custom schema registered under the creator's name for the creator's lifetime.
    A caller with no live participant session (the CLI, a frontend) may only pass a custom schema
    inline: nothing is registered, so no shared name outlives anyone.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise BadRequest(
            "response_format must be a JSON object or null; pass a schema hint object such as "
            "{'type': 'object'}, or a strict form such as {'$theater': 'review'}"
        )
    if CONTRACT_TAG not in raw:
        return json.dumps(raw, sort_keys=True, separators=(",", ":"))
    return _strict(store, raw, _session_of(store, creator_id)).envelope()


def reject_response_format_resume(
    harness_name: Any, resume: Any, response_format: str | None
) -> None:
    if response_format is None or not resume or not isinstance(harness_name, str):
        return
    harness = HARNESSES.get(normalize(harness_name))
    if harness is not None and not harness.resume_takes_prompt:
        raise BadRequest(
            f"harness {harness_name!r} cannot resume a session with response_format; "
            f"resume it without one and use send to deliver the task"
        )


def _session_of(store: Any, creator_id: str | None) -> str | None:
    """The creator when it is a live participant; registered names die with that participant."""
    if creator_id is None:
        return None
    participant = store.get_participant(creator_id)
    return creator_id if participant is not None and participant.status is not Status.DEAD else None


def _strict(store: Any, raw: dict, creator_id: str | None) -> StrictContract:
    kind = raw[CONTRACT_TAG]
    if kind in CONTRACT_PRESET_NAMES:
        if raw.keys() != {CONTRACT_TAG}:
            raise BadRequest(
                f"the {kind!r} preset takes no other keys; use "
                "{'$theater': 'custom', 'name': ..., 'schema': ...} for your own schema"
            )
        return StrictContract(kind, admit_schema(PRESET_SCHEMAS[kind]))
    if kind != CONTRACT_KIND_CUSTOM:
        raise BadRequest(
            f"unknown response contract {kind!r}: choose one of {', '.join(CONTRACT_PRESET_NAMES)} "
            f"or {CONTRACT_KIND_CUSTOM!r}"
        )
    extra = raw.keys() - _CUSTOM_KEYS
    if extra:
        raise BadRequest(
            f"custom response contracts take only name and schema, not {sorted(extra)}"
        )
    name = raw.get("name")
    if not isinstance(name, str) or not CONTRACT_NAME.fullmatch(name):
        raise BadRequest(
            "a custom response contract needs a 'name' of 1-64 letters, digits, '_', '.' or '-'"
        )
    if "schema" not in raw:
        text = None if creator_id is None else store.get_response_contract(creator_id, name)
        if text is None:
            raise BadRequest(
                f"no custom response contract {name!r} is registered by this session; "
                "pass its 'schema' once to register it (names need a participant session)"
            )
        return StrictContract(CONTRACT_KIND_CUSTOM, text, name)
    text = admit_schema(raw["schema"])
    if creator_id is None:
        return StrictContract(CONTRACT_KIND_CUSTOM, text, name)
    outcome = store.register_response_contract(
        creator_id, name, text, created_at=now(), limit=CONTRACT_MAX_CUSTOM_PER_CREATOR
    )
    if outcome == "conflict":
        raise BadRequest(
            f"response contract {name!r} is already registered by {creator_id!r} with a "
            "different schema; names are fixed for the participant's lifetime, so use a new name"
        )
    if outcome == "full":
        raise BadRequest(
            f"{creator_id!r} already holds {CONTRACT_MAX_CUSTOM_PER_CREATOR} custom response "
            "contracts; reuse one by name"
        )
    return StrictContract(CONTRACT_KIND_CUSTOM, text, name)
