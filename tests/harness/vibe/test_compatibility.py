"""The Vibe version probe is display-only: it never claims a native runtime."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from theater import harness as harness_registry
from theater.harness.builtin.plugins.vibe.compatibility import (
    _CEILING,
    _FLOOR,
    probe_vibe_compatibility,
    vibe_qualified_range_text,
    vibe_store_format_ceiling,
)
from theater.harness.builtin.plugins.vibe.manifest import manifest_for_roots
from theater.harness.builtin.plugins.vibe.unified_store import STORE_FORMAT_MINOR
from theater.harness.contracts.runtime import RuntimeProbeContext
from theater.harness.manifests.validation import validate_manifest


def _version(parts: tuple[int, int, int]) -> str:
    return ".".join(map(str, parts))


_FLOOR_BELOW = (_FLOOR[0], _FLOOR[1] - 1, 99)
_LAST_ADMITTED = (
    (_CEILING[0], _CEILING[1], _CEILING[2] - 1)
    if _CEILING[2]
    else (_CEILING[0], _CEILING[1] - 1, 99)
)
_NEXT_MINOR = (_CEILING[0], _CEILING[1] + 1, 0)
_REFUSAL = f"Vibe harness support requires {vibe_qualified_range_text()}"


@pytest.mark.parametrize(
    ("sample", "supported", "reason"),
    [
        (_FLOOR_BELOW, False, _REFUSAL),
        (_FLOOR, True, None),
        (_LAST_ADMITTED, True, None),
        (_CEILING, False, _REFUSAL),
        (_NEXT_MINOR, False, _REFUSAL),
    ],
)
def test_vibe_version_parse_table(sample, supported, reason, monkeypatch):
    version = _version(sample)
    monkeypatch.setattr(
        "theater.harness.builtin.plugins.vibe.compatibility.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=f"vibe {version}"),
    )
    result = probe_vibe_compatibility(RuntimeProbeContext())
    assert result.supported is supported
    assert result.native_version == version
    assert result.reason == reason
    assert result.policy == f"vibe-harness-{_version(_FLOOR)}-compatible"


def test_malformed_version_is_refused(monkeypatch):
    monkeypatch.setattr(
        "theater.harness.builtin.plugins.vibe.compatibility.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=_version(_FLOOR)),
    )
    assert probe_vibe_compatibility(RuntimeProbeContext()).reason == (
        "Vibe did not report a stable CLI version"
    )


def test_ceiling_tracks_the_reader_store_minor():
    """The launch ceiling and declared range use the probe's gate."""
    assert vibe_store_format_ceiling() == _CEILING
    assert STORE_FORMAT_MINOR == 9
    declared = manifest_for_roots().native_compatibility
    assert declared is not None
    assert declared.qualified_range == vibe_qualified_range_text()


def test_nonzero_exit_is_not_a_stable_version(monkeypatch):
    monkeypatch.setattr(
        "theater.harness.builtin.plugins.vibe.compatibility.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=f"vibe {_version(_FLOOR)}"),
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
    version = _version(_LAST_ADMITTED)
    monkeypatch.setattr(
        "theater.harness.builtin.plugins.vibe.compatibility.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=f"vibe {version}"),
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
