"""Bounded, content-free pending input evidence for OpenCode display status."""

from __future__ import annotations

from collections.abc import Mapping

_MAX_PENDING = 128
_MAX_SESSIONS = 128
_MAX_ID_CHARS = 512
_KINDS = ("permission", "question")
_EVENTS = frozenset(
    (
        "permission.asked",
        "permission.replied",
        "question.asked",
        "question.replied",
        "question.rejected",
    )
)


def snapshot_awaiting(params: Mapping[str, object]) -> bool:
    return any(type(count := params.get(f"{kind}_count")) is int and count > 0 for kind in _KINDS)


def is_input_event(value: object) -> bool:
    return isinstance(value, str) and value in _EVENTS


def _identifier(value: object) -> str | None:
    return value if isinstance(value, str) and 0 < len(value) <= _MAX_ID_CHARS else None


class PendingInputs:
    """Track request IDs, never questions, answers, paths, or approval payloads."""

    def __init__(self) -> None:
        self.revision = 0
        self._session_id: str | None = None
        self._sessions: set[str] = set()
        self._requests: set[tuple[str, str, str]] = set()
        self._overflow: set[str] = set()

    @property
    def awaiting(self) -> bool:
        return bool(self._requests or self._overflow)

    def reset(self, session_id: str | None) -> None:
        self.revision += 1
        self._session_id = session_id
        self._sessions = {session_id} if session_id is not None else set()
        self._requests.clear()
        self._overflow.clear()

    def accepts(self, session_id: object) -> bool:
        return isinstance(session_id, str) and session_id in self._sessions

    def clear_session(self, session_id: str) -> bool:
        before = self.awaiting
        self.revision += 1
        self._requests = {key for key in self._requests if key[0] != session_id}
        self._overflow.discard(session_id)
        return self.awaiting != before

    def feed(self, event_type: str | None, properties: Mapping[str, object]) -> bool:
        if not is_input_event(event_type):
            return False
        assert event_type is not None
        kind, _, action = event_type.partition(".")
        request_id = _identifier(properties.get("id" if action == "asked" else "requestID"))
        session_id = _identifier(properties.get("sessionID"))
        if request_id is None or session_id is None:
            return False
        before = self.awaiting
        self.revision += 1
        if not self.accepts(session_id):
            return False
        key = (session_id, kind, request_id)
        if action == "asked":
            if len(self._requests) < _MAX_PENDING or key in self._requests:
                self._requests.add(key)
            else:
                # Overflow stays visible until an authoritative snapshot or idle clears it.
                self._overflow.add(session_id)
        else:
            self._requests.discard(key)
        return self.awaiting != before

    def reconcile(self, snapshot: Mapping[str, object], *, session_id: str, revision: int) -> bool:
        """A readback cannot overwrite input events accepted while it was in flight."""
        if revision != self.revision or session_id != self._session_id:
            return False
        sessions = self._snapshot_sessions(snapshot, session_id)
        if sessions is None:
            return False
        requests: set[tuple[str, str, str]] = set()
        overflow: set[str] = set()
        for kind in _KINDS:
            rows = snapshot.get(kind)
            if not isinstance(rows, list):
                return False
            for row in rows:
                if not isinstance(row, Mapping):
                    return False
                row_session = _identifier(row.get("sessionID"))
                if row_session is None:
                    return False
                if row_session not in sessions:
                    continue
                request_id = _identifier(row.get("id"))
                if request_id is None:
                    return False
                key = (row_session, kind, request_id)
                if len(requests) < _MAX_PENDING or key in requests:
                    requests.add(key)
                else:
                    overflow.add(row_session)
        before = self.awaiting
        self._sessions = sessions
        self._requests = requests
        self._overflow = overflow
        self.revision += 1
        return self.awaiting != before

    @staticmethod
    def _snapshot_sessions(snapshot: Mapping[str, object], session_id: str) -> set[str] | None:
        children = snapshot.get("children")
        if not isinstance(children, list):
            return None
        sessions = {session_id}
        for child in children:
            if not isinstance(child, Mapping):
                return None
            child_id = _identifier(child.get("id"))
            if child_id is None:
                return None
            if child.get("parentID") == session_id:
                sessions.add(child_id)
                if len(sessions) > _MAX_SESSIONS:
                    return None
        return sessions
