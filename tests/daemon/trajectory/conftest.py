"""Fixtures for the trajectory service tests."""

from __future__ import annotations

import pytest

from theater.daemon.trajectory import history as history_module


@pytest.fixture
def source_opener(monkeypatch):
    """Map participant ids to the fake sources the service opens for them."""
    sources: dict = {}

    def open_source(_observer, **kwargs):
        return sources[kwargs["participant_id"]]

    monkeypatch.setattr(history_module, "open_participant_source", open_source)
    return sources
