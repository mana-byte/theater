"""Causal final-answer seal: the ``turn_duration`` marker names the record that ended a turn."""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, replace

from .constants import SEAL_MAX_HOPS, SEAL_MESSAGE_LIMIT, SEAL_MIN_VERSION, SEAL_NODE_LIMIT

_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)")

#: Unsealed reasons caused by a record the chain needs not being read yet: retry as more arrive.
_RETRYABLE = frozenset({"unknown_parent", "incomplete_message"})


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
    sidechain: bool = False
    ts: float | None = None


@dataclass(frozen=True, slots=True)
class _Deferred:
    """A held marker bound to its origin; only its parent, session, ts and line index are kept."""

    parent: str | None
    session_id: str | None
    ts: float | None
    index: int
    #: Copied history was read while holding: only the prompt the message answers may release it.
    skipped: bool = False


def _is_prompt(record: dict) -> bool:
    """A main-thread user turn with text of its own: not a tool result, injected or summary text."""
    if (
        record.get("type") != "user"
        or record.get("isSidechain") is True
        or record.get("isMeta") is True
        or record.get("isCompactSummary") is True
    ):
        return False
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return bool(content.strip())
    return isinstance(content, list) and any(
        isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
        and block["text"].strip()
        for block in content
    )


def _completes_safely(record: dict) -> bool:
    """Only a bare attachment-like record may finish a held marker.

    Anything that carries its own terminal event or could be a fragment of the answer
    (assistant, user, system, sidechain, or message-bearing) must drop it instead.
    """
    return (
        record.get("type") not in ("assistant", "user", "system")
        and record.get("isSidechain") is not True
        and FinalAnswerSeal._message_id(record) is None
    )


