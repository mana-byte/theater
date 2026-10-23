"""Raw and rich Codex tool identity correlation for one rollout parser."""

from __future__ import annotations

from dataclasses import dataclass

from theater.constants.trajectory import TRAJECTORY_MCP_CALL_CONTEXT_LIMIT

from .constants import CODEX_RAW_TOOL_CALL_TYPES, CODEX_RAW_TOOL_RESULT_TYPES
from .values import _codex_revision, _codex_scoped_id, _trajectory_id


@dataclass(frozen=True)
class RecordLocation:
    native_id: str
    revision: int


@dataclass(frozen=True)
class RichToolIdentity:
    call_native: str | None
    result_native: str | None
    provides_control: bool
    call_revision: int | None
    result_revision: int | None


@dataclass(frozen=True)
class RawResultDecision:
    emit: bool
    native_id: str | None


class ToolCorrelation:
    """Keep bounded raw locations and rich adoption decisions in stream order."""

    def __init__(self) -> None:
        self._raw_calls: dict[str, RecordLocation] = {}
        self._raw_results: dict[str, RecordLocation] = {}
        self._rich_items: dict[str, RichToolIdentity] = {}

    def clear(self) -> None:
        self._raw_calls.clear()
        self._raw_results.clear()
        self._rich_items.clear()

    def rich_covers_call(self, call_id: str | None) -> bool:
        rich = self._rich_items.get(call_id) if call_id is not None else None
        return rich is not None and rich.provides_control

    def raw_call_precedes(self, item_id: str | None) -> bool:
        return item_id is not None and item_id in self._raw_calls

    def raw_result_decision(self, call_id: str | None) -> RawResultDecision:
        rich = self._rich_items.get(call_id) if call_id is not None else None
        if rich is None:
            return RawResultDecision(True, None)
        return RawResultDecision(not rich.provides_control, rich.result_native)

    def rich_identity(self, item: dict, item_id: str | None) -> RichToolIdentity:
        stored = self._rich_items.get(item_id) if item_id is not None else None
        if stored is not None:
            return stored
        return self._adopt(item, item_id)

    def _adopt(self, item: dict, item_id: str | None) -> RichToolIdentity:
        if item_id is None:
            return RichToolIdentity(None, None, True, None, None)
        raw_call = self._raw_calls.get(item_id)
        if raw_call is None:
            call_native = (
                item_id if item.get("type") == "McpToolCall" else _codex_scoped_id(item_id, "call")
            )
            return RichToolIdentity(
                call_native, _codex_scoped_id(item_id, "result"), True, None, None
            )
        call_revision = raw_call.revision + 1
        raw_result = self._raw_results.get(item_id)
        result_native = (
            raw_result.native_id if raw_result is not None else _codex_scoped_id(item_id, "result")
        )
        result_revision = raw_result.revision + 1 if raw_result is not None else call_revision
        return RichToolIdentity(
            raw_call.native_id, result_native, False, call_revision, result_revision
        )

    def remember(self, record: dict, payload: dict) -> None:
        record_kind = record.get("type")
        if record_kind == "response_item":
            item_type = payload.get("type")
            call_id = _trajectory_id(payload.get("call_id"))
            if item_type in CODEX_RAW_TOOL_CALL_TYPES and not self.rich_covers_call(call_id):
                native = _trajectory_id(payload.get("id")) or call_id
                if call_id is not None and native is not None:
                    self._raw_calls[call_id] = RecordLocation(
                        native, _codex_revision(record, payload)
                    )
            elif item_type in CODEX_RAW_TOOL_RESULT_TYPES and call_id not in self._rich_items:
                native = _trajectory_id(payload.get("id"))
                if call_id is not None and native is not None:
                    self._raw_results[call_id] = RecordLocation(
                        native, _codex_revision(record, payload)
                    )
            self._bound()
            return
        if record_kind != "event_msg" or payload.get("type") != "item_completed":
            return
        item = payload.get("item")
        if not isinstance(item, dict) or item.get("type") not in {"FileChange", "McpToolCall"}:
            return
        item_id = _trajectory_id(item.get("id"))
        if item_id is not None:
            self._rich_items[item_id] = self._adopt(item, item_id)
            self._bound()

    def _bound(self) -> None:
        for table in (self._raw_calls, self._raw_results, self._rich_items):
            while len(table) > TRAJECTORY_MCP_CALL_CONTEXT_LIMIT:
                table.pop(next(iter(table)))
