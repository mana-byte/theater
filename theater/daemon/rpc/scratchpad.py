"""Scratchpad RPC handlers: repo-scoped get and write."""

from __future__ import annotations

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


@method("scratchpad.write")
async def _scratchpad_write(daemon, params: dict) -> dict:
    caller = _caller_participant(daemon, params, method_name="scratchpad.write")
    namespace = _string_param(params, "namespace", method_name="scratchpad.write")
    value = _string_param(params, "value", method_name="scratchpad.write", allow_empty=True)
    key = _optional_string_param(params, "key", method_name="scratchpad.write")
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
    namespace = _string_param(params, "namespace", method_name="scratchpad.get")
    keys_raw = params.get("keys")
    if keys_raw is None:
        keys: list[str] | None = None
    elif isinstance(keys_raw, list) and all(isinstance(k, str) for k in keys_raw):
        keys = keys_raw
    else:
        raise BadRequest("scratchpad.get parameter 'keys' must be a list of strings or null")
    entries = daemon.store.scratchpad_get(
        tree_root_id=lineage.root_of(daemon.store, caller.id),
        repo_root=await _repo_scope_for_store(caller),
        namespace=namespace,
        keys=keys,
    )
    return {"namespace": namespace, "entries": entries}
