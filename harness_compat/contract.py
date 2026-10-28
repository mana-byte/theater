"""Contracts shared by the harness-compat pipeline, its mock model and the live suite."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, Protocol

if TYPE_CHECKING:
    from .mock.provisioning import CompatManifest

#: red records a contract assertion failure; inconclusive/tooling do not establish a binary defect.
Verdict = Literal["green", "red", "inconclusive", "tooling"]
CheckStatus = Literal["pass", "fail", "skip", "error"]
#: Wire protocols the mock speaks; "openai-chat" is Chat Completions.
MockProtocol = Literal["anthropic", "openai-chat", "openai-responses"]
Route = tuple[str, str]  # (HTTP method, path without query string)

#: The text every mock model reply carries unless a test queues another one.
MOCK_REPLY = "MOCK-OK"

#: What the pipeline hands the live conformance suite (besides the sandbox token).
SUITE_ENV_HARNESS = "HARNESS_COMPAT_HARNESS"
SUITE_ENV_BINARY = "HARNESS_COMPAT_BINARY"
SUITE_ENV_VERSION = "HARNESS_COMPAT_VERSION"
SUITE_ENV_SANDBOX_ROOT = "HARNESS_COMPAT_SANDBOX_ROOT"
SUITE_ENV_EVIDENCE_DIR = "HARNESS_COMPAT_EVIDENCE_DIR"
#: Names a mutation canary (``harness_compat/support/mutations.py``); never set for a release run.
SUITE_ENV_MUTATION = "HARNESS_COMPAT_MUTATION"
#: Schema of ``required.json`` and ``inventory.json``, written transiently into the sandbox.
SUITE_DOCUMENT_SCHEMA = 1
#: How an obligation can end in ``inventory.json``; None means no scenario ran for it.
SUITE_OUTCOMES = ("passed", "failed", "error", "skipped", "tooling")


class SuiteDocumentError(ValueError):
    """A ``required.json`` or ``inventory.json`` that cannot be trusted, and why."""


@dataclass(frozen=True, slots=True)
class SuiteRow:
    """One obligation: promised in ``required.json``, accounted for in ``inventory.json``."""

    id: str
    family: str
    #: The live test module that exercises the family.
    scenario: str
    #: Inventory only: whether a scenario ran for it (None in a promise), and how it ended.
    executed: bool | None = None
    outcome: str | None = None
    detail: str = ""

    def to_json(self) -> dict[str, object]:
        row: dict[str, object] = {"id": self.id, "family": self.family, "scenario": self.scenario}
        if self.executed is not None:
            row |= {
                "status": "executed" if self.executed else "unproven",
                "outcome": self.outcome,
                "detail": self.detail,
            }
        return row

    @classmethod
    def parse(cls, body: object, *, inventory: bool) -> SuiteRow:
        if not isinstance(body, dict):
            raise SuiteDocumentError("holds a row that is not an object")
        name, family = body.get("id"), body.get("family")
        if not (isinstance(name, str) and name and isinstance(family, str) and family):
            raise SuiteDocumentError(f"holds a row without an id and a family: {body!r}")
        if not isinstance(scenario := body.get("scenario"), str) or not scenario:
            raise SuiteDocumentError(f"gives {name} no scenario module name")
        if not inventory:
            return cls(name, family, scenario)
        status, outcome, detail = body.get("status"), body.get("outcome"), body.get("detail", "")
        if not isinstance(detail, str):
            raise SuiteDocumentError(f"gives {name} a detail that is not text")
        if status == "executed":
            if outcome not in SUITE_OUTCOMES:
                raise SuiteDocumentError(f"gives executed {name} the outcome {outcome!r}")
        elif status == "unproven":
            if outcome is not None:
                raise SuiteDocumentError(f"gives unproven {name} the outcome {outcome!r}")
        else:
            raise SuiteDocumentError(f"gives {name} the status {status!r}")
        return cls(name, family, scenario, status == "executed", outcome, detail)


@dataclass(frozen=True, slots=True)
class SuiteDocument:
    """``required.json`` (every promised obligation) or ``inventory.json`` (what each one did)."""

    harness: str
    version: str
    rows: tuple[SuiteRow, ...]

    def to_json(self) -> dict[str, object]:
        return {
            "schema": SUITE_DOCUMENT_SCHEMA,
            "harness": self.harness,
            "version": self.version,
            "obligations": [row.to_json() for row in self.rows],
        }

    @classmethod
    def parse(cls, body: object, *, inventory: bool) -> SuiteDocument:
        if not isinstance(body, dict):
            raise SuiteDocumentError("is not a JSON object")
        if body.get("schema") != SUITE_DOCUMENT_SCHEMA:
            raise SuiteDocumentError(f"has schema {body.get('schema')!r}")
        harness, version, rows = body.get("harness"), body.get("version"), body.get("obligations")
        if not (isinstance(harness, str) and isinstance(version, str) and isinstance(rows, list)):
            raise SuiteDocumentError("lacks a harness, a version and a list of obligations")
        parsed = tuple(SuiteRow.parse(row, inventory=inventory) for row in rows)
        ids = [row.id for row in parsed]
        if twice := sorted({name for name in ids if ids.count(name) > 1}):
            raise SuiteDocumentError(f"lists {', '.join(twice)} more than once")
        return cls(harness, version, parsed)


class InstallFailed(RuntimeError):
    """The candidate binary could not be installed: the run is inconclusive, not red."""


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: CheckStatus
    detail: str = ""


class CleanupFailed(RuntimeError):
    """Temporary data or live processes remain; completed checks cannot authorize a pass."""

    checks: tuple[Check, ...] = ()


@dataclass(frozen=True, slots=True)
class Result:
    """One release's qualification/publication outcome, held only in memory."""

    harness: str
    version: str
    verdict: Verdict
    checks: tuple[Check, ...] = ()


