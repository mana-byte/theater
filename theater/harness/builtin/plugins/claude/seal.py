"""Causal final-answer seal: the ``turn_duration`` marker names the record that ended a turn."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, replace
from enum import Enum

from .constants import (
    SEAL_MAX_HOPS,
    SEAL_MESSAGE_LIMIT,
    SEAL_NODE_LIMIT,
    SEAL_TEXT_BYTES_LIMIT,
)

#: Unsealed reasons caused by a record the chain needs not being read yet: retry as more arrive.
_RETRYABLE = frozenset({"unknown_parent", "incomplete_message"})


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
    #: Attachment-like: nothing of an answer, so it may sit between a prompt and its answer.
    bare: bool = False


class _Chain(Enum):
    """Whether a held message's ancestry leads to a prompt through attachment-like records only."""

    REACHED = "reached"
    UNREAD = "unread"
    BROKEN = "broken"


@dataclass(frozen=True, slots=True)
class _Deferred:
    """A held marker bound to its origin; only its parent, session, ts and line index are kept."""

    parent: str | None
    session_id: str | None
    ts: float | None
    index: int
    #: Copied history was read while holding: only the prompt the message answers may release it.
    skipped: bool = False
    #: That prompt was read, but the attachments between it and the message were not all yet.
    prompt: str | None = None


