"""Public typed async client for participant-scoped MCP-plugin sidecars."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from theater.client import DaemonClient
from theater.constants.plugins import (
    MCP_PLUGIN_CREDENTIAL_MAX_CHARS,
    MCP_PLUGIN_CREDENTIAL_PATH_ENV,
)
from theater.protocol import RemoteError


class TheaterPluginError(Exception):
    """Base error raised by :class:`TheaterPluginClient`."""


class PluginAuthenticationError(TheaterPluginError):
    """The injected credential file is missing, malformed, or revoked."""


class PluginCapabilityError(TheaterPluginError):
    """The attached sidecar does not hold the operation's required grant."""

    def __init__(self, message: str, *, required: str | None, granted: tuple[str, ...]) -> None:
        super().__init__(message)
        self.required = required
        self.granted = granted


class PluginRemoteError(TheaterPluginError):
    """A non-authentication structured error returned by Theater."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details


class TheaterPluginClient:
    """Authenticated sidecar client with no SQLite or tmux access."""

    def __init__(
        self,
        *,
        credential_path: str | Path | None = None,
        client: DaemonClient | None = None,
        autostart: bool = False,
    ) -> None:
        raw_path = credential_path or os.environ.get(MCP_PLUGIN_CREDENTIAL_PATH_ENV)
        if raw_path is None or not str(raw_path):
            raise PluginAuthenticationError(
                f"missing {MCP_PLUGIN_CREDENTIAL_PATH_ENV}; this process was not launched by "
                "Theater"
            )
        self.credential_path = Path(raw_path)
        self._client = client if client is not None else DaemonClient(autostart=autostart)
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> TheaterPluginClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def list_participants(
        self,
        *,
        include_dead: bool = False,
        ids: list[str] | None = None,
        parent_id: str | None = None,
        limit: int | None = None,
        after_id: str | None = None,
    ) -> list[dict[str, Any]]:
        result = await self._call(
            "participants.list",
            include_dead=include_dead,
            ids=ids,
            parent_id=parent_id,
            limit=limit,
            after_id=after_id,
        )
        return _list_result(result, "participants.list")

    async def get_participant(self, participant_id: str) -> dict[str, Any]:
        return _dict_result(
            await self._call("participants.get", id=participant_id),
            "participants.get",
        )

    async def participant_tree(self) -> list[dict[str, Any]]:
        return _list_result(await self._call("participants.tree"), "participants.tree")

    async def recent_dead(self, *, limit: int = 20) -> list[dict[str, Any]]:
        return _list_result(
            await self._call("participants.recent_dead", limit=limit), "participants.recent_dead"
        )

    async def update_participant(
        self,
        *,
        target: str | None = None,
        name: str | None = None,
        description: str | None = None,
    ) -> dict[str, Any]:
        return _dict_result(
            await self._call(
                "participants.update",
                target=target,
                name=name,
                description=description,
            ),
            "participants.update",
        )

    async def catalog(self) -> dict[str, Any]:
        return _dict_result(await self._call("catalog.read"), "catalog.read")

    async def harnesses(self) -> list[dict[str, Any]]:
        return _list_result(await self._call("catalog.harnesses"), "catalog.harnesses")

    async def models(self) -> list[dict[str, Any]]:
        return _list_result(await self._call("catalog.models"), "catalog.models")

    async def get_job(self, handle: str) -> dict[str, Any]:
        return _dict_result(await self._call("jobs.get", handle=handle), "jobs.get")

    async def await_jobs(
        self,
        *,
        handles: Sequence[str],
        max_wait: float | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"handles": list(handles)}
        if max_wait is not None:
            params["max_wait"] = max_wait
        return _list_result(await self._call("jobs.await", **params), "jobs.await")

    async def read_transcript(self, *, target: str, cursor: str | None = None) -> dict[str, Any]:
        return _dict_result(
            await self._call("transcripts.read", id=target, cursor=cursor), "transcripts.read"
        )

    async def recall(
        self,
        *,
        paths: Sequence[str],
        depth: int = 5,
        caller_cwd: str | None = None,
    ) -> dict[str, Any]:
        return _dict_result(
            await self._call("recall", paths=list(paths), depth=depth, caller_cwd=caller_cwd),
            "recall",
        )

    async def read_recall(
        self, *, segment_id: str, caller_cwd: str | None = None
    ) -> dict[str, Any]:
        return _dict_result(
            await self._call("recall.read", segment_id=segment_id, caller_cwd=caller_cwd),
            "recall.read",
        )

    async def list_skills(self) -> dict[str, Any]:
        return _dict_result(await self._call("skills.list"), "skills.list")

    async def load_skill(self, name: str) -> dict[str, Any]:
        return _dict_result(await self._call("skills.load", name=name), "skills.load")

    async def trajectory_snapshot(
        self, *, participant_id: str, before: str | None = None, limit: int | None = None
    ) -> dict[str, Any]:
        return _dict_result(
            await self._call("trajectory.snapshot", id=participant_id, before=before, limit=limit),
            "trajectory.snapshot",
        )

    async def trajectory_follow(
        self,
        *,
        participant_id: str,
        stream_id: str,
        after: str,
        wait: float | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        return _dict_result(
            await self._call(
                "trajectory.follow",
                id=participant_id,
                stream_id=stream_id,
                after=after,
                wait=wait,
                limit=limit,
            ),
            "trajectory.follow",
        )

    async def close_trajectory(
        self, *, participant_id: str, stream_id: str | None = None
    ) -> dict[str, Any]:
        return _dict_result(
            await self._call("trajectory.close", id=participant_id, stream_id=stream_id),
            "trajectory.close",
        )

    async def locate_trajectory(self, *, participant_id: str, record_id: str) -> dict[str, Any]:
        return _dict_result(
            await self._call("trajectory.locate", id=participant_id, record_id=record_id),
            "trajectory.locate",
        )

    async def search_trajectory(
        self, *, participant_id: str, query: str, limit: int | None = None
    ) -> dict[str, Any]:
        return _dict_result(
            await self._call("trajectory.search", id=participant_id, query=query, limit=limit),
            "trajectory.search",
        )

    async def stats(self, *, window: float | None = None) -> dict[str, Any]:
        return _dict_result(await self._call("analytics.stats", window=window), "analytics.stats")

    async def usage_totals(self, *, window: float | None = None) -> dict[str, Any]:
        return _dict_result(
            await self._call("analytics.usage_totals", window=window), "analytics.usage_totals"
        )

    async def usage_summary(
        self, *, window: float | None = None, period: str | None = None
    ) -> dict[str, Any]:
        return _dict_result(
            await self._call("analytics.usage_summary", window=window, period=period),
            "analytics.usage_summary",
        )

    async def usage_by_harness(self, *, detailed: bool = False) -> dict[str, Any]:
        return _dict_result(
            await self._call("analytics.usage_by_harness", detailed=detailed),
            "analytics.usage_by_harness",
        )

    async def bus_tail(self, *, limit: int = 100, after_id: int = 0) -> list[dict[str, Any]]:
        return _list_result(
            await self._call("analytics.bus_tail", limit=limit, after_id=after_id),
            "analytics.bus_tail",
        )

    async def scratchpad_get(
        self, *, namespace: str, keys: list[str] | None = None
    ) -> dict[str, Any]:
        return _dict_result(
            await self._call("scratchpad.get", namespace=namespace, keys=keys), "scratchpad.get"
        )

    async def scratchpad_write(
        self, *, namespace: str, value: str, key: str | None = None
    ) -> dict[str, Any]:
        return _dict_result(
            await self._call("scratchpad.write", namespace=namespace, value=value, key=key),
            "scratchpad.write",
        )

    async def spawn_session(
        self,
        *,
        harness: str,
        approval: str,
        cwd: str,
        prompt: str = "",
        tmux_session: str | None = None,
        window_name: str | None = None,
        background: bool = True,
        worktree: str | bool | None = False,
        base_branch: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        resume: str | None = None,
        name: str | None = None,
        description: str | None = None,
        response_format: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return _dict_result(
            await self._call(
                "sessions.spawn",
                harness=harness,
                approval=approval,
                cwd=cwd,
                prompt=prompt,
                tmux_session=tmux_session,
                window_name=window_name,
                background=background,
                worktree=worktree,
                base_branch=base_branch,
                model=model,
                reasoning_effort=reasoning_effort,
                resume=resume,
                name=name,
                description=description,
                response_format=dict(response_format) if response_format is not None else None,
            ),
            "sessions.spawn",
        )

    async def send_session(
        self,
        *,
        target: str,
        prompt: str,
        response_format: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return _dict_result(
            await self._call(
                "sessions.send",
                target=target,
                prompt=prompt,
                response_format=dict(response_format) if response_format is not None else None,
            ),
            "sessions.send",
        )

    async def interrupt_session(self, *, target: str) -> dict[str, Any]:
        return _dict_result(
            await self._call("sessions.interrupt", target=target), "sessions.interrupt"
        )

    async def kill_session(self, *, target: str) -> dict[str, Any]:
        return _dict_result(await self._call("sessions.kill", id=target), "sessions.kill")

    async def call(self, operation: str, params: Mapping[str, Any] | None = None) -> Any:
        """Call one documented plugin operation for compatibility wrappers.

        ``operation`` is still checked by the daemon's fixed dispatcher; this
        method cannot select arbitrary daemon RPC methods.
        """
        if not isinstance(operation, str) or not operation:
            raise PluginRemoteError("bad_request", "plugin operation must be a non-empty string")
        if params is not None and not isinstance(params, Mapping):
            raise PluginRemoteError("bad_request", "plugin operation params must be an object")
        return await self._call(operation, **dict(params or {}))

    async def _call(self, operation: str, **params: Any) -> Any:
        credential = _read_credential(self.credential_path)
        try:
            return await self._client.call(
                "plugin.call",
                credential=credential,
                operation=operation,
                params={key: value for key, value in params.items() if value is not None},
            )
        except RemoteError as exc:
            raise _map_remote_error(exc) from exc


def _read_credential(path: Path) -> str:
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as exc:
        raise PluginAuthenticationError("cannot open injected plugin credential file") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MCP_PLUGIN_CREDENTIAL_MAX_CHARS + 1:
            raise PluginAuthenticationError("injected plugin credential file is invalid")
        raw = os.read(fd, MCP_PLUGIN_CREDENTIAL_MAX_CHARS + 1)
    except OSError as exc:
        raise PluginAuthenticationError("cannot read injected plugin credential file") from exc
    finally:
        os.close(fd)
    try:
        credential = raw.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise PluginAuthenticationError("injected plugin credential file is not UTF-8") from exc
    if not credential or len(credential) > MCP_PLUGIN_CREDENTIAL_MAX_CHARS:
        raise PluginAuthenticationError("injected plugin credential file is malformed")
    return credential


def _map_remote_error(exc: RemoteError) -> TheaterPluginError:
    if exc.code == "plugin_auth_failed":
        return PluginAuthenticationError(exc.message)
    if exc.code == "capability_denied":
        details = exc.details or {}
        required = details.get("required") if isinstance(details.get("required"), str) else None
        raw_granted = details.get("granted")
        granted = (
            tuple(item for item in raw_granted if isinstance(item, str))
            if isinstance(raw_granted, list)
            else ()
        )
        return PluginCapabilityError(exc.message, required=required, granted=granted)
    return PluginRemoteError(exc.code, exc.message, exc.details)


def _dict_result(value: Any, operation: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PluginRemoteError("invalid_result", f"{operation} returned a non-object result")
    return value


def _list_result(value: Any, operation: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise PluginRemoteError("invalid_result", f"{operation} returned a non-list result")
    return value


__all__ = [
    "PluginAuthenticationError",
    "PluginCapabilityError",
    "PluginRemoteError",
    "TheaterPluginClient",
    "TheaterPluginError",
]