@dataclass(frozen=True, slots=True)
class MockToolCall:
    name: str
    #: JSON object arguments, or the raw input of a freeform (grammar) tool (Responses only).
    arguments: Mapping[str, Any] | str
    #: Replaces the id the mock numbers by position, which every reply restarts from zero.
    call_id: str | None = None


class Hold(Protocol):
    """A response the mock keeps open mid-turn until it is released."""

    def wait_reached(self, timeout: float) -> bool:
        """Block until the held request is in flight and its first chunk was written."""
        ...

    def release(self) -> None:
        """Let the held response finish."""
        ...

    @property
    def cancelled(self) -> bool:
        """The client dropped the connection while the response was held."""
        ...


@dataclass(frozen=True, slots=True)
class MockReply:
    """One scripted model turn: text, or text plus tool calls."""

    text: str = MOCK_REPLY
    tool_calls: tuple[MockToolCall, ...] = ()
    #: Streams the first chunk, then stays open until released (a JSON reply withholds its body).
    hold: Hold | None = None


@dataclass(frozen=True, slots=True)
class ToolShape:
    """The top-level arguments an offered tool declares."""

    properties: tuple[str, ...] = ()
    required: tuple[str, ...] = ()
    #: The schema forbids arguments beyond ``properties``.
    closed: bool = False


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    method: str
    path: str
    handled: bool
    body_keys: tuple[str, ...]
    authorized: bool
    #: What the request asked for, read from the body: protocol is "" for non-model routes.
    protocol: str = ""
    model: str | None = None
    effort: str | None = None
    tools: tuple[str, ...] = ()
    #: Declared arguments per offered tool name, to check a scripted call fits its schema.
    tool_shapes: Mapping[str, ToolShape] = field(default_factory=dict)
    messages: int = 0
    #: The last text part of the newest human message, usually the prompt itself.
    last_user: str | None = None
    #: Every text part of the newest human message: a CLI may put its own context beside the prompt.
    last_user_parts: tuple[str, ...] = ()
    #: Every human text part in the conversation, oldest first: proves history was carried over.
    user_texts: tuple[str, ...] = ()
    #: The newest tool result the client returned to the model, if the conversation has one.
    last_tool_result: str | None = None
    stream: bool = False

    def carries(self, prompt: str) -> bool:
        """Whether the newest human message holds ``prompt`` in one of its text parts."""
        return any(prompt in part for part in self.last_user_parts)


@dataclass(frozen=True, slots=True)
class Sandbox:
    """What one run may touch; ``env`` is hermetic but network-capable (installs, ``uv``).

    ``offline_env`` adds a dead proxy so the CLI under test cannot reach the internet.
    """

    root: Path
    home: Path
    config_dir: Path
    bin_dir: Path
    evidence_dir: Path
    tmux_socket: Path
    env: Mapping[str, str]
    offline_env: Mapping[str, str]


class Adapter(Protocol):
    """A harness's release channel and provisioning; ``adapters/<name>.py`` exports one."""

    #: The canonical plugin name: ``theater/harness/builtin/plugins/<name>``.
    name: str
    #: How the live suite points this harness's stock CLI at the mock model.
    compat: CompatManifest

    def discover(self) -> list[str]:
        """Every stable upstream release, ascending."""
        ...

    def install(self, sb: Sandbox, version: str) -> Path:
        """Install exactly ``version`` under ``sb.bin_dir``; raise InstallFailed on failure."""
        ...
