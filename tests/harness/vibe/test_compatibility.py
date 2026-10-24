"""The Vibe version probe is display-only: it never claims a native runtime."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from theater import harness as harness_registry
from theater.harness.builtin.plugins.vibe.compatibility import (
    VERSION_POLICY,
    probe_vibe_compatibility,
    vibe_qualified_range_text,
)
from theater.harness.builtin.plugins.vibe.manifest import manifest_for_roots
from theater.harness.builtin.plugins.vibe.unified_store import STORE_FORMAT_MINOR
from theater.harness.contracts.runtime import RuntimeProbeContext
from theater.harness.contracts.version_policy import version_key
from theater.harness.manifests.validation import validate_manifest


def _version(parts: tuple[int, int, int]) -> str:
    return ".".join(map(str, parts))


_MINIMUM = version_key(VERSION_POLICY.minimum)
_MAXIMUM = version_key(VERSION_POLICY.maximum)
_BELOW = (*_MINIMUM[:2], _MINIMUM[2] - 1)
_ABOVE = (*_MAXIMUM[:2], _MAXIMUM[2] + 1)
_REFUSAL = f"Vibe harness support requires {vibe_qualified_range_text()}"


@pytest.mark.parametrize(
    ("sample", "supported", "reason"),
    [
        (_BELOW, False, _REFUSAL),
        (_MINIMUM, True, None),
        (_MAXIMUM, True, None),
        (_ABOVE, False, _REFUSAL),
    ],
)
def test_vibe_version_parse_table(sample, supported, reason, monkeypatch):
    version = _version(sample)
    monkeypatch.setattr(
        "theater.harness.builtin.plugins.vibe.compatibility.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr="", stdout=f"vibe {version}"),
    )
    result = probe_vibe_compatibility(RuntimeProbeContext())
    assert result.supported is supported
    assert result.native_version == version
    assert result.reason == reason
    assert result.policy == f"vibe-harness-{VERSION_POLICY.minimum}-compatible"


def test_malformed_version_is_refused(monkeypatch):
    monkeypatch.setattr(
        "theater.harness.builtin.plugins.vibe.compatibility.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0, stderr="", stdout=VERSION_POLICY.minimum
        ),
    )
    assert probe_vibe_compatibility(RuntimeProbeContext()).reason == (
        "Vibe did not report a stable CLI version"
    )


def test_display_range_uses_the_same_policy_as_admission():
    assert STORE_FORMAT_MINOR == 9
    declared = manifest_for_roots().native_compatibility
    assert declared is not None
    assert declared.qualified_range == vibe_qualified_range_text()


def test_nonzero_exit_is_not_a_stable_version(monkeypatch):
    monkeypatch.setattr(
        "theater.harness.builtin.plugins.vibe.compatibility.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stderr="", stdout=f"vibe {VERSION_POLICY.minimum}"
        ),
    )
    result = probe_vibe_compatibility(RuntimeProbeContext())
    assert result.supported is False
    assert result.native_version is None
    assert result.reason == "Vibe did not report a stable CLI version"


def test_missing_or_timed_out_binary_fails_closed(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(
        "theater.harness.builtin.plugins.vibe.compatibility.subprocess.run", unavailable
    )
    result = probe_vibe_compatibility(RuntimeProbeContext())
    assert result.supported is False
    assert result.native_version is None
    assert result.reason == "Vibe version probe could not complete"


def test_installed_vibe_stays_legacy_only_with_its_version_shown(monkeypatch):
    version = VERSION_POLICY.maximum
    monkeypatch.setattr(
        "theater.harness.builtin.plugins.vibe.compatibility.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stderr="", stdout=f"vibe {version}"),
    )
    validate_manifest("vibe", manifest_for_roots())
    record = harness_registry.native_compatibility_record(
        harness_registry.HARNESSES["vibe"],
        installed=True,
        result=probe_vibe_compatibility(RuntimeProbeContext()),
    )
    assert record["status"] == "legacy-only"
    assert record["installed_version"] == version
    assert record["qualified_range"] is None
    assert record["wiring"] == "legacy"
    assert record["reason"] == "no native runtime; controls use the provider terminal"
