"""Bus notifications never fire from an external transaction that has not committed."""

from __future__ import annotations

import pytest

from theater.daemon.persistence.store import Store


def test_external_transaction_cannot_notify_before_commit(tmp_path) -> None:
    store = Store(tmp_path / "external.db")
    heard: list[dict] = []
    store.register_bus_listener(heard.append)
    try:
        with pytest.raises(RuntimeError, match="external transaction"):  # noqa: SIM117
            with store.engine.begin() as connection:
                store.bus_append("agent.assistant", from_id="participant-a", connection=connection)

        assert heard == []
        assert store.bus_tail() == []
    finally:
        store.close()
