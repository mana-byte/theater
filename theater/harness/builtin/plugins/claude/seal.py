"""Causal final-answer seal: the ``turn_duration`` marker names the record that ended a turn."""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass

from .constants import SEAL_MAX_HOPS, SEAL_MESSAGE_LIMIT, SEAL_MIN_VERSION, SEAL_NODE_LIMIT

_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)")


def seal_supported(record: dict) -> bool:
    """Whether this record's CLI version is verified to write the marker; unknown fails closed."""
    match = _VERSION.match(str(record.get("version") or ""))
    return match is not None and tuple(int(part) for part in match.groups()) >= SEAL_MIN_VERSION


@dataclass(frozen=True, slots=True)
class Sealed:
    message_id: str
    #: Joined text of every fragment of the final message; empty only when proven empty.
    raw_text: str


@dataclass(frozen=True, slots=True)
class Unsealed:
    reason: str


@dataclass(frozen=True, slots=True)
class _Node:
    parent: str | None
    message_id: str | None


class FinalAnswerSeal:
    """Remembers recent records so a marker in a later read can prove which message ended a turn."""

    def __init__(self) -> None:
        self._nodes: OrderedDict[str, _Node] = OrderedDict()
        #: message id -> {record uuid -> text}; a key means every fragment seen was recorded.
        self._texts: OrderedDict[str, dict[str, str]] = OrderedDict()
        self._awaiting: str | None = None

    def reset(self) -> None:
        self._nodes.clear()
        self._texts.clear()
        self._awaiting = None

    def observe(self, record: dict) -> str | None:
        """Remember one record; returns a reason when a sealed-mode final never got its marker."""
        uuid = record.get("uuid")
        if not isinstance(uuid, str) or not uuid:
            return None
        parent = record.get("parentUuid")
        message_id = self._message_id(record)
        self._nodes.pop(uuid, None)
        self._nodes[uuid] = _Node(parent if isinstance(parent, str) else None, message_id)
        while len(self._nodes) > SEAL_NODE_LIMIT:
            self._nodes.popitem(last=False)
        if message_id is not None:
            fragments = self._texts.setdefault(message_id, {})
            self._texts.move_to_end(message_id)
            text = self._text_of(record)
            if text:
                fragments[uuid] = text
            while len(self._texts) > SEAL_MESSAGE_LIMIT:
                self._texts.popitem(last=False)
        if record.get("type") == "user" and self._awaiting is not None:
            self._awaiting = None
            return "marker_missing"
        return None

    def expect_marker(self, message_id: str | None) -> None:
        self._awaiting = message_id or self._awaiting

    def resolve(self, marker: dict) -> Sealed | Unsealed:
        """Walk the marker's parent chain to its assistant record and prove the message is whole."""
        parent = marker.get("parentUuid")
        current: str | None = parent if isinstance(parent, str) else None
        for _ in range(SEAL_MAX_HOPS + 1):
            node = self._nodes.get(current) if current is not None else None
            if node is None:
                return Unsealed("unknown_parent")
            if node.message_id is not None and current is not None:
                break
            current = node.parent
        else:
            return Unsealed("no_assistant_record")
        message_id = node.message_id
        if message_id not in self._texts:
            return Unsealed("message_evicted")
        if not self._message_is_whole(current, message_id):
            return Unsealed("incomplete_message")
        self._awaiting = None
        return Sealed(message_id, "\n\n".join(self._texts[message_id].values()))

    def _message_is_whole(self, uuid: str, message_id: str) -> bool:
        """Fragments of one message chain by parentUuid; a seen predecessor outside it bounds it."""
        for _ in range(SEAL_NODE_LIMIT):
            parent = self._nodes[uuid].parent
            if parent is None:
                return True
            node = self._nodes.get(parent)
            if node is None:
                return False
            if node.message_id != message_id:
                return True
            uuid = parent
        return False

    @staticmethod
    def _message_id(record: dict) -> str | None:
        if record.get("type") != "assistant":
            return None
        message = record.get("message")
        identifier = (message.get("id") if isinstance(message, dict) else None) or record.get(
            "requestId"
        )
        return identifier if isinstance(identifier, str) and identifier else None

    @staticmethod
    def _text_of(record: dict) -> str:
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return ""
        return "\n\n".join(
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
            and block["text"]
        )
