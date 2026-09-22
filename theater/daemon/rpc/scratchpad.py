"""Private RPC adapters for the shared machine-wide scratchpad service."""

from __future__ import annotations

import hashlib

from theater.constants.daemon import (
    SCRATCHPAD_MAX_KEYS_PER_GET,
    SCRATCHPAD_MAX_NAME_LENGTH,
    SCRATCHPAD_MAX_VALUE_BYTES,
    SCRATCHPAD_READ_BUDGET_BYTES,
)
from theater.daemon.persistence.repositories.scratchpad import (
    _WIRE_WRAPPER_BYTES,
    _wire_bytes,
)
from theater.daemon.rpc.params import _optional_string_param, _string_param
from theater.daemon.rpc.router import method
from theater.daemon.scratchpad import service_for_daemon
from theater.models import BadRequest


def _bounded_name(value: str, label: str, *, method_name: str) -> str:
    """A namespace, provided key, or cursor within its length bound."""
    if len(value) > SCRATCHPAD_MAX_NAME_LENGTH:
        raise BadRequest(
            f"{method_name} parameter {label!r} is {len(value)} characters; scratchpad "
            f"names are bounded to {SCRATCHPAD_MAX_NAME_LENGTH}"
        )
    return value


def _actor_participant_id(daemon, params: dict, *, method_name: str) -> str | None:
    """Use a valid optional private caller only as write audit metadata."""
    caller_id = _optional_string_param(params, "caller_id", method_name=method_name)
    if not caller_id:
        return None
    caller = daemon.store.get_participant(caller_id)
    return caller.id if caller is not None else None


@method("scratchpad.write")
async def _scratchpad_write(daemon, params: dict) -> dict:
    namespace = _bounded_name(
        _string_param(params, "namespace", method_name="scratchpad.write"),
        "namespace",
        method_name="scratchpad.write",
    )
    value = _string_param(params, "value", method_name="scratchpad.write", allow_empty=True)
    key = _optional_string_param(params, "key", method_name="scratchpad.write")
    if key is not None:
        if key == "":
            raise BadRequest(
                "scratchpad.write parameter 'key' must be a non-empty string when provided"
            )
        _bounded_name(key, "key", method_name="scratchpad.write")
    minted = service_for_daemon(daemon).write(
        namespace=namespace,
        value=value,
        key=key,
        actor_participant_id=_actor_participant_id(daemon, params, method_name="scratchpad.write"),
    )
    return {"namespace": namespace, "key": minted}


@method("scratchpad.get")
async def _scratchpad_get(daemon, params: dict) -> dict:
    # Legacy overlong namespaces remain readable so they can be cleaned up.
    namespace = _string_param(params, "namespace", method_name="scratchpad.get")
    namespace_wire = _wire_bytes(namespace)
    if namespace_wire + _WIRE_WRAPPER_BYTES > SCRATCHPAD_READ_BUDGET_BYTES:
        raise BadRequest(
            f"scratchpad namespace encodes to {namespace_wire} wire bytes, and with "
            f"the fixed response overhead cannot fit the {SCRATCHPAD_READ_BUDGET_BYTES}-byte "
            "read budget; it predates the name bound — clear it with "
            "scratchpad.delete to clean it up"
        )
    keys_raw = params.get("keys")
    if keys_raw is None:
        keys: list[str] | None = None
    elif isinstance(keys_raw, list) and all(isinstance(key, str) for key in keys_raw):
        if len(keys_raw) > SCRATCHPAD_MAX_KEYS_PER_GET:
            raise BadRequest(
                f"scratchpad.get names {len(keys_raw)} keys; one request is bounded to "
                f"{SCRATCHPAD_MAX_KEYS_PER_GET} — read the namespace in pages instead"
            )
        for requested in keys_raw:
            if requested == "":
                raise BadRequest("scratchpad.get parameter 'keys' must contain non-empty strings")
            _bounded_name(requested, "keys", method_name="scratchpad.get")
        keys = keys_raw
    else:
        raise BadRequest("scratchpad.get parameter 'keys' must be a list of strings or null")
    after_key = _optional_string_param(params, "after_key", method_name="scratchpad.get")
    if after_key == "":
        raise BadRequest(
            "scratchpad.get parameter 'after_key' must be a non-empty string when provided"
        )
    page = service_for_daemon(daemon).get(
        namespace=namespace,
        keys=keys,
        after_key=after_key,
    )
    response = {
        "namespace": namespace,
        "entries": page.entries,
        "keys": list(page.keys),
        "truncated": page.truncated,
        "after_key": page.after_key,
    }
    if page.oversized_bytes:
        response["oversized_key"] = page.oversized_key
        response["oversized_digest"] = page.oversized_digest
        response["oversized_bytes"] = page.oversized_bytes
    return response


