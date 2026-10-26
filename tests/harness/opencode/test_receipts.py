"""OpenCode generic receipts: validation, admission, staging and root-session switching."""

from __future__ import annotations

import asyncio
import io
import json
import sqlite3
from pathlib import Path

import pytest

from tests.support.opencode_recorder import SCHEMA
from tests.support.opencode_recorder import rec as shared_rec
from tests.support.shipped import OpenCodeHarness, OpenCodeObserver
from tests.support.waiting import wait_until
from theater import cli, paths
from theater.cli.commands import identity as identity_mod
from theater.client import DaemonClient
from theater.daemon.server import Daemon
from theater.harness.builtin.plugins.opencode.mcp import catalog_path
from theater.harness.builtin.plugins.opencode.native_plugin import plugin_dir
from theater.harness.contracts.trajectory import TrajectoryFact
from theater.harness.source import SourceContractError
from theater.protocol import RemoteError
from theater.provenance import TranscriptProvenance
from theater.trajectory import TrajectoryKind


def _database(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _root_session(conn: sqlite3.Connection, session_id: str, cwd: Path, created: int) -> None:
    conn.execute(
        "INSERT INTO session_v2 (id, parent_id, directory, time_created) VALUES (?, NULL, ?, ?)",
        (session_id, str(cwd.resolve()), created),
    )
    conn.commit()


def _attach(source):
    batch = asyncio.run(source.read())
    assert batch.attached is not None
    source.commit_attachment()
    return batch


async def _wait_for_exact_attachment(daemon: Daemon, participant_id: str, session_id: str) -> None:
    def attached() -> bool:
        participant = daemon.store.get_participant(participant_id)
        return (
            participant is not None
            and participant.session_id == session_id
            and participant.transcript_location == f"opencode://{session_id}"
        )

    await wait_until(attached, what=f"{participant_id} attaching opencode://{session_id}")


def test_launch_uses_a_core_owned_generic_receipt_token(
    tmp_path, monkeypatch, available_harness_binaries
):
    monkeypatch.setenv("THEATER_HOME", str(tmp_path / "theater-home"))
    config = tmp_path / "opencode.json"
    plan = OpenCodeHarness().plan_launch(
        participant_id="participant",
        prompt="",
        config_path=config,
        approval="yolo",
    )

    token_path = paths.participant_observation_dir("participant", "opencode") / "receipt-token"
    plugin = plugin_dir(config) / "server.js"
    source = plan.files[plugin]
    assert plan.receipt_token_path == token_path
    assert plan.receipt_token is None
    assert "transcript-receipt" in source
    assert '"--strict-exit"' in source
    assert '"--id"' in source
    assert "settings.participantID" in source
    assert "spawn(" in source
    assert 'stdio: ["pipe", "ignore", "ignore"]' in source
    assert '"retryDelays": [0, 100, 500, 2000]' in source
    assert "code === 0" in source
    assert 'event.type === "session.created" && !data.parentID' in source
    assert str(catalog_path("participant")) in source
    assert str(token_path) in source
    assert '"pluginID": "theater.opencode-session"' in source


@pytest.mark.parametrize(("strict", "expected"), [(False, 0), (True, 1)])
@pytest.mark.parametrize("failure", ["token_read", "parse", "rpc"])
def test_generic_receipt_cli_reports_failures_only_in_strict_mode(
    tmp_path, monkeypatch, failure, strict, expected
):
    token_file = tmp_path / ("missing-token" if failure == "token_read" else "token")
    if failure != "token_read":
        token_file.write_text("secret")
    argv = ["transcript-receipt", "--id", "participant", "--token-file", str(token_file)]
    if strict:
        argv.append("--strict-exit")
    args = cli._parser().parse_args(argv)
    if failure == "rpc":

        async def reject(*args, **kwargs):
            raise ConnectionError("daemon unavailable")

        monkeypatch.setattr(identity_mod, "_send_transcript_receipt", reject)
    stdin = "not-json" if failure == "parse" else json.dumps({"session_id": "ses-root"})
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))

    assert cli.cmd_transcript_receipt(args) == expected


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
    [
        pytest.param({}, id="missing"),
        pytest.param({"session_id": ""}, id="empty"),
        pytest.param({"session_id": " "}, id="blank"),
        pytest.param({"session_id": 1}, id="non_string"),
        pytest.param({"session_id": "root"}, id="bare_root"),
        pytest.param({"session_id": "ses-root\nother"}, id="newline"),
        pytest.param({"session_id": "ses-" + "x" * 256}, id="too_long"),
        pytest.param({"session_id": "ses-\ud800"}, id="surrogate"),
    ],
)
def test_validator_rejects_invalid_session_ids(tmp_path, payload):
    observer = OpenCodeObserver(db=tmp_path / "opencode.db")

    with pytest.raises(ValueError, match="session_id"):
        observer.validate_transcript_receipt(
            payload=payload,
            cwd=None,
            expected_session_id=None,
        )


