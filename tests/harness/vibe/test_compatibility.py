"""The Vibe version probe is display-only: it never claims a native runtime."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from theater.harness.builtin.plugins.vibe.compatibility import (
    VERSION_POLICY,
    probe_vibe_compatibility,
    vibe_qualified_range_text,
)
from theater.harness.contracts.runtime import RuntimeProbeContext
from theater.harness.contracts.version_policy import version_key


def _version(parts: tuple[int, int, int]) -> str:
    return ".".join(map(str, parts))


_MINIMUM = version_key(VERSION_POLICY.minimum)
_BELOW = (0, 0, 1)
_REFUSAL = f"Vibe harness support requires {vibe_qualified_range_text()}"


@pytest.mark.parametrize(
    ("sample", "supported", "reason"),
    [
        (_BELOW, False, _REFUSAL),
        (_MINIMUM, True, None),
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
