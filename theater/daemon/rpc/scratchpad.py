"""Scratchpad RPC handlers: repo-scoped get, write, and delete."""

from __future__ import annotations

import hashlib

from theater.constants.daemon import (
    SCRATCHPAD_MAX_KEYS_PER_GET,
    SCRATCHPAD_MAX_NAME_LENGTH,
    SCRATCHPAD_MAX_VALUE_BYTES,
    SCRATCHPAD_READ_BUDGET_BYTES,
)
from theater.daemon import lineage, workers
from theater.daemon.persistence.repositories.scratchpad import (
    _WIRE_WRAPPER_BYTES,
    _wire_bytes,
)
from theater.daemon.rpc.params import (
    _optional_string_param,
    _string_param,
)
from theater.daemon.rpc.router import method
from theater.daemon.worktrees import main_repo_root
from theater.models import BadRequest


def _caller_participant(daemon, params: dict, *, method_name: str):
    caller_id = _string_param(params, "caller_id", method_name=method_name)
    caller = daemon.store.get_participant(caller_id)
    if caller is None:
        raise BadRequest(f"{method_name} requires caller_id to name an existing participant")
    return caller


async def _repo_scope_for_store(caller) -> str:
    if not caller.cwd:
        raise BadRequest("scratchpad cannot be used outside a git repository: caller has no cwd")
    repo_root = await workers.to_thread(
        main_repo_root,
        caller.cwd,
        child_id=caller.id,
        label="store.repo_root",
    )
    if repo_root is None:
        raise BadRequest(
            "scratchpad cannot be used outside a git repository: caller cwd is not in a git repo"
        )
    return repo_root


def _bounded_name(value: str, label: str, *, method_name: str) -> str:
    """A namespace, provided key, or cursor within its length bound."""
    if len(value) > SCRATCHPAD_MAX_NAME_LENGTH:
        raise BadRequest(
            f"{method_name} parameter {label!r} is {len(value)} characters; scratchpad "
            f"names are bounded to {SCRATCHPAD_MAX_NAME_LENGTH}"
        )
    return value


@method("scratchpad.write")
async def _scratchpad_write(daemon, params: dict) -> dict:
    caller = _caller_participant(daemon, params, method_name="scratchpad.write")
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
    minted = daemon.store.scratchpad_write(
        tree_root_id=lineage.root_of(daemon.store, caller.id),
        repo_root=await _repo_scope_for_store(caller),
        namespace=namespace,
        value=value,
        updated_by=caller.id,
        key=key,
    )
    return {"namespace": namespace, "key": minted}


@method("scratchpad.get")
async def _scratchpad_get(daemon, params: dict) -> dict:
    caller = _caller_participant(daemon, params, method_name="scratchpad.get")
    # Namespace length is deliberately unchecked here: entries written
    # under a pre-bound legacy namespace must stay readable and deletable.
    namespace = _string_param(params, "namespace", method_name="scratchpad.get")
    # The response echoes the namespace, so a namespace whose wire bytes
    # alone exceed the read budget can never fit: refuse before reading.
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
    elif isinstance(keys_raw, list) and all(isinstance(k, str) for k in keys_raw):
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
    # Length deliberately unchecked: a page cursor must be able to name
    # any stored key, including ones written before the name bound.
    page = daemon.store.scratchpad_get(
        tree_root_id=lineage.root_of(daemon.store, caller.id),
        repo_root=await _repo_scope_for_store(caller),
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
        # The refused entry is named so the caller can delete it; the
        # digest always names it, a key too large to echo is by size.
        response["oversized_key"] = page.oversized_key
        response["oversized_digest"] = page.oversized_digest
        response["oversized_bytes"] = page.oversized_bytes
    return response


def _digests_param(params: dict) -> list[str] | None:
    """The 64-hex deletion digests a refusal page issued, or None."""
    digests_raw = params.get("digests")
    if digests_raw is None:
        return None
    if not isinstance(digests_raw, list) or not all(isinstance(d, str) for d in digests_raw):
        raise BadRequest("scratchpad.delete parameter 'digests' must be a list of strings or null")
    if len(digests_raw) > SCRATCHPAD_MAX_KEYS_PER_GET:
        raise BadRequest(
            f"scratchpad.delete names {len(digests_raw)} digests; one request is "
            f"bounded to {SCRATCHPAD_MAX_KEYS_PER_GET} — delete in batches instead"
        )
    for digest in digests_raw:
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise BadRequest(
                "scratchpad.delete parameter 'digests' must contain 64-character "
                "lowercase hex digests"
            )
    return digests_raw


def _echo_safe_deleted(deleted: list[str], start: int) -> tuple[list[str], list[str]]:
    """Split deleted keys into names that fit the echo budget and digests;
    `start` is the committed response cost — echo, wrapper, and a digest
    reserve — so names, digests, and echo together stay in budget.
    """
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
    caller = _caller_participant(daemon, params, method_name="scratchpad.delete")
    # Namespace length is deliberately unchecked: a legacy overlong
    # namespace must stay deletable, or it could never be cleaned up.
    namespace = _string_param(params, "namespace", method_name="scratchpad.delete")
    keys_raw = params.get("keys")
    if keys_raw is None:
        keys_raw = []
    if not isinstance(keys_raw, list) or not all(isinstance(k, str) for k in keys_raw):
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
    tree_root_id = lineage.root_of(daemon.store, caller.id)
    repo_root = await _repo_scope_for_store(caller)
    # A namespace whose echo cannot fit the read budget is confirmed by
    # a null echo, not by a response larger than what it names.
    echoed = (
        namespace
        if _wire_bytes(namespace) + _WIRE_WRAPPER_BYTES + 256 * 72 <= SCRATCHPAD_READ_BUDGET_BYTES
        else None
    )
    if clear:
        count = daemon.store.scratchpad_clear(
            tree_root_id=tree_root_id, repo_root=repo_root, namespace=namespace
        )
        return {"namespace": echoed, "deleted_count": count}
    # Key length deliberately unchecked: pre-bound legacy entries must
    # stay deletable, or they could never be cleaned up.
    deleted = daemon.store.scratchpad_delete(
        tree_root_id=tree_root_id,
        repo_root=repo_root,
        namespace=namespace,
        keys=keys_raw,
        digests=digests,
    )
    names, oversized = _echo_safe_deleted(
        deleted, _wire_bytes(echoed or "") + _WIRE_WRAPPER_BYTES + 256 * 72
    )
    response: dict = {"namespace": echoed, "deleted": names}
    if oversized:
        response["deleted_oversized"] = oversized
    return response
