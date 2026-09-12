"""Scratchpad RPC handlers: repo-scoped get, write, and delete."""

from __future__ import annotations

from theater.constants.daemon import (
    SCRATCHPAD_MAX_KEYS_PER_GET,
    SCRATCHPAD_MAX_NAME_LENGTH,
    SCRATCHPAD_READ_BUDGET_BYTES,
)
from theater.daemon import lineage, workers
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
    namespace = _bounded_name(
        _string_param(params, "namespace", method_name="scratchpad.get"),
        "namespace",
        method_name="scratchpad.get",
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
    if page.oversized_key is not None:
        raise BadRequest(
            f"scratchpad entry {page.oversized_key!r} encodes to {page.oversized_bytes} "
            f"wire bytes, beyond the {SCRATCHPAD_READ_BUDGET_BYTES}-byte read budget; it "
            "predates the value bound — delete it with scratchpad.delete to read past it"
        )
    return {
        "namespace": namespace,
        "entries": page.entries,
        "keys": list(page.keys),
        "truncated": page.truncated,
        "after_key": page.after_key,
    }


@method("scratchpad.delete")
async def _scratchpad_delete(daemon, params: dict) -> dict:
    caller = _caller_participant(daemon, params, method_name="scratchpad.delete")
    namespace = _bounded_name(
        _string_param(params, "namespace", method_name="scratchpad.delete"),
        "namespace",
        method_name="scratchpad.delete",
    )
    keys_raw = params.get("keys")
    if not isinstance(keys_raw, list) or not all(isinstance(k, str) for k in keys_raw):
        raise BadRequest("scratchpad.delete parameter 'keys' must be a list of strings")
    if len(keys_raw) > SCRATCHPAD_MAX_KEYS_PER_GET:
        raise BadRequest(
            f"scratchpad.delete names {len(keys_raw)} keys; one request is bounded to "
            f"{SCRATCHPAD_MAX_KEYS_PER_GET} — delete in batches instead"
        )
    for named in keys_raw:
        if named == "":
            raise BadRequest("scratchpad.delete parameter 'keys' must contain non-empty strings")
    # Key length deliberately unchecked: pre-bound legacy entries must
    # stay deletable, or they could never be cleaned up.
    deleted = daemon.store.scratchpad_delete(
        tree_root_id=lineage.root_of(daemon.store, caller.id),
        repo_root=await _repo_scope_for_store(caller),
        namespace=namespace,
        keys=keys_raw,
    )
    return {"namespace": namespace, "deleted": deleted}
