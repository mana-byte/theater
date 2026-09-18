"""Public control-action facade retained as a focused Régie controller module."""

from __future__ import annotations

from regie.controllers.actions import ActionRecord, OperationController
from theater.frontend import FrontendClient


class ControlController(OperationController):
    """Named controller for steer, queue, settings, and interrupt public operations."""

    def __init__(self, client: FrontendClient) -> None:
        super().__init__(client)


__all__ = ["ActionRecord", "ControlController"]
