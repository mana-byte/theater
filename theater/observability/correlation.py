"""Content-free call correlation when no tracing collector is configured."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar

CALL_ID_KEY = "theater_call_id"
_CALL_ID = ContextVar[str | None]("theater_call_id", default=None)
_VALID_ID = re.compile(r"[0-9a-f]{32}")


def current_call_id() -> str | None:
    return _CALL_ID.get()


def extract_call_id(carrier: object) -> str | None:
    """Accept only bounded diagnostic IDs; they convey no authority."""
    value = carrier.get(CALL_ID_KEY) if isinstance(carrier, Mapping) else None
    return value if isinstance(value, str) and _VALID_ID.fullmatch(value) else None


@contextmanager
def call_scope() -> Iterator[str]:
    call_id = uuid.uuid4().hex
    token = _CALL_ID.set(call_id)
    try:
        yield call_id
    finally:
        _CALL_ID.reset(token)
