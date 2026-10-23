"""Private participant RPC adapters and retained compatibility names."""

from __future__ import annotations

from theater.daemon.participants import operations
from theater.daemon.rpc.router import method

_resume_state = operations._resume_state
authorize_participant_mutation = operations.authorize_participant_mutation
update_participant_metadata = operations.update_participant_metadata
update_participant_status = operations.update_participant_status
persist_participant_status = operations.persist_participant_status
_require_verified_backend_stop = operations._require_verified_backend_stop
_stop_verified_detached_backend = operations._stop_verified_detached_backend
_TerminationUncertain = operations._TerminationUncertain
_terminate_provider_terminal = operations._terminate_provider_terminal
_authorize_termination = operations._authorize_termination
_require_absent_or_forget_hint = operations._require_absent_or_forget_hint
terminate_participant = operations.terminate_participant
_with_presence = operations._with_presence
_tree_with_presence = operations._tree_with_presence
_pagination = operations._pagination
_current_git = operations._current_git


@method("hello")
async def _hello(daemon, params: dict) -> dict:
    return await operations._hello(daemon, params)


@method("participants.list")
async def _list(daemon, params: dict) -> list[dict]:
    return await operations.list_participants(daemon, params)


@method("participants.recent_dead")
async def _recent_dead(daemon, params: dict) -> list[dict]:
    return await operations.recent_dead(daemon, params)


@method("participants.tree")
async def _tree(daemon, params: dict) -> list[dict]:
    return await operations.tree(daemon, params)


@method("participants.get")
async def _get(daemon, params: dict) -> dict:
    return await operations.get_participant(daemon, params)


@method("participant.rename")
async def _rename(daemon, params: dict) -> dict:
    return await operations._rename(daemon, params)


@method("participant.update")
async def _update(daemon, params: dict) -> dict:
    return await operations.update(daemon, params)


@method("participant.status")
async def _status(daemon, params: dict) -> dict:
    return await operations._status(daemon, params)


@method("participant.kill")
async def _kill(daemon, params: dict) -> dict:
    return await operations.kill(daemon, params)


@method("adopt")
async def _adopt(daemon, params: dict) -> dict:
    return await operations._adopt(daemon, params)


@method("participants.unmanaged")
async def _unmanaged(daemon, params: dict) -> list[dict]:
    return await operations._unmanaged(daemon, params)