def is_prompt(record: dict) -> bool:
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
        #: Proven copied ancestry survives root eviction without retaining copied messages.
        self._history: OrderedDict[str, _Node] = OrderedDict()
        self._guard_history = False
        self.failure: str | None = None
        #: message id -> {record uuid -> text}; a key means every fragment seen was recorded.
        self._texts: OrderedDict[str, dict[str, str]] = OrderedDict()
        self._text_bytes = 0
        self._awaiting: str | None = None
        #: At most one marker awaits its late parent records; superseded or dropped, never grown.
        #: It lives in this observer, so history paging (a fresh observer per page) fails closed.
        self._deferred: _Deferred | None = None

    def reset(self, *, history_guard: bool = False) -> None:
        self._nodes.clear()
        self._history.clear()
        self._texts.clear()
        self._text_bytes = 0
        self._guard_history = history_guard
        self.failure = None
        self._awaiting = None
        self._deferred = None
        self.skipped_history = False

    def observe(
        self, record: dict, ts: float | None = None, *, index: int | None = None
    ) -> str | None:
        """Remember one record; returns a reason when a sealed-mode final never got its marker."""
        self.skipped_history = False
        if self.failure is not None:
            return None
        pending = self._deferred
        if (
            pending is not None
            and index is not None
            and not self._continues(pending, record.get("sessionId"), index)
        ):
            self._deferred = None
            if pending.skipped:
                self._fail("fork_discontinuity")
                return None
        uuid = record.get("uuid")
        if not isinstance(uuid, str) or not uuid:
            pending = self._deferred
            if not _completes_safely(record) and (
                self._guard_history
                or (pending is not None and self._is_local(pending.ts) and not self._is_local(ts))
            ):
                self._fail("fork_record_identity_unavailable")
            return None
        parent = record.get("parentUuid")
        message_id = self._message_id(record)
        node = _Node(
            parent if isinstance(parent, str) else None,
            message_id,
            record.get("isSidechain") is True,
            ts,
            _completes_safely(record),
        )
        previous = self._nodes.get(uuid) or self._history.get(uuid)
        if previous is not None and previous != node:
            self._fail("conflicting_record")
            return None
        if self._skip_history(record, uuid, node):
            return None
        self._nodes.pop(uuid, None)
        self._nodes[uuid] = node
        if message_id is not None:
            self._remember_text(record, uuid, message_id)
            if self.failure is not None:
                return None
        self._prune_nodes()
        if self.failure is not None:
            return None
        if record.get("type") == "user" and self._awaiting is not None:
            self._awaiting = None
            return "marker_missing"
        return None

    def _remember_text(self, record: dict, uuid: str, message_id: str) -> None:
        fragments = self._texts.setdefault(message_id, {})
        self._texts.move_to_end(message_id)
        text = self._text_of(record)
        if uuid in fragments and fragments[uuid] != text:
            self._fail("conflicting_fragment")
            return
        self._text_bytes += len(text.encode("utf-8")) - len(fragments.get(uuid, "").encode("utf-8"))
        fragments[uuid] = text
        protected = {message_id}
        found = self._final_assistant(self._deferred.parent) if self._deferred is not None else None
        if isinstance(found, tuple):
            protected.add(found[1])
        while len(self._texts) > SEAL_MESSAGE_LIMIT or self._text_bytes > SEAL_TEXT_BYTES_LIMIT:
            oldest = next((key for key in self._texts if key not in protected), None)
            if oldest is None:
                self._fail("seal_text_limit")
                return
            self._drop_text(oldest)

    def _prune_nodes(self) -> None:
        """Evict cache outside the held message's fragments and causal links."""
        if len(self._nodes) <= SEAL_NODE_LIMIT:
            return
        pending = self._deferred
        found = self._final_assistant(pending.parent) if pending is not None else None
        message_id = found[1] if isinstance(found, tuple) else None
        current = pending.parent if pending is not None else None
        protected: set[str] = set()
        for _ in range(SEAL_NODE_LIMIT + 1):
            if current is None or current in protected:
                break
            protected.add(current)
            node = self._nodes.get(current)
            if node is None or (
                not node.bare and not (message_id is not None and node.message_id == message_id)
            ):
                break
            current = node.parent
        if message_id is not None:
            protected.update(self._texts.get(message_id, {}))
        while len(self._nodes) > SEAL_NODE_LIMIT:
            oldest = next((key for key in self._nodes if key not in protected), None)
            if oldest is None:
                self._fail("seal_node_limit")
                return
            evicted = self._nodes.pop(oldest)
            if evicted.message_id is not None:
                self._drop_text(evicted.message_id)

    def _skip_history(self, record: dict, uuid: str, node: _Node) -> bool:
        """Classify copies before they can evict or contaminate a live causal chain."""
        pending = self._deferred
        if self._is_history(record, node.ts):
            self._history[uuid] = node
            self._history.move_to_end(uuid)
            while len(self._history) > SEAL_NODE_LIMIT:
                self._history.popitem(last=False)
            found = self._final_assistant(pending.parent) if pending is not None else None
            if (
                pending is not None
                and isinstance(found, tuple)
                and self._is_local(pending.ts)
                and self._is_local(found[2].ts)
            ):
                self._guard_history = True
            if self._guard_history:
                self.skipped_history = True
                if (
                    pending is not None
                    and isinstance(found, tuple)
                    and (node.message_id == found[1] or self._chain_reaches(pending.parent, uuid))
                ):
                    self._fail("copied_answer_fragment")
                elif self._deferred is not None:
                    self._deferred = replace(self._deferred, skipped=True)
                return True
        elif (
            self._guard_history
            and not self._is_local(node.ts)
            and self._history_ancestry(node.parent) is None
        ):
            self._fail("fork_ancestry_unavailable")
            return True
        return False

    def _drop_text(self, message_id: str) -> None:
        self._text_bytes -= sum(
            len(text.encode("utf-8")) for text in self._texts.pop(message_id, {}).values()
        )

    def _fail(self, reason: str) -> None:
        """A lost proof cannot be downgraded to live replay or quiet-time rescue."""
        self.failure = reason
        self._deferred = None
        self._awaiting = None
        self._nodes.clear()
        self._history.clear()
        self._texts.clear()
        self._text_bytes = 0

    @property
    def pending(self) -> bool:
        return self._deferred is not None

    def expect_marker(self, message_id: str | None) -> None:
        if not self.skipped_history and self.failure is None:
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
        if self.failure is not None:
            return Unsealed(self.failure)
        found = self._final_assistant(marker.get("parentUuid"))
        if isinstance(found, Unsealed):
            return found
        current, message_id, _ = found
        if message_id not in self._texts:
            return Unsealed("message_evicted")
        if not self._message_is_whole(current, message_id):
            return Unsealed("incomplete_message")
        self._awaiting = None
        return Sealed(
            message_id, "\n\n".join(text for text in self._texts[message_id].values() if text)
        )

    def resolve_or_defer(self, marker: dict, ts: float | None, index: int) -> Sealed | Unsealed:
        """Seal now, or hold the marker when its chain needs records not read yet.

        A supported main-thread marker always supersedes any older held one, resolved or not,
        except copied history: it ended a turn no job here waits for.
        """
        if self.skipped_history:
            return Unsealed("history")
        if self.failure is not None:
            return Unsealed(self.failure)
        self._deferred = None
        self._awaiting = None
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

        Only an attachment-like record, or a fork's first prompt that the held message answers
        (and the attachments that link them, whichever of these is read last), may; fail closed on
        any discontinuity: a non-increasing index (rewrite/rewind), a changed session, or a record
        that could itself be a fragment of the answer.
        """
        pending = self._deferred
        if pending is None or self.skipped_history or self.failure is not None:
            return None
        if pending.prompt is not None:
            # One prompt is awaited: only attachments that may link it count, anything else drops.
            if _completes_safely(record):
                return self._follow_prompt(pending, pending.prompt)
        elif self._is_first_prompt(record, ts):
            return self._follow_prompt(pending, record["uuid"])
        if not _completes_safely(record):
            self._deferred = None
            if pending.skipped:
                self._fail("fork_chain_broken")
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
        return self._history_ancestry(parent) is True

    def _history_ancestry(self, parent: object) -> bool | None:
        """Whether ``parent`` and every record above it, back to a root, was written before the
        launch. An ancestry not read yet proves nothing."""
        current = parent
        for _ in range(SEAL_NODE_LIMIT):
            if current is None:
                return True
            if isinstance(current, str) and current in self._history:
                return True
            node = self._nodes.get(current) if isinstance(current, str) else None
            if node is None or node.ts is None or self._launched_at is None:
                return None
            if node.ts >= self._launched_at:
                return False
            current = node.parent
        return None

    def _is_first_prompt(self, record: dict, ts: float | None) -> bool:
        """Whether ``record`` is a fork's first prompt: typed, written by this session, and the one
        record that follows the copied history and nothing else.

        A fork writes its first turn before that turn's own prompt, which therefore began the turn
        a held marker ended. A later turn's prompt follows this session's own records, so it never
        qualifies.
        """
        return (
            is_prompt(record)
            and isinstance(record.get("uuid"), str)
            and self._is_local(ts)
            and record.get("parentUuid") is not None
            and self._descends_from_history(record.get("parentUuid"))
        )

    def _follow_prompt(
        self, held: _Deferred, prompt: str
    ) -> tuple[Sealed, float | None, int] | None:
        """Release the message once its ancestry reaches ``prompt``; wait while that is unread."""
        chain = self._chain_to_prompt(held.parent, held.ts, prompt)
        if chain is _Chain.UNREAD:
            self._deferred = replace(held, prompt=prompt)
            return None
        self._deferred = None
        if chain is _Chain.BROKEN:
            if held.skipped:
                self._fail("fork_chain_broken")
            return None
        outcome = self.resolve({"parentUuid": held.parent})
        return (outcome, held.ts, held.index) if isinstance(outcome, Sealed) else None

    def local_turn_known(self, parent: object, prompt: object) -> bool:
        """Whether a retained message has a proved launch-local chain to this typed prompt."""
        found = self._final_assistant(parent)
        prompt_node = self._nodes.get(prompt) if isinstance(prompt, str) else None
        return (
            isinstance(prompt, str)
            and prompt_node is not None
            and self._is_local(prompt_node.ts)
            and not prompt_node.sidechain
            and isinstance(found, tuple)
            and self._chain_to_prompt(parent, found[2].ts, prompt) is _Chain.REACHED
        )

    def _chain_to_prompt(self, parent: object, ts: float | None, prompt: str) -> _Chain:
        """Whether the held message, written by this session alone, descends from ``prompt``
        through attachment-like records of this session and its own earlier fragments."""
        found = self._final_assistant(parent)
        if isinstance(found, Unsealed):
            return _Chain.BROKEN
        _, message_id, node = found
        if not (self._is_local(ts) and self._is_local(node.ts)):
            return _Chain.BROKEN
        if not self._fragments_are_local(message_id):
            return _Chain.BROKEN
        current = node.parent
        for _ in range(SEAL_NODE_LIMIT):
            if current == prompt:
                return _Chain.REACHED
            link = self._nodes.get(current) if isinstance(current, str) else None
            if link is None:
                return _Chain.UNREAD if isinstance(current, str) else _Chain.BROKEN
            if link.sidechain or not self._is_local(link.ts):
                return _Chain.BROKEN
            if link.message_id != message_id and not link.bare:
                return _Chain.BROKEN  # an earlier fragment of the message itself is no break
            current = link.parent
        return _Chain.BROKEN

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
