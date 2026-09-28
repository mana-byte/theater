"""OpenCode 2.x routes on the shared loopback transport: `/api/...`, success bodies in `{data}`.

Routes and shapes follow packages/protocol/src/groups/session.ts and server.ts at 2.0.18.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import quote

from .http import OpenCodeClient, OpenCodeHttpError, _safe_session_id


class OpenCodeV2Client:
    """2.x routes only; the 1.x client is the bounded, credentialed transport underneath."""

    def __init__(self, *, endpoint: str, token_file: Path) -> None:
        self._transport = OpenCodeClient(endpoint=endpoint, token_file=token_file)

    async def version(self) -> str:
        result = await self._transport._json_request("GET", "/api/info")
        if not isinstance(result, Mapping) or not isinstance(result.get("version"), str):
            raise OpenCodeHttpError("GET", "/api/info", "info carries no version", written=True)
        return result["version"]

    async def create_session(
        self,
        *,
        directory: str | None,
        model: Mapping[str, str] | None = None,
        session_id: str | None = None,
        permissions: Sequence[Mapping[str, str]] | None = None,
    ) -> str:
        """Without a directory the server's own cwd, the participant's, is the location."""
        body: dict[str, object] = {}
        if session_id is not None:
            body["id"] = session_id
        if directory is not None:
            body["location"] = {"directory": directory}
        if model is not None:
            body["model"] = dict(model)
        if permissions is not None:
            body["permissions"] = [dict(rule) for rule in permissions]
        found = await self._data("POST", "/api/session", body=body)
        return _session_id(found, "POST", "/api/session")

    async def read_session(self, session_id: str) -> Mapping[str, object]:
        sid = _safe_session_id(session_id)
        return await self._data("GET", f"/api/session/{sid}", session_id=sid)

    async def update_session_permissions(
        self, session_id: str, rules: Sequence[Mapping[str, str]]
    ) -> None:
        """Replace the session ruleset; the stock server answers 204 and stores it verbatim."""
        sid = _safe_session_id(session_id)
        await self._transport._json_request(
            "PATCH",
            f"/api/session/{sid}",
            body={"permissions": [dict(rule) for rule in rules]},
            session_id=sid,
        )

    async def list_plugins(self, *, directory: str | None = None) -> tuple[Mapping[str, object], ...]:
        """`GET /api/plugin` (deepObject location query); empty until the location has booted."""
        path = "/api/plugin"
        if directory is not None:
            path += f"?location%5Bdirectory%5D={quote(directory, safe='')}"
        result = await self._transport._json_request("GET", path)
        data = result.get("data") if isinstance(result, Mapping) else None
        if not isinstance(data, list):
            raise OpenCodeHttpError("GET", "/api/plugin", "response carries no plugin list")
        return tuple(entry for entry in data if isinstance(entry, Mapping))

    async def evaluate_permission(
        self,
        session_id: str,
        *,
        action: str,
        resources: Sequence[str],
        save: Sequence[str] | None = None,
    ) -> Mapping[str, object]:
        """Evaluate one permission decision; an `ask` effect leaves a pending request."""
        sid = _safe_session_id(session_id)
        body: dict[str, object] = {"action": action, "resources": list(resources)}
        if save is not None:
            body["save"] = list(save)
        return await self._data(
            "POST", f"/api/session/{sid}/permission", body=body, session_id=sid
        )

    async def reply_permission(
        self, session_id: str, request_id: str, *, decision: str
    ) -> None:
        sid = _safe_session_id(session_id)
        rid = _safe_session_id(request_id)
        await self._transport._json_request(
            "POST",
            f"/api/session/{sid}/permission/{rid}/reply",
            body={"decision": decision},
            session_id=sid,
        )

    async def saved_permissions(self, *, project_id: str) -> tuple[Mapping[str, object], ...]:
        result = await self._transport._json_request(
            "GET", f"/api/permission/saved?projectID={quote(project_id, safe='')}"
        )
        data = result.get("data") if isinstance(result, Mapping) else None
        if not isinstance(data, list):
            raise OpenCodeHttpError(
                "GET", "/api/permission/saved", "response carries no saved list"
            )
        return tuple(entry for entry in data if isinstance(entry, Mapping))

    async def delete_saved_permission(self, saved_id: str) -> None:
        await self._transport._json_request(
            "DELETE", f"/api/permission/saved/{_safe_session_id(saved_id)}"
        )

    async def prompt(
        self, session_id: str, *, message_id: str, text: str, delivery: str
    ) -> Mapping[str, object]:
        """Durable admission: a 200 carries the admitted inbox item, whose id is the message."""
        sid = _safe_session_id(session_id)
        body = {"id": message_id, "text": text, "delivery": delivery}
        return await self._data("POST", f"/api/session/{sid}/prompt", body=body, session_id=sid)

    async def active(self) -> Mapping[str, object]:
        """Sessions this server is running now; absence proves idle (session.active)."""
        return await self._data("GET", "/api/session/active")

    async def _data(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, object] | None = None,
        session_id: str | None = None,
    ) -> Mapping[str, object]:
        result = await self._transport._json_request(method, path, body=body, session_id=session_id)
        data = result.get("data") if isinstance(result, Mapping) else None
        if not isinstance(data, Mapping):
            raise OpenCodeHttpError(
                method, path, "response carries no data object", written=True, session_id=session_id
            )
        return data


def _session_id(found: Mapping[str, object], method: str, path: str) -> str:
    value = found.get("id")
    if not isinstance(value, str) or not value.startswith("ses"):
        raise OpenCodeHttpError(
            method, path, "session response carries no session id", written=True
        )
    return value


def model_ref(model: str) -> dict[str, str] | None:
    """`provider/model[#variant]`, as the TUI and config spell it, as a 2.x `Model.Ref`."""
    reference, _, variant = model.partition("#")
    provider, _, model_id = reference.partition("/")
    if not provider or not model_id:
        return None
    ref = {"providerID": provider, "id": model_id}
    if variant:
        ref["variant"] = variant
    return ref


__all__ = ["OpenCodeV2Client", "model_ref"]
