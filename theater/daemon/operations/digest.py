"""RFC 8785 request-payload identities for public idempotency."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any, cast

import rfc8785


def request_digest(method: str, params: Mapping[str, object]) -> str:
    """Hash only the canonical method and validated domain parameters."""
    if not isinstance(method, str) or not method:
        raise ValueError("idempotent method must be a non-empty string")
    if not isinstance(params, Mapping) or any(not isinstance(key, str) for key in params):
        raise TypeError("idempotent params must be an object with string keys")
    canonical = rfc8785.dumps(cast(Any, {"method": method, "params": dict(params)}))
    return hashlib.sha256(canonical).hexdigest()


__all__ = ["request_digest"]
