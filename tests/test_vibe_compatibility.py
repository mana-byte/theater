"""The Vibe version probe is display-only: it never claims a native runtime."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from theater import harness as harness_registry
from theater.harness.builtin.plugins.vibe.compatibility import (
    probe_vibe_compatibility,
    vibe_qualified_range_text,
    vibe_store_format_ceiling,
)
from theater.harness.builtin.plugins.vibe.manifest import manifest_for_roots
from theater.harness.builtin.plugins.vibe.unified_store import STORE_FORMAT_MINOR
from theater.harness.contracts.runtime import RuntimeProbeContext
from theater.harness.manifests.validation import validate_manifest


@pytest.mark.parametrize(
    ("stdout", "supported", "version", "reason"),
    [
        ("vibe 2.26.0", True, "2.26.0", None),
        ("vibe 2.25.8", True, "2.25.8", None),
        ("vibe 2.24.0", True, "2.24.0", None),
        ("vibe 2.20.0", False, "2.20.0", "Vibe harness support requires >=2.24.0,<2.27.0"),
        ("2.24.0", False, None, "Vibe did not report a stable CLI version"),
        ("vibe 2.27.0", False, "2.27.0", "Vibe harness support requires >=2.24.0,<2.27.0"),
        ("vibe 3.0.0", False, "3.0.0", "Vibe harness support requires >=2.24.0,<2.27.0"),
    ],
)
def test_vibe_version_parse_table(stdout, supported, version, reason, monkeypatch):
    monkeypatch.setattr(
        "theater.harness.builtin.plugins.vibe.compatibility.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=stdout),
    )
    result = probe_vibe_compatibility(RuntimeProbeContext())
    assert result.supported is supported
    assert result.native_version == version
    assert result.reason == reason
    assert result.policy == "vibe-harness-2.24.0-compatible"


def test_ceiling_tracks_the_reader_store_minor():
    """The qualified ceiling must not outrun the unified-store reader again.

    2.26.x writes store_format_minor 8 (vibe 376f6a33); the ceiling is the next
    release line, and the reader understands exactly that minor. The observed
    incident was this pair drifting apart: the probe admitted a writer whose
    stores the reader refused, and finished jobs waited forever.
    """
    assert vibe_store_format_ceiling() == (2, 27, 0)
    assert STORE_FORMAT_MINOR == 8
    declared = manifest_for_roots().native_compatibility
    assert declared is not None
    assert declared.qualified_range == vibe_qualified_range_text()


def test_nonzero_exit_is_not_a_stable_version(monkeypatch):
    monkeypatch.setattr(
        "theater.harness.builtin.plugins.vibe.compatibility.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout="vibe 2.25.8"),
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
    monkeypatch.setattr(
        "theater.harness.builtin.plugins.vibe.compatibility.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="vibe 2.25.8"),
    )
    validate_manifest("vibe", manifest_for_roots())
    record = harness_registry.native_compatibility_record(
        harness_registry.HARNESSES["vibe"],
        installed=True,
        result=probe_vibe_compatibility(RuntimeProbeContext()),
    )
    assert record["status"] == "legacy-only"
    assert record["installed_version"] == "2.25.8"
    assert record["qualified_range"] is None
    assert record["wiring"] == "legacy"
    assert record["reason"] == "no native runtime; controls use the provider terminal"
