"""OpenCode generic transcript receipt coverage."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest
from shipped import OpenCodeHarness, OpenCodeObserver

from theater import paths
from theater.client import DaemonClient
from theater.daemon.server import Daemon
from theater.harness.source import SourceContractError
from theater.protocol import RemoteError
from theater.provenance import TranscriptProvenance

SCHEMA = """
CREATE TABLE session (
    id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT,
    time_created INTEGER, time_updated INTEGER
);
CREATE TABLE event (
    id INTEGER PRIMARY KEY AUTOINCREMENT, aggregate_id TEXT, seq INTEGER,
    type TEXT, data TEXT
);
CREATE TABLE message (
    id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER,
    time_updated INTEGER, data TEXT
);
CREATE TABLE part (
    id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT,
    time_created INTEGER, time_updated INTEGER, data TEXT
);
"""


def _database(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _root_session(conn: sqlite3.Connection, session_id: str, cwd: Path, created: int) -> None:
    conn.execute(
        "INSERT INTO session (id, parent_id, directory, time_created) VALUES (?, NULL, ?, ?)",
        (session_id, str(cwd.resolve()), created),
    )
    conn.commit()


def _attach(source):
    batch = asyncio.run(source.read())
    assert batch.attached is not None
    source.commit_attachment()
    return batch


def test_launch_uses_a_core_owned_generic_receipt_token(tmp_path, monkeypatch):
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    config = tmp_path / "opencode.json"
    plan = OpenCodeHarness().plan_launch(
        participant_id="participant",
        prompt="",
        config_path=config,
        approval="manual",
    )

    token_path = paths.observation_dir("opencode", "participant") / "receipt-token"
    plugin = config.with_suffix(".opencode.mjs")
    source = plan.files[plugin]
    assert plan.receipt_token_path == token_path
    assert plan.receipt_token is None
    assert "transcript-receipt" in source
    assert '"--id", participantID, "--token-file", tokenPath' in source
    assert "spawn(" in source
    assert 'stdio: ["pipe", "ignore", "ignore"]' in source
    assert 'event.type !== "session.created" || event.properties.info.parentID' in source
    assert str(token_path) in source
    assert ".opencode-session" not in source
    assert "await" not in source


def test_validator_accepts_a_root_id_before_its_database_row_exists(tmp_path):
    db = tmp_path / "opencode.db"
    observer = OpenCodeObserver(db=db)

    candidate = observer.validate_transcript_receipt(
        payload={"session_id": "ses-root"},
        cwd=str(tmp_path),
        expected_session_id=None,
    )

    assert candidate.location == "opencode://ses-root"
    assert candidate.session_id == "ses-root"
    assert candidate.domain == f"opencode://{db.resolve()}"


@pytest.mark.parametrize(
    "payload",
    ({}, {"session_id": ""}, {"session_id": " "}, {"session_id": 1}),
)
def test_validator_rejects_invalid_session_ids(tmp_path, payload):
    observer = OpenCodeObserver(db=tmp_path / "opencode.db")

    with pytest.raises(ValueError, match="session_id"):
        observer.validate_transcript_receipt(
            payload=payload,
            cwd=None,
            expected_session_id=None,
        )


def test_validator_rejects_locations_and_expected_id_mismatches(tmp_path):
    observer = OpenCodeObserver(db=tmp_path / "opencode.db")

    with pytest.raises(ValueError, match="native session id"):
        observer.validate_transcript_receipt(
            payload={"session_id": "opencode://ses-root"},
            cwd=None,
            expected_session_id=None,
        )
    with pytest.raises(ValueError, match="does not match expected"):
        observer.validate_transcript_receipt(
            payload={"session_id": "ses-root"},
            cwd=None,
            expected_session_id="ses-other",
        )


def test_exact_admission_waits_for_its_root_row_without_cwd_fallback(tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    db = tmp_path / "opencode.db"
    conn = _database(db)
    _root_session(conn, "ses-foreign", cwd, 1000)
    source = OpenCodeObserver(db=db).open_source(cwd=str(cwd))

    assert (
        source.admit_exact_location(location="opencode://ses-target", session_id="ses-target")
        == "staged"
    )
    waiting = asyncio.run(source.read())
    assert waiting.waiting is True
    assert waiting.attached is None

    _root_session(conn, "ses-target", cwd, 2000)
    attached = asyncio.run(source.read())
    assert attached.attached is not None
    assert attached.attached.session_id == "ses-target"
    assert attached.attached.correlation == str(TranscriptProvenance.EXACT)
    conn.close()


def test_same_session_receipt_upgrades_without_resetting_live_state(tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    db = tmp_path / "opencode.db"
    conn = _database(db)
    _root_session(conn, "ses-one", cwd, 1000)
    source = OpenCodeObserver(db=db).open_source(cwd=str(cwd))
    _attach(source)
    source._cursor = 41
    source._roles["message"] = "assistant"
    source._text["message"] = {"part": "answer"}
    source._trajectory_revisions["fact"] = 2

    assert (
        source.admit_exact_location(location="opencode://ses-one", session_id="ses-one")
        == "accepted"
    )
    assert source._cursor == 41
    assert source._roles == {"message": "assistant"}
    assert source._text == {"message": {"part": "answer"}}
    assert source._trajectory_revisions == {"fact": 2}
    assert source._session_provenance is TranscriptProvenance.EXACT
    conn.close()


def test_different_session_receipt_stages_only_the_exact_target(tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    db = tmp_path / "opencode.db"
    conn = _database(db)
    _root_session(conn, "ses-one", cwd, 1000)
    _root_session(conn, "ses-foreign", cwd, 3000)
    source = OpenCodeObserver(db=db).open_source(cwd=str(cwd))
    _attach(source)
    source._cursor = 17
    source._roles["message"] = "assistant"

    assert (
        source.admit_exact_location(location="opencode://ses-target", session_id="ses-target")
        == "staged"
    )
    assert source._session is None
    assert source._cursor == -1
    assert source._roles == {}
    assert asyncio.run(source.read()).waiting is True

    _root_session(conn, "ses-target", cwd, 2000)
    attached = asyncio.run(source.read())
    assert attached.attached is not None
    assert attached.attached.session_id == "ses-target"
    conn.close()


def test_receipt_marker_requires_a_generic_admission(tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    db = tmp_path / "opencode.db"
    conn = _database(db)
    _root_session(conn, "ses-one", cwd, 1000)
    correlation = tmp_path / "correlation"
    correlation.mkdir()
    (correlation / "participant.opencode.mjs").write_text("// marker\n")
    source = OpenCodeObserver(db=db, correlation_dir=correlation).open_source_for(
        participant_id="participant",
        cwd=str(cwd),
    )

    assert asyncio.run(source.read()).waiting is True
    source._receipt_deadline = 0.0
    failed = asyncio.run(source.read())
    assert failed.error_code == "transcript_correlation_failed"
    conn.close()


def test_observer_without_a_marker_keeps_heuristic_discovery(tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    db = tmp_path / "opencode.db"
    conn = _database(db)
    _root_session(conn, "ses-one", cwd, 1000)
    source = OpenCodeObserver(db=db, correlation_dir=tmp_path / "correlation").open_source_for(
        participant_id="participant",
        cwd=str(cwd),
    )

    attached = asyncio.run(source.read())
    assert attached.attached is not None
    assert attached.attached.session_id == "ses-one"
    assert attached.attached.correlation == str(TranscriptProvenance.HEURISTIC)
    conn.close()


def test_generic_receipt_authenticates_and_persists_after_exact_attachment(theater_home, tmp_path):
    async def exercise() -> None:
        cwd = tmp_path / "work"
        cwd.mkdir()
        db = tmp_path / "opencode.db"
        conn = _database(db)
        daemon = Daemon(harnesses={"opencode": OpenCodeHarness(db=db)})
        client = DaemonClient(autostart=False)
        try:
            await daemon.start()
            await client.connect()
            participant = daemon.registry.create_spawned(
                harness="opencode", cwd=str(cwd), pid="opencode-participant"
            )
            daemon.registry.attach_pane(participant.id, "%1", pane_pid=10001)
            daemon.store.set_receipt_token(participant.id, "secret")
            payload = {"session_id": "ses-target"}
            with pytest.raises(RemoteError, match="token is invalid"):
                await client.call(
                    "transcript.receipt",
                    id=participant.id,
                    token="wrong",
                    payload=payload,
                )

            receipt = await client.call(
                "transcript.receipt",
                id=participant.id,
                token="secret",
                payload=payload,
            )
            assert receipt["admission"] == "staged"
            staged = daemon.store.get_participant(participant.id)
            assert staged is not None
            assert staged.session_id is None
            assert staged.transcript_location is None

            _root_session(conn, "ses-target", cwd, 1000)
            deadline = asyncio.get_running_loop().time() + 3.0
            while asyncio.get_running_loop().time() < deadline:
                current = daemon.store.get_participant(participant.id)
                if current is not None and current.transcript_location == "opencode://ses-target":
                    break
                await asyncio.sleep(0.01)
            current = daemon.store.get_participant(participant.id)
            assert current is not None
            assert current.session_id == "ses-target"
            assert current.transcript_location == "opencode://ses-target"
        finally:
            await client.aclose()
            await daemon.aclose()
            conn.close()

    asyncio.run(exercise())


def test_source_rejects_a_mismatched_exact_location(tmp_path):
    source = OpenCodeObserver(db=tmp_path / "opencode.db").open_source(cwd=None)

    with pytest.raises(SourceContractError, match="location"):
        source.admit_exact_location(location="other://ses-one", session_id="ses-one")
