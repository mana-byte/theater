"""Manifest compilation, validation, and generic manifest strategies."""

from __future__ import annotations

from theater.harness.manifests.compiler import ManifestHarnessObserver, compile_manifest
from theater.harness.manifests.strategies import ScreenMarker, screen_classifier_from_markers
from theater.harness.manifests.validation import (
    ManifestValidationError,
    validate_manifest,
)

__all__ = [
    "ManifestHarnessObserver",
    "ManifestValidationError",
    "ScreenMarker",
    "compile_manifest",
    "screen_classifier_from_markers",
    "validate_manifest",
]
