"""A registered vibe target with a trusted, ready terminal for send tests."""

from __future__ import annotations

from tests.support.paths import fixture

SCREENS = fixture("screens")


def trust_target(daemon, participant_id: str, *, provenance: str = "operator") -> None:
    participant = daemon.registry.get(participant_id)
    participant.session_id = f"session-{participant_id}"
    participant.session_correlation = provenance
    daemon.store.upsert_participant(participant)


def ready_screen(daemon, terminal_id: str, harness: str) -> None:
    """Provider sends require a proven ready composer on screen."""
    daemon._test_terminal_provider.screens[terminal_id] = (
        SCREENS / f"{harness}_idle.txt"
    ).read_text()


async def send_target(client, daemon, *, pane: str = "%1", harness: str = "vibe"):
    del pane
    target = await client.call("hello", harness=harness, pane=None, cwd="/tmp")
    trust_target(daemon, target["id"])
    terminal_id = daemon._test_terminal_provider.bind(daemon, target["id"], command=harness)
    ready_screen(daemon, terminal_id, harness)
    return target
