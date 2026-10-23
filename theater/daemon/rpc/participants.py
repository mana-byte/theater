"""Private participant RPC adapters and retained compatibility names."""

from __future__ import annotations

from theater.daemon.participants import operations, resume, termination
from theater.daemon.rpc.router import method

_resume_state = resume.resume_state
_authorize_termination = termination._authorize_termination
_stop_verified_detached_backend = termination._stop_verified_detached_backend
terminate_participant = termination.terminate_participant


@method("hello")
async def _hello(daemon, params: dict) -> dict:
    return await operations.hello(daemon, params)


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
    return await operations.rename(daemon, params)


@method("participant.update")
async def _update(daemon, params: dict) -> dict:
    return await operations.update(daemon, params)


@method("participant.status")
async def _status(daemon, params: dict) -> dict:
    return await operations.set_status(daemon, params)


@method("participant.kill")
async def _kill(daemon, params: dict) -> dict:
    return await operations.kill(daemon, params)


@method("adopt")
async def _adopt(daemon, params: dict) -> dict:
    return await operations.reject_legacy_adopt(daemon, params)


@method("participants.unmanaged")
async def _unmanaged(daemon, params: dict) -> list[dict]:
    return await operations.list_legacy_unmanaged(daemon, params)