class FinalAnswerSeal:
    """Remembers recent records so a marker in a later read can prove which message ended a turn."""

    def __init__(self, launched_at: float | None = None) -> None:
        #: Records stamped before this are history copied from the session this one forked.
        self._launched_at = launched_at
        #: Set while parsing a record that is copied history skipped over a held marker: the
        #: parser keeps such a record out of live accounting, which it would only confuse.
        self.skipped_history = False
        self._nodes: OrderedDict[str, _Node] = OrderedDict()
        #: message id -> {record uuid -> text}; a key means every fragment seen was recorded.
        self._texts: OrderedDict[str, dict[str, str]] = OrderedDict()
        self._awaiting: str | None = None
        #: At most one marker awaits its late parent records; superseded or dropped, never grown.
        #: It lives in this observer, so history paging (a fresh observer per page) fails closed.
        self._deferred: _Deferred | None = None

    def reset(self) -> None:
        self._nodes.clear()
        self._texts.clear()
        self._awaiting = None
        self._deferred = None
        self.skipped_history = False

    def observe(self, record: dict, ts: float | None = None) -> str | None:
        """Remember one record; returns a reason when a sealed-mode final never got its marker."""
        self.skipped_history = False
        uuid = record.get("uuid")
        if not isinstance(uuid, str) or not uuid:
            return None
        parent = record.get("parentUuid")
        message_id = self._message_id(record)
        self._nodes.pop(uuid, None)
        self._nodes[uuid] = _Node(
            parent if isinstance(parent, str) else None,
            message_id,
            record.get("isSidechain") is True,
            ts,
        )

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

    def _final_assistant(self, parent: object) -> tuple[str, str, _Node] | Unsealed:
        """The assistant record, its message and node, that a marker's parent chain ends at."""
        current = parent if isinstance(parent, str) else None
        for _ in range(SEAL_MAX_HOPS + 1):
            node = self._nodes.get(current) if current is not None else None
            if node is None:
                return Unsealed("unknown_parent")
            if node.sidechain:
                return Unsealed("sidechain")
            if node.message_id is not None and current is not None:
                return current, node.message_id, node
            current = node.parent
        return Unsealed("no_assistant_record")

    def resolve(self, marker: dict) -> Sealed | Unsealed:
        """Walk the marker's parent chain to its assistant record and prove the message is whole."""
        found = self._final_assistant(marker.get("parentUuid"))
        if isinstance(found, Unsealed):
            return found
        current, message_id, _ = found
        if message_id not in self._texts:
            return Unsealed("message_evicted")
        if not self._message_is_whole(current, message_id):
            return Unsealed("incomplete_message")
        self._awaiting = None
        return Sealed(message_id, "\n\n".join(self._texts[message_id].values()))

    def resolve_or_defer(self, marker: dict, ts: float | None, index: int) -> Sealed | Unsealed:
        """Seal now, or hold the marker when its chain needs records not read yet.

        A supported main-thread marker always supersedes any older held one, resolved or not,
        except copied history: it ended a turn no job here waits for.
        """
        held = self._deferred
        if (
            held is not None
            and self._continues(held, marker.get("sessionId"), index)
            and self._skips_history(held, marker, ts)
        ):
            self._deferred = replace(held, skipped=True)
            self.skipped_history = True
            return Unsealed("history")
        self._deferred = None
        outcome = self.resolve(marker)
        if isinstance(outcome, Unsealed) and outcome.reason in _RETRYABLE:
            parent = marker.get("parentUuid")
            session = marker.get("sessionId")
            self._deferred = _Deferred(
                parent if isinstance(parent, str) else None,
                session if isinstance(session, str) else None,
                ts,
                index,
            )
            return Unsealed("deferred")
        return outcome

    def retry_deferred(
        self, record: dict, index: int, ts: float | None
    ) -> tuple[Sealed, float | None, int] | None:
        """Seal a held marker when a later record completes its chain.

        Only an attachment-like record, or a fork's first prompt that the held message answers,
        may; fail closed on any discontinuity: a non-increasing index (rewrite/rewind), a changed
        session, or a record that could itself be a fragment of the answer.
        """
        pending = self._deferred
        if pending is None:
            return None
        if not self._continues(pending, record.get("sessionId"), index):
            self._deferred = None
            return None
        if self._skips_history(pending, record, ts):
            self._deferred = replace(pending, skipped=True)
            self.skipped_history = True
            return None
        if self._is_prompt_of(pending, record, ts):
            outcome = self.resolve({"parentUuid": pending.parent})
            self._deferred = None
            return (outcome, pending.ts, pending.index) if isinstance(outcome, Sealed) else None
        if not _completes_safely(record):
            self._deferred = None
            return None
        if pending.skipped or not isinstance(record.get("uuid"), str):
            return None  # Only its prompt releases a hold that skipped history; no uuid: no link.
        outcome = self.resolve({"parentUuid": pending.parent})
        if isinstance(outcome, Sealed):
            self._deferred = None
            return outcome, pending.ts, pending.index
        if outcome.reason not in _RETRYABLE:
            self._deferred = None
        return None

    @staticmethod
    def _continues(held: _Deferred, session: object, index: int) -> bool:
        """Whether a later record belongs to the held marker's own read of the transcript."""
        return index > held.index and (
            held.session_id is None or not isinstance(session, str) or session == held.session_id
        )

    def _is_local(self, ts: float | None) -> bool:
        """Stamped at or after the launch: written by this session, not copied from a fork's."""
        return self._launched_at is not None and ts is not None and ts >= self._launched_at

    def _is_history(self, record: dict, ts: float | None) -> bool:
        """Whether ``record`` is provably copied history: stamped before the launch, and so is every
        record it descends from, back to the root of the copy. A copy never follows what this
        session wrote, and an ancestry not yet read proves nothing."""
        if (
            self._launched_at is None
            or ts is None
            or ts >= self._launched_at
            or not isinstance(record.get("uuid"), str)
        ):
            return False
        return self._descends_from_history(record.get("parentUuid"))

    def _descends_from_history(self, parent: object) -> bool:
        """Whether ``parent`` and every record above it, back to a root, was written before the
        launch. An ancestry not read yet proves nothing."""
        current = parent
        for _ in range(SEAL_NODE_LIMIT):
            if current is None:
                return True
            node = self._nodes.get(current) if isinstance(current, str) else None
            if node is None or node.ts is None or self._launched_at is None:
                return False
            if node.ts >= self._launched_at:
                return False
            current = node.parent
        return False

    def _skips_history(self, held: _Deferred, record: dict, ts: float | None) -> bool:
        """Whether ``record`` is copied history that leaves the held marker as it is.

        Claude's fork writes its first turn before the history it continues, so that history
        arrives after the marker. It may not be part of the held message or fill a link of its
        chain; the held message itself must be one this session wrote.
        """
        if not self._is_history(record, ts):
            return False
        found = self._final_assistant(held.parent)
        if isinstance(found, Unsealed):
            return False
        _, message_id, node = found
        if not self._is_local(node.ts) or self._message_id(record) == message_id:
            return False
        return not self._chain_reaches(held.parent, record.get("uuid"))

    def _is_prompt_of(self, held: _Deferred, record: dict, ts: float | None) -> bool:
        """Whether ``record`` is a fork's first prompt, and the held message is the answer to it.

        A fork writes its first turn before that turn's own prompt. That prompt follows the copied
        history and nothing else, so it is the one prompt whose parent is provably copied: it
        began the turn the marker ended, and is heard before it. A later turn's prompt follows
        this session's own records, so it never qualifies.
        """
        if not (
            _is_prompt(record)
            and self._is_local(ts)
            and self._is_local(held.ts)
            and self._descends_from_history(record.get("parentUuid"))
            and record.get("parentUuid") is not None
        ):
            return False
        found = self._final_assistant(held.parent)
        if isinstance(found, Unsealed):
            return False
        _, message_id, node = found
        return (
            self._is_local(node.ts)
            and node.parent == record.get("uuid")
            and self._fragments_are_local(message_id)
        )

    def _fragments_are_local(self, message_id: str) -> bool:
        """Whether every fragment seen of a message was written by this session."""
        return all(
            (node := self._nodes.get(uuid)) is not None and self._is_local(node.ts)
            for uuid in self._texts.get(message_id, {})
        )

    def _chain_reaches(self, start: object, uuid: object) -> bool:
        """Whether ``uuid`` is among the records descending from ``start``, as far as known."""
        current = start
        for _ in range(SEAL_NODE_LIMIT):
            node = self._nodes.get(current) if isinstance(current, str) else None
            if node is None:
                return False
            if current == uuid:
                return True
            current = node.parent
        return False

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
            if node.sidechain:
                return False
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
