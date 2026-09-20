"""Small Régie-owned timing context used by presentation hot paths."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

_TRACER = trace.get_tracer("regie")


@contextmanager
def span(operation: str, **attributes: object) -> Iterator[dict[str, object]]:
    """Create one standard OpenTelemetry span and accept late-bound attributes."""
    fields = dict(attributes)
    with _TRACER.start_as_current_span(operation) as active:
        try:
            yield fields
        except Exception as exc:
            active.record_exception(exc)
            active.set_status(Status(StatusCode.ERROR, str(exc)))
            raise
        finally:
            for name, value in fields.items():
                if isinstance(value, str | bool | int | float):
                    active.set_attribute(name, value)


REGIE_TRAJECTORY_DETAIL_PROJECT = "regie.trajectory.detail.project"
REGIE_TRAJECTORY_DETAIL_RENDER = "regie.trajectory.detail.render"

__all__ = ["REGIE_TRAJECTORY_DETAIL_PROJECT", "REGIE_TRAJECTORY_DETAIL_RENDER", "span"]
