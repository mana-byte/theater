"""What a harness declares that the live suite must prove, derived from its manifest and adapter."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from harness_compat.adapters import ADAPTER_NAMES, load_adapter
from harness_compat.contract import SuiteDocument, SuiteRow
from theater.harness.contracts.harness import Harness
from theater.harness.contracts.manifest import HookChannelManifest, OtelChannelManifest
from theater.harness.contracts.runtime import RuntimeCapability

#: Obligations proven under a plugin configured on purpose: the mutation of that name, applied as
#: a variant of the shipped manifest (an unqualified release is a real state, not only a canary).
_VARIANTS = {"runtime:legacy": "native-runtime-refused"}


@dataclass(frozen=True, slots=True)
class Obligation:
    """One thing the manifest promises, identified as ``family:detail``."""

    family: str
    detail: str

    @property
    def id(self) -> str:
        return f"{self.family}:{self.detail}"

    @property
    def variant(self) -> str | None:
        """The mutation whose wrapper this obligation's scenario runs under, if any."""
        return _VARIANTS.get(self.id)


@dataclass(frozen=True, slots=True)
class Family:
    """A family of obligations and the live scenario module that exercises it."""

    name: str
    derive: Callable[[Harness], tuple[str, ...]]
    #: The test module under ``harness_compat/conformance/live``.
    scenario: str


def _approvals(harness: Harness) -> tuple[str, ...]:
    return tuple(harness.launch_parameter_support.approvals)


def _launch(harness: Harness) -> tuple[str, ...]:
    # The planner contract carries a launch prompt for every harness (LaunchContext.prompt).
    return (*_approvals(harness), "prompt")


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


def _unavailable(harness: Harness, capability: RuntimeCapability) -> bool:
    """Whether the runtime manifest declares ``capability`` unavailable, so it must be refused."""
    return harness.runtime is not None and capability in harness.runtime.unavailable_capabilities


def _steer(harness: Harness) -> tuple[str, ...]:
    """A declared steer is proven; a declared-unavailable one must be refused, never queued."""
    if _unavailable(harness, RuntimeCapability.STEER):
        return ("refused",)
    declared = harness.controls is not None and harness.controls.steer is not None
    return ("steer",) if declared else ()


def _gating(harness: Harness, details: dict[str, str]) -> tuple[str, ...]:
    """The outcomes provable under the approvals this harness declares."""
    approvals = _approvals(harness)
    return tuple(detail for detail, approval in details.items() if approval in approvals)


def _mcp(harness: Harness) -> tuple[str, ...]:
    if not harness.supports_mcp_rendering:
        return ()
    return _gating(harness, {"gated": "manual", "call": "yolo", "refusal": "yolo"})


def _edit(harness: Harness) -> tuple[str, ...]:
    # The recipe is the adapter's: a harness without one has no native edit to prove.
    if harness.name not in ADAPTER_NAMES or load_adapter(harness.name).compat.native_edit is None:
        return ()
    return _gating(harness, {"gated": "manual", "runs": "edits"})


def _runtime(harness: Harness) -> tuple[str, ...]:
    """Native routing, reconnect, queue/settings behavior and declared refusals."""
    if harness.runtime is None:
        return ()
    refused = sorted(
        capability.value
        for capability in harness.runtime.unavailable_capabilities
        if capability is not RuntimeCapability.STEER  # the steer family proves its own refusal
    )
    supported = tuple(
        name
        for name, capability in (
            ("queue", RuntimeCapability.QUEUE_FOLLOWUP),
            ("settings", RuntimeCapability.SETTINGS_UPDATE),
        )
        if capability not in harness.runtime.unavailable_capabilities
    )
    return (
        "native",
        "survival",
        *(("legacy",) if harness.observer.has_screen else ()),
        *supported,
        *(f"unavailable-{name}" for name in refused),
    )


FAMILIES: tuple[Family, ...] = (
    Family("launch", _launch, "test_launch.py"),
    Family("turn", _always, "test_turn.py"),
    Family("interrupt", _flag("interrupt", _interrupt), "test_interrupt.py"),
    Family("steer", _steer, "test_steer.py"),
    Family(
        "resume", _flag("resume", lambda h: h.launch_parameter_support.resume), "test_resume.py"
    ),
    Family("model", _flag("model", lambda h: h.launch_parameter_support.model), "test_model.py"),
    Family(
        "effort",
        _flag("effort", lambda h: h.launch_parameter_support.reasoning_effort),
        "test_effort.py",
    ),
    Family("mcp", _mcp, "test_mcp.py"),
    Family("edit", _edit, "test_edit.py"),
    Family("channels", _channels, "test_channels.py"),
    Family("runtime", _runtime, "test_runtime.py"),
)


def obligations_for(harness: Harness, family: str) -> tuple[Obligation, ...]:
    declared = next(item for item in FAMILIES if item.name == family)
    return tuple(Obligation(family, detail) for detail in declared.derive(harness))


def required(harness: Harness) -> tuple[Obligation, ...]:
    """Every obligation the manifest declares, independent of what pytest collects."""
    return tuple(
        obligation for family in FAMILIES for obligation in obligations_for(harness, family.name)
    )


def document(harness: str, version: str, obligations: tuple[Obligation, ...]) -> SuiteDocument:
    """The ``required.json`` body: the pipeline reconciles results against it."""
    scenarios = {family.name: family.scenario for family in FAMILIES}
    return SuiteDocument(
        harness,
        version,
        tuple(
            SuiteRow(obligation.id, obligation.family, scenarios[obligation.family])
            for obligation in obligations
        ),
    )
