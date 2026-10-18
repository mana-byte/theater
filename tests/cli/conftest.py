"""Fixtures shared by the CLI command tests."""

from __future__ import annotations

import pytest

from theater.cli.commands import bus as bus_mod
from theater.cli.commands import controls as controls_mod
from theater.cli.commands import identity as identity_mod
from theater.cli.commands import introspection as introspection_mod
from theater.cli.commands import maintenance as maintenance_mod
from theater.cli.commands import management as management_mod
from theater.cli.commands import participants as participants_mod


@pytest.fixture
def answers(monkeypatch):
    """Replace call_sync with a canned-answer recorder."""
    state = {"replies": {}, "calls": []}

    def call_sync(method, **params):
        state["calls"].append((method, params))
        reply = state["replies"].get(method, [])
        if isinstance(reply, Exception):
            raise reply
        return reply

    # Patch call_sync in every command module that reads it.
    for mod in (
        bus_mod,
        controls_mod,
        identity_mod,
        introspection_mod,
        maintenance_mod,
        management_mod,
        participants_mod,
    ):
        monkeypatch.setattr(mod, "call_sync", call_sync)
    return state
