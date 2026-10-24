"""What a harness plugin declares that the live suite must prove, derived from its manifest."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from scripts.harness_compat.contract import SUITE_DOCUMENT_SCHEMA
from theater.harness.contracts.harness import Harness
from theater.harness.contracts.manifest import HookChannelManifest, OtelChannelManifest

SCHEMA = SUITE_DOCUMENT_SCHEMA


@dataclass(frozen=True, slots=True)
class Obligation:
    """One thing the manifest promises, identified as ``family:detail``."""

    family: str
    detail: str

    @property
    def id(self) -> str:
        return f"{self.family}:{self.detail}"


@dataclass(frozen=True, slots=True)
class Family:
    """A family of obligations and the live scenario module that proves it, if one exists."""

    name: str
    derive: Callable[[Harness], tuple[str, ...]]
    #: The test module under ``tests/conformance/live``; None while no scenario exists.
    scenario: str | None
    #: Why a family without a scenario stays unproven.
    reason: str = ""


def _approvals(harness: Harness) -> tuple[str, ...]:
    return tuple(harness.launch_parameter_support.approvals)


def _always(_harness: Harness) -> tuple[str, ...]:
    return ("prompt",)


def _flag(name: str, present: Callable[[Harness], bool]) -> Callable[[Harness], tuple[str, ...]]:
    return lambda harness: (name,) if present(harness) else ()


def _channels(harness: Harness) -> tuple[str, ...]:
    return tuple(
        enrichment.declaration.id
        for enrichment in harness.observer.enrichment_manifests()
        if isinstance(enrichment, (HookChannelManifest, OtelChannelManifest))
        and enrichment.unavailable_reason is None
    )


def _interrupt(harness: Harness) -> bool:
    return harness.controls is not None and harness.controls.interrupt is not None


def _steer(harness: Harness) -> bool:
    return harness.controls is not None and harness.controls.steer is not None


def _gating(harness: Harness, details: dict[str, str]) -> tuple[str, ...]:
    """The outcomes provable under the approvals this harness declares."""
    approvals = _approvals(harness)
    return tuple(detail for detail, approval in details.items() if approval in approvals)


def _mcp(harness: Harness) -> tuple[str, ...]:
    if not harness.supports_mcp_rendering:
        return ()
    return _gating(harness, {"gated": "manual", "call": "yolo"})


def _edit(harness: Harness) -> tuple[str, ...]:
    if harness.compat is None or harness.compat.native_edit is None:
        return ()
    return _gating(harness, {"gated": "manual", "runs": "edits"})


FAMILIES: tuple[Family, ...] = (
    Family("launch", _approvals, "test_launch.py"),
    Family("turn", _always, "test_turn.py"),
    Family("interrupt", _flag("interrupt", _interrupt), "test_interrupt.py"),
    Family("steer", _flag("steer", _steer), None, "scenario pending"),
    Family(
        "resume", _flag("resume", lambda h: h.launch_parameter_support.resume), "test_resume.py"
    ),
    Family("model", _flag("model", lambda h: h.launch_parameter_support.model), "test_model.py"),
    Family(
        "effort",
        _flag("effort", lambda h: h.launch_parameter_support.reasoning_effort),
        None,
        "scenario pending",
    ),
    Family("mcp", _mcp, "test_mcp.py"),
    Family("edit", _edit, "test_edit.py"),
    Family("channels", _channels, None, "scenario pending"),
    Family("runtime", _flag("native", lambda h: h.runtime is not None), None, "scenario pending"),
)


def obligations_for(harness: Harness, family: str) -> tuple[Obligation, ...]:
    declared = next(item for item in FAMILIES if item.name == family)
    return tuple(Obligation(family, detail) for detail in declared.derive(harness))


def required(harness: Harness) -> tuple[Obligation, ...]:
    """Every obligation the manifest declares, independent of what pytest collects."""
    return tuple(
        obligation for family in FAMILIES for obligation in obligations_for(harness, family.name)
    )


def document(harness: str, version: str, obligations: tuple[Obligation, ...]) -> dict[str, object]:
    """The ``required.json`` body: the pipeline reconciles results against it."""
    scenarios = {family.name: family.scenario for family in FAMILIES}
    return {
        "schema": SCHEMA,
        "harness": harness,
        "version": version,
        "obligations": [
            {
                "id": obligation.id,
                "family": obligation.family,
                "scenario": scenarios[obligation.family],
            }
            for obligation in obligations
        ],
    }
