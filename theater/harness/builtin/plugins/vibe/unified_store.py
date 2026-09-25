"""Public facade for the bounded, read-only Vibe unified-session store."""

from __future__ import annotations

from . import unified_store_loading as _loading
from . import unified_store_reader as _reader
from . import unified_store_types as _types

STORE_FORMAT = _types.STORE_FORMAT
STORE_FORMAT_MINOR = _types.STORE_FORMAT_MINOR
UnifiedStoreError = _types.UnifiedStoreError
UnifiedStoreReader = _reader.UnifiedStoreReader
UnifiedStoreRequiresNewer = _types.UnifiedStoreRequiresNewer
UnifiedStoreUpdate = _reader.UnifiedStoreUpdate
UnifiedStoreView = _types.UnifiedStoreView
current_fingerprint = _loading.current_fingerprint
load_unified_store = _loading.load_unified_store

__all__ = [
    "STORE_FORMAT",
    "STORE_FORMAT_MINOR",
    "UnifiedStoreError",
    "UnifiedStoreReader",
    "UnifiedStoreRequiresNewer",
    "UnifiedStoreUpdate",
    "UnifiedStoreView",
    "current_fingerprint",
    "load_unified_store",
]
