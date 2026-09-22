from __future__ import annotations

import importlib

import pytest


def test_local_catalog_adapts_the_installed_registry_without_daemon_io(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = importlib.import_module("theater.frontend.local_catalog")
    discovered: list[bool] = []
    monkeypatch.setattr(
        catalog,
        "_configured_registry_rows",
        lambda: (
            discovered.append(True)
            or [
                {
                    "name": "claude",
                    "icon": "◆",
                    "binary": "claude",
                    "binaries": ["claude-code"],
                    "installed": True,
                    "approvals": ["manual", "edits"],
                    "error": None,
                },
                {
                    "name": "missing",
                    "icon": "?",
                    "binary": "missing",
                    "binaries": [],
                    "installed": False,
                    "approvals": ["manual"],
                    "error": None,
                },
            ]
        ),
    )

    available, missing = catalog.local_harness_catalog()

    assert discovered == [True]
    assert (available.name, available.icon, available.launch_available) == (
        "claude",
        "◆",
        True,
    )
    assert available.approvals == ("manual", "edits")
    assert missing.launch_available is False
    assert missing.reason == "not_installed"