def test_validator_rejects_locations_and_accepts_new_root_ids(tmp_path):
    observer = OpenCodeObserver(db=tmp_path / "opencode.db")

    with pytest.raises(ValueError, match="start with"):
        observer.validate_transcript_receipt(
            payload={"session_id": "opencode://ses-root"},
            cwd=None,
            expected_session_id=None,
        )
    candidate = observer.validate_transcript_receipt(
        payload={"session_id": "ses-root"},
        cwd=None,
        expected_session_id="ses-other",
    )
    assert candidate.location == "opencode://ses-root"
    assert candidate.session_id == "ses-root"


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
    fact = TrajectoryFact(kind=TrajectoryKind.ASSISTANT)
    source._trajectory_state["fact"] = (2, fact)

    assert (
        source.admit_exact_location(location="opencode://ses-one", session_id="ses-one")
        == "accepted"
    )
    assert source._cursor == 41
    assert source._roles == {"message": "assistant"}
    assert source._text == {"message": {"part": "answer"}}
    assert source._trajectory_state == {"fact": (2, fact)}
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


def test_repeated_staged_receipt_does_not_reset_state_or_extend_deadline(tmp_path):
    db = tmp_path / "opencode.db"
    conn = _database(db)
    source = OpenCodeObserver(db=db).open_source(cwd=str(tmp_path))

    assert (
        source.admit_exact_location(location="opencode://ses-target", session_id="ses-target")
        == "staged"
    )
    source._receipt_deadline = 123.0
    source._roles["message"] = "assistant"

    assert (
        source.admit_exact_location(location="opencode://ses-target", session_id="ses-target")
        == "staged"
    )
    assert source._receipt_deadline == 123.0
    assert source._roles == {"message": "assistant"}
    conn.close()


def test_staged_receipt_times_out_when_database_row_never_appears(tmp_path):
    db = tmp_path / "opencode.db"
    conn = _database(db)
    source = OpenCodeObserver(db=db).open_source(cwd=str(tmp_path))
    source.admit_exact_location(location="opencode://ses-missing", session_id="ses-missing")
    source._receipt_deadline = 0.0

    failed = asyncio.run(source.read())

    assert failed.error_code == "transcript_correlation_failed"
    assert "did not appear" in (failed.error or "")
    conn.close()


def test_staged_receipt_times_out_when_database_row_is_not_a_root(tmp_path):
    db = tmp_path / "opencode.db"
    conn = _database(db)
    conn.execute(
        "INSERT INTO session_v2 (id, parent_id, directory, time_created) VALUES (?, ?, ?, ?)",
        ("ses-child", "ses-parent", str(tmp_path.resolve()), 1000),
    )
    conn.commit()
    source = OpenCodeObserver(db=db).open_source(cwd=str(tmp_path))
    source.admit_exact_location(location="opencode://ses-child", session_id="ses-child")
    source._receipt_deadline = 0.0

    failed = asyncio.run(source.read())

    assert failed.error_code == "transcript_correlation_failed"
    assert "not a root session" in (failed.error or "")
    conn.close()