def _digests_param(params: dict) -> list[str] | None:
    """The 64-hex deletion digests a refusal page issued, or None."""
    digests_raw = params.get("digests")
    if digests_raw is None:
        return None
    if not isinstance(digests_raw, list) or not all(
        isinstance(digest, str) for digest in digests_raw
    ):
        raise BadRequest("scratchpad.delete parameter 'digests' must be a list of strings or null")
    if len(digests_raw) > SCRATCHPAD_MAX_KEYS_PER_GET:
        raise BadRequest(
            f"scratchpad.delete names {len(digests_raw)} digests; one request is "
            f"bounded to {SCRATCHPAD_MAX_KEYS_PER_GET} — delete in batches instead"
        )
    for digest in digests_raw:
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise BadRequest(
                "scratchpad.delete parameter 'digests' must contain 64-character "
                "lowercase hex digests"
            )
    return digests_raw


def _echo_safe_deleted(deleted: list[str], start: int) -> tuple[list[str], list[str]]:
    """Split deleted keys into echo-safe names and stable cleanup digests."""
    names: list[str] = []
    digests: list[str] = []
    used = start
    for key in deleted:
        key_wire = _wire_bytes(key)
        if (
            key_wire <= SCRATCHPAD_MAX_VALUE_BYTES
            and used + key_wire <= SCRATCHPAD_READ_BUDGET_BYTES
        ):
            names.append(key)
            used += key_wire
        else:
            digests.append(hashlib.sha256(key.encode("utf-8")).hexdigest())
    return names, digests


@method("scratchpad.delete")
async def _scratchpad_delete(daemon, params: dict) -> dict:
    # Legacy names are intentionally addressable by deletion even beyond current bounds.
    namespace = _string_param(params, "namespace", method_name="scratchpad.delete")
    keys_raw = params.get("keys")
    if keys_raw is None:
        keys_raw = []
    if not isinstance(keys_raw, list) or not all(isinstance(key, str) for key in keys_raw):
        raise BadRequest("scratchpad.delete parameter 'keys' must be a list of strings")
    for named in keys_raw:
        if named == "":
            raise BadRequest("scratchpad.delete parameter 'keys' must contain non-empty strings")
    if len(keys_raw) > SCRATCHPAD_MAX_KEYS_PER_GET:
        raise BadRequest(
            f"scratchpad.delete names {len(keys_raw)} keys; one request is bounded to "
            f"{SCRATCHPAD_MAX_KEYS_PER_GET} — delete in batches instead"
        )
    digests = _digests_param(params)
    clear = params.get("clear", False)
    if clear is None:
        clear = False
    if not isinstance(clear, bool):
        raise BadRequest("scratchpad.delete parameter 'clear' must be a boolean")
    if clear and (keys_raw or digests):
        raise BadRequest(
            "scratchpad.delete 'clear' empties the namespace; name no keys or digests with it"
        )
    if not clear and not keys_raw and not digests:
        raise BadRequest("scratchpad.delete needs at least one key or digest")
    echoed = (
        namespace
        if _wire_bytes(namespace) + _WIRE_WRAPPER_BYTES + 256 * 72 <= SCRATCHPAD_READ_BUDGET_BYTES
        else None
    )
    service = service_for_daemon(daemon)
    if clear:
        return {"namespace": echoed, "deleted_count": service.clear(namespace=namespace)}
    deleted = service.delete(namespace=namespace, keys=keys_raw, digests=digests)
    names, oversized = _echo_safe_deleted(
        deleted, _wire_bytes(echoed or "") + _WIRE_WRAPPER_BYTES + 256 * 72
    )
    response: dict = {"namespace": echoed, "deleted": names}
    if oversized:
        response["deleted_oversized"] = oversized
    return response
