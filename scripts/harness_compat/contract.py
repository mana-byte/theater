"""Frozen contract between the harness-compat framework and its per-harness adapters.

Only the master edits this file; a stream that needs a change escalates instead.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

#: ``tooling`` means our own machinery failed (mock gap, inconsistent bump), never upstream.
Verdict = Literal["green", "red", "inconclusive", "tooling"]
CheckStatus = Literal["pass", "fail", "skip", "error"]
#: Wire protocols the mock speaks; "openai-chat" is Chat Completions.
MockProtocol = Literal["anthropic", "openai-chat", "openai-responses"]
Route = tuple[str, str]  # (HTTP method, path without query string)

#: Legacy evidence paths remain excluded from artifacts and stale-limit scans.
EVIDENCE_ROOT = Path("tests/fixtures/harness_compat")
#: The text every mock model reply carries unless a test queues another one.
MOCK_REPLY = "MOCK-OK"

#: What the pipeline hands the live conformance suite (besides the sandbox token).
SUITE_ENV_HARNESS = "HARNESS_COMPAT_HARNESS"
SUITE_ENV_BINARY = "HARNESS_COMPAT_BINARY"
SUITE_ENV_VERSION = "HARNESS_COMPAT_VERSION"
SUITE_ENV_SANDBOX_ROOT = "HARNESS_COMPAT_SANDBOX_ROOT"
SUITE_ENV_EVIDENCE_DIR = "HARNESS_COMPAT_EVIDENCE_DIR"
#: Names a mutation canary (``tests/support/mutations.py``); never set for a release run.
SUITE_ENV_MUTATION = "HARNESS_COMPAT_MUTATION"
#: Schema of ``required.json`` and ``inventory.json`` in the evidence directory.
SUITE_DOCUMENT_SCHEMA = 1
#: How an obligation can end in ``inventory.json``; None means no scenario ran for it.
SUITE_OUTCOMES = ("passed", "failed", "error", "skipped")

_STABLE = re.compile(r"\d+\.\d+\.\d+")


class SuiteDocumentError(ValueError):
    """A ``required.json`` or ``inventory.json`` that cannot be trusted, and why."""


@dataclass(frozen=True, slots=True)
class SuiteRow:
    """One obligation: promised in ``required.json``, accounted for in ``inventory.json``."""

    id: str
    family: str
    #: The live test module that proves the family; None while no scenario exists.
    scenario: str | None
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
        # A missing key is not a declared gap: only an explicit null says no scenario exists yet.
        if "scenario" not in body or not isinstance(
            scenario := body["scenario"], (str, type(None))
        ):
            raise SuiteDocumentError(f"gives {name} no scenario module name (or an explicit null)")
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


def is_stable(version: str) -> bool:
    """True for a plain MAJOR.MINOR.PATCH release (no prerelease or build suffix)."""
    return _STABLE.fullmatch(version) is not None


def version_key(version: str) -> tuple[int, int, int]:
    """Sort key of a stable release; raises ValueError for anything else."""
    if not is_stable(version):
        raise ValueError(f"not a stable MAJOR.MINOR.PATCH release: {version!r}")
    major, minor, patch = (int(part) for part in version.split("."))
    return major, minor, patch


@dataclass(frozen=True, slots=True)
class VerifiedSet:
    """A bounded [floor, ceiling) gate; ``known_bad`` releases are never admitted."""

    kind: Literal["range"]
    floor: str
    ceiling: str
    known_bad: frozenset[str] = frozenset()

    def admits(self, version: str) -> bool:
        if version in self.known_bad:
            return False
        key = version_key(version)
        return version_key(self.floor) <= key < version_key(self.ceiling)


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: CheckStatus
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Result:
    """Outcome of qualifying one release; adapters leave ``adapter_digest`` empty."""

    harness: str
    version: str
    verdict: Verdict
    checks: tuple[Check, ...] = ()
    evidence_dir: Path | None = None
    adapter_digest: str = ""
    #: A non-authoritative second opinion (the live conformance suite during its shadow period):
    #: recorded and summarised next to the verdict, never part of it.
    shadow_checks: tuple[Check, ...] = ()
    #: Every obligation the live suite derived and accounted for in this run (empty otherwise).
    inventory: tuple[str, ...] = ()
    #: The plugin tree with its verified gate literals normalised: the inventory baseline's key.
    plugin_digest: str = ""

    def to_evidence(self) -> dict[str, Any]:
        """The deterministic JSON document committed as the release's evidence file."""
        document: dict[str, Any] = {
            "harness": self.harness,
            "version": self.version,
            "verdict": self.verdict,
            "adapter_digest": self.adapter_digest,
            "checks": _checks_document(self.checks),
        }
        if self.shadow_checks:
            document["shadow"] = _checks_document(self.shadow_checks)
        if self.inventory:
            document["inventory"] = sorted(self.inventory)
            document["plugin_digest"] = self.plugin_digest
        return document


