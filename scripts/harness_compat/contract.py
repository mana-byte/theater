"""Frozen contract between the harness-compat framework and its per-harness adapters.

Only the master edits this file; a stream that needs a change escalates instead.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

#: ``tooling`` means our own machinery failed (mock gap, inconsistent bump), never upstream.
Verdict = Literal["green", "red", "inconclusive", "tooling"]
CheckStatus = Literal["pass", "fail", "skip", "error"]
Kind = Literal["exact", "range"]
MockProtocol = Literal["anthropic", "openai"]
Route = tuple[str, str]  # (HTTP method, path without query string)

#: Committed evidence lives at <root>/<EVIDENCE_ROOT>/<harness>/<version>.json.
EVIDENCE_ROOT = Path("tests/fixtures/harness_compat")
#: The text every mock model reply carries unless a test queues another one.
MOCK_REPLY = "MOCK-OK"

_STABLE = re.compile(r"\d+\.\d+\.\d+")


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
    """What a plugin admits: ``exact`` takes ``versions``, ``range`` takes [floor, ceiling).

    ``ceiling`` is the first release NOT admitted (None = open); ``known_bad`` is never admitted.
    """

    kind: Kind
    versions: frozenset[str] = frozenset()
    floor: str | None = None
    ceiling: str | None = None
    known_bad: frozenset[str] = frozenset()
    evidenced: frozenset[str] = frozenset()  # releases with a committed evidence file

    def admits(self, version: str) -> bool:
        if version in self.known_bad:
            return False
        if self.kind == "exact":
            return version in self.versions
        key = version_key(version)
        if self.floor is not None and key < version_key(self.floor):
            return False
        return self.ceiling is None or key < version_key(self.ceiling)


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

    def to_evidence(self) -> dict[str, Any]:
        """The deterministic JSON document committed as the release's evidence file."""
        return {
            "harness": self.harness,
            "version": self.version,
            "verdict": self.verdict,
            "adapter_digest": self.adapter_digest,
            "checks": [
                {"name": check.name, "status": check.status, "detail": check.detail}
                for check in self.checks
            ],
        }


def verdict_from_checks(checks: Sequence[Check]) -> Verdict:
    """A failed proof is red; an errored or empty run is inconclusive; skips are pre-approved."""
    if any(check.status == "fail" for check in checks):
        return "red"
    if any(check.status == "error" for check in checks):
        return "inconclusive"
    return "green" if any(check.status == "pass" for check in checks) else "inconclusive"


def evidence_file(root: Path, harness: str, version: str) -> Path:
    return root / EVIDENCE_ROOT / harness / f"{version}.json"


def load_evidenced(root: Path, harness: str) -> frozenset[str]:
    """Releases with a committed evidence file under ``root``."""
    directory = root / EVIDENCE_ROOT / harness
    if not directory.is_dir():
        return frozenset()
    return frozenset(path.stem for path in directory.glob("*.json"))


@dataclass(frozen=True, slots=True)
class MockToolCall:
    name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class MockReply:
    """One scripted model turn: text, or text plus tool calls."""

    text: str = MOCK_REPLY
    tool_calls: tuple[MockToolCall, ...] = ()


@dataclass(frozen=True, slots=True)
class RecordedRequest:
    method: str
    path: str
    handled: bool
    body_keys: tuple[str, ...]
    authorized: bool


class MockControl(Protocol):
    """The running mock model server, as an adapter sees it."""

    def queue(self, protocol: MockProtocol, reply: MockReply) -> None:
        """Serve ``reply`` once, FIFO, to the next model request on that protocol."""
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
    mock_base_urls: Mapping[str, str]  # "anthropic": http://127.0.0.1:P, "openai": .../v1
    mock_requests: Path  # JSONL the mock appends every request to
    env: Mapping[str, str]
    offline_env: Mapping[str, str]


class Adapter(Protocol):
    """One harness. Streams implement this in ``adapters/<name>.py`` and export ``ADAPTER``."""

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
