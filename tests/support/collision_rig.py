"""Sessions, transcripts, trusted pins and staged sources for the transcript collision tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.support.shipped import VibeHarness
from theater.daemon.observer import Observer, QuietClock, TurnAccumulator
from theater.daemon.registry import Registry


def trust_pin(registry: Registry, participant, transcript: Path, *, provenance: str = "operator"):
    participant.session_id = "a00bff57-1111-2222-3333"
    participant.session_correlation = provenance
    participant.transcript_location = str(transcript)
    participant.transcript_domain = str(transcript.parents[1].resolve())
    registry.store.upsert_participant(participant)
    return registry.get(participant.id)


async def accept_bound_source(observer: Observer, harness: VibeHarness, participant):
    source = harness.observer.open_source_for(
        participant_id=participant.id,
        cwd=participant.cwd,
        session_id=participant.session_id,
        session_provenance=participant.session_correlation,
        known_location=participant.transcript_location,
    )
    batch = await source.read()
    assert batch.attached is not None
    assert observer._accept_attachment(participant.id, source, batch)
    observer._apply(participant.id, batch, QuietClock(), TurnAccumulator())
    return source


def make_session(root: Path, short: str, cwd: str, *, text: str = "hello") -> Path:
    """Create a vibe session directory with meta.json and messages.jsonl."""
    d = root / f"session_20260816_191459_{short}"
    d.mkdir(parents=True)
    (d / "meta.json").write_text(
        json.dumps(
            {
                "session_id": f"{short}-1111-2222-3333",
                "environment": {"working_directory": cwd},
            }
        )
    )
    messages = d / "messages.jsonl"
    messages.write_text(json.dumps({"role": "assistant", "content": text}) + "\n")
    return messages


def write_claude_transcript(path: Path, *, sid: str, cwd: str, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                json.dumps({"type": "system", "cwd": cwd}),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "id": f"message-{sid}",
                            "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": text}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def vibe_tree(tmp_path):
    """A Vibe log root with two sessions in the same cwd."""
    root = tmp_path / "logs" / "session"
    root.mkdir(parents=True)
    project = tmp_path / "project"
    project.mkdir()

    a = make_session(root, "a00bff57", str(project), text="agent A says hello")
    b = make_session(root, "aa5d2d32", str(project), text="agent B says world")

    return {"root": root, "project": project, "transcript_a": a, "transcript_b": b}


@pytest.fixture
def collision_registry(store):
    return Registry(store)


@pytest.fixture
async def collision_observer(collision_registry, vibe_tree):
    """An observer wound tight enough to finish inside a test."""
    observer = Observer(
        collision_registry,
        {"vibe": VibeHarness(root=vibe_tree["root"])},
        poll=0.01,
        search=0.01,
        sync=0.01,
    )
    observer.start()
    yield observer
    await observer.aclose()