def test_receipt_marker_requires_a_generic_admission(tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    db = tmp_path / "opencode.db"
    conn = _database(db)
    _root_session(conn, "ses-one", cwd, 1000)
    correlation = tmp_path / "correlation"
    correlation.mkdir()
    plugin_dir(correlation / "participant.json").mkdir()
    source = OpenCodeObserver(db=db, correlation_dir=correlation).open_source_for(
        participant_id="participant",
        cwd=str(cwd),
    )

    assert asyncio.run(source.read()).waiting is True
    source._receipt_deadline = 0.0
    failed = asyncio.run(source.read())
    assert failed.error_code == "transcript_correlation_failed"
    conn.close()


def test_receipt_marker_waits_without_error_until_an_eligible_root_exists(tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    db = tmp_path / "opencode.db"
    conn = _database(db)
    _root_session(conn, "ses-too-old", cwd, 1000)
    correlation = tmp_path / "correlation"
    correlation.mkdir()
    plugin_dir(correlation / "participant.json").mkdir()
    source = OpenCodeObserver(db=db, correlation_dir=correlation).open_source_for(
        participant_id="participant",
        cwd=str(cwd),
        after=2.0,
    )
    source._receipt_deadline = 0.0

    waiting = asyncio.run(source.read())
    assert waiting.waiting is True
    assert waiting.error_code is None

    _root_session(conn, "ses-eligible", cwd, 3000)
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


def test_generic_receipt_switches_to_a_new_root_session(theater_home, tmp_path):
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
            participant = daemon.registry.register(
                harness="opencode",
                pane=None,
                cwd=str(cwd),
                claimed_id="opencode-participant",
            )
            daemon.store.set_receipt_token(participant.id, "secret")
            with pytest.raises(RemoteError, match="token is invalid"):
                await client.call(
                    "transcript.receipt",
                    id=participant.id,
                    token="wrong",
                    payload={"session_id": "ses-one"},
                )

            receipt = await client.call(
                "transcript.receipt",
                id=participant.id,
                token="secret",
                payload={"session_id": "ses-one"},
            )
            assert receipt["admission"] == "staged"
            staged = daemon.store.get_participant(participant.id)
            assert staged is not None
            assert staged.session_id is None
            assert staged.transcript_location is None

            _root_session(conn, "ses-one", cwd, 1000)
            await _wait_for_exact_attachment(daemon, participant.id, "ses-one")
            current = daemon.store.get_participant(participant.id)
            assert current is not None
            assert current.session_id == "ses-one"
            assert current.transcript_location == "opencode://ses-one"

            repeated = await client.call(
                "transcript.receipt",
                id=participant.id,
                token="secret",
                payload={"session_id": "ses-one"},
            )
            assert repeated["admission"] == "accepted"

            receipt = await client.call(
                "transcript.receipt",
                id=participant.id,
                token="secret",
                payload={"session_id": "ses-two"},
            )
            assert receipt["admission"] == "staged"
            current = daemon.store.get_participant(participant.id)
            assert current is not None
            assert current.session_id == "ses-one"
            assert current.transcript_location == "opencode://ses-one"

            _root_session(conn, "ses-two", cwd, 2000)
            await _wait_for_exact_attachment(daemon, participant.id, "ses-two")
            current = daemon.store.get_participant(participant.id)
            assert current is not None
            assert current.session_id == "ses-two"
            assert current.transcript_location == "opencode://ses-two"
        finally:
            await client.aclose()
            await daemon.aclose()
            conn.close()

    asyncio.run(exercise())


def test_source_rejects_a_mismatched_exact_location(tmp_path):
    source = OpenCodeObserver(db=tmp_path / "opencode.db").open_source(cwd=None)

    with pytest.raises(SourceContractError, match="location"):
        source.admit_exact_location(location="other://ses-one", session_id="ses-one")


rec = shared_rec  # the shared fixture


def test_generic_receipts_disambiguate_concurrent_sessions_in_the_same_cwd(rec, workdir):
    """Exact receipts disambiguate concurrent sessions sharing a cwd."""
    rec.conn.execute(
        "INSERT INTO session_v2 (id, parent_id, directory, time_created) "
        "VALUES ('ses_two', NULL, ?, 2000)",
        (str(workdir.resolve()),),
    )
    rec.conn.commit()
    observer = OpenCodeObserver(db=rec.path)
    source_a = observer.open_source_for(
        participant_id="participant_a",
        cwd=str(workdir),
        # Simulate a daemon restarting with the wrong stored binding.
        session_id="ses_two",
        after=0.0,
    )
    source_b = observer.open_source_for(
        participant_id="participant_b",
        cwd=str(workdir),
        session_id="ses_one",
        after=0.0,
    )

    assert (
        source_a.admit_exact_location(location="opencode://ses_one", session_id="ses_one")
        == "staged"
    )
    assert (
        source_b.admit_exact_location(location="opencode://ses_two", session_id="ses_two")
        == "staged"
    )
    assert asyncio.run(source_a.read()).attached.session_id == "ses_one"
    assert asyncio.run(source_b.read()).attached.session_id == "ses_two"


def test_a_receipt_capable_process_waits_for_its_receipt(rec, workdir, tmp_path):
    """Never fall back during the creation-event/receipt race window."""
    correlation = tmp_path / "mcp"
    correlation.mkdir()
    plugin_dir(correlation / "participant.json").mkdir()
    observer = OpenCodeObserver(db=rec.path, correlation_dir=correlation)

    source = observer.open_source_for(
        participant_id="participant",
        cwd=str(workdir),
        after=0.0,
    )
    batch = asyncio.run(source.read())

    assert batch.waiting is True
    assert batch.attached is None
    source._receipt_deadline = 0.0
    failed = asyncio.run(source.read())
    assert failed.error_code == "transcript_correlation_failed"