def _checks_document(checks: Sequence[Check]) -> list[dict[str, str]]:
    return [
        {"name": check.name, "status": check.status, "detail": check.detail} for check in checks
    ]


def verdict_from_checks(checks: Sequence[Check]) -> Verdict:
    """A failed proof is red; an errored or empty run is inconclusive; skips are pre-approved."""
    if any(check.status == "fail" for check in checks):
        return "red"
    if any(check.status == "error" for check in checks):
        return "inconclusive"
    return "green" if any(check.status == "pass" for check in checks) else "inconclusive"


@dataclass(frozen=True, slots=True)
class MockToolCall:
    name: str
    #: JSON object arguments, or the raw input of a freeform (grammar) tool (Responses only).
    arguments: Mapping[str, Any] | str


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


class MockControl(Protocol):
    """The running mock model server, as an adapter sees it."""

    def queue(self, protocol: MockProtocol, reply: MockReply) -> None:
        """Serve ``reply`` once, FIFO, to the next model request on that protocol."""
        ...

    def hold(self) -> Hold:
        """A new hold to attach to a queued reply."""
        ...

    @property
    def requests(self) -> tuple[RecordedRequest, ...]: ...

    @property
    def unhandled(self) -> tuple[RecordedRequest, ...]:
        """Requests the mock had no handler for; it answered them with HTTP 599."""
        ...


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
    mock: MockControl
    #: "anthropic" is the origin http://127.0.0.1:P; the OpenAI protocols end in /v1.
    mock_base_urls: Mapping[str, str]
    mock_requests: Path  # JSONL the mock appends every request to
    env: Mapping[str, str]
    offline_env: Mapping[str, str]


class Adapter(Protocol):
    """One harness. Streams implement this in ``adapters/<name>.py`` and export ``ADAPTER``.

    Optional attributes, read with their defaults: ``plugin`` and ``artifact_roots`` (inputs.py);
    ``gate_literals``, ``((repo-relative path, (assignment names, ...)), ...)``, the verified gate
    literals a bump rewrites, so the obligation baseline survives an ordinary bump; and
    ``shadow_suite``: run the live conformance suite after a conclusive verdict and record it as a
    non-authoritative shadow result (default False).
    """

    name: str
    #: Dotted ``package.module:function`` path of the plugin's compatibility probe.
    probe: str
    #: Text the stub binary prints for ``--help`` when the probe reads it ("" if unused).
    stub_help: str
    #: Pytest node ids that prove the candidate with the real binary; they decide green or red.
    green_tests: tuple[str, ...]
    #: Pytest node ids that only check a bump is coherent; a failure is a tooling failure.
    consistency_tests: tuple[str, ...]
    expected_skips: Mapping[str, str]  # node id -> reason; any other skip is inconclusive
    expected_routes: frozenset[Route]  # mock routes the CLI is expected to call
    ignorable_routes: frozenset[Route]  # unhandled routes that never matter (telemetry, ...)

    def discover(self) -> list[str]:
        """Every stable upstream release, ascending."""
        ...

    def verified(self, root: Path) -> VerifiedSet:
        """Read the gate from the files under ``root``; never import ``theater`` to do it."""
        ...

    def install(self, sb: Sandbox, version: str) -> Path:
        """Install exactly ``version`` under ``sb.bin_dir``; raise InstallFailed on failure."""
        ...

    def version_line(self, version: str) -> str:
        """What ``<binary> --version`` prints for ``version``."""
        ...

    def apply_bump(self, root: Path, greens: Sequence[str]) -> list[Path]:
        """Admit ``greens`` (ascending) in every coupled file under ``root``.

        Idempotent and deterministic; returns the changed paths relative to ``root``.
        """
        ...

    def qualify(self, root: Path, version: str, sb: Sandbox, binary: Path) -> Result:
        """Prove ``binary`` against the tree at ``root`` (already bumped to ``version``)."""
        ...
