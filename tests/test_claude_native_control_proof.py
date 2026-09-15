"""Claude Code native-control Phase 0 proof: the messaging-socket gates.

The shipped manifest stays fail-closed until a stock binary >= 2.1.248 passes
every executable gate in docs/native-interaction/claude.md. The pinned fixture
records the honest no-go; a live proof writes a fresh record outside the
fixture tree, and copying that into the fixture stays a separate reviewed
action. The FakeInbox tests below are harness self-tests on a model receiver,
never stock evidence.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import pytest

from tests.native import claude_messaging_client as harness
from theater.harness.builtin.plugins.claude.manifest import MANIFEST, manifest_for_root
from theater.harness.contracts.manifest import InterruptPlan

FIXTURES = Path(__file__).parent / "fixtures" / "claude_native_control"
RECORD = FIXTURES / "messaging_conformance.json"
PROOF_ENV = harness.PROOF_ENV
PROBE_ENABLED = os.environ.get(PROOF_ENV) == "1" and bool(shutil.which("claude"))
PINNED_INSTALLED = "2.1.272"
PINNED_STATIC = "2.1.248"
PINNED_STATIC_SHA = "8c9482ad0510ad5e3c88f0ebe6f035ec148f73e2"
PINNED_BINARY_SHA = "195e24e8e1f9bf46f1eaee72d434a33e18f9f5796f29a6348a00d16c5f8aee75"
PINNED_RESULT = (
    "no-go: required subcases unclassified: failure_taxonomy; "
    "failing gates: auth, session_rotation, stale_credentials"
)
GATE_NAMES = harness.REQUIRED_GATE_NAMES
PROVEN_IDLE_SEND = {
    "frameProven": True,
    "admissionReply": "transcript-user-record",
    "turnIdField": "uuid",
    "duplicateSemantics": "duplicate-suppressed",
}
PINNED_GATE_STATUSES = {
    "admission_fact": "pass",
    "auth": "fail",
    "busy_behavior": "pass",
    "duplicate_msg_id": "pass",
    "failure_taxonomy": "no-go",
    "idle_submission": "pass",
    "own_child_delivery": "pass",
    "session_rotation": "fail",
    "session_start": "pass",
    "stale_credentials": "fail",
    "turn_mapping": "pass",
}


def _facts(version: tuple[int, int, int]) -> harness.BinaryFacts:
    return harness.BinaryFacts(
        resolved="/usr/local/bin/claude",
        version=version,
        sha256="0" * 64,
        size_bytes=1234,
    )


def _outcome() -> harness.MainGatesOutcome:
    return harness.MainGatesOutcome(
        idle_ok=True,
        turn_mapped=True,
        duplicate_semantics="duplicate-suppressed",
        stale_socket="/gone/inbox.sock",
        stale_token="stale-token",
    )


def test_shipped_manifest_stays_fail_closed() -> None:
    assert MANIFEST.runtime is None
    assert MANIFEST.controls.interrupt == InterruptPlan(keys=("Escape",))
    for_root = manifest_for_root(Path("/anywhere"))
    assert for_root.runtime is None
    assert for_root.controls.interrupt == InterruptPlan(keys=("Escape",))


def test_pinned_fixture_records_the_honest_no_go() -> None:
    record = json.loads(RECORD.read_text())
    assert record["schema"] == 2
    assert record["inspected"] == {
        "binary": "<resolved claude executable>",
        "date": "2026-09-15",
        "platform": "darwin-arm64",
        "sha256": PINNED_BINARY_SHA,
        "sizeBytes": 210702192,
        "version": PINNED_INSTALLED,
    }
    assert record["staticInspection"]["version"] == PINNED_STATIC
    assert record["staticInspection"]["gitSha"] == PINNED_STATIC_SHA
    assert record["staticInspection"]["sameMachineFloor"] == PINNED_STATIC
    assert set(record["gates"]) == set(GATE_NAMES)
    assert {
        name: gate["status"] for name, gate in record["gates"].items()
    } == PINNED_GATE_STATUSES
    assert record["gates"]["auth"]["evidence"]["failing"] == [
        "noFrameAcceptedBeforeAuth",
        "rejectedCredentials",
    ]
    assert record["gates"]["busy_behavior"]["evidence"]["classification"] == (
        "editor-buffered-not-submitted"
    )
    assert record["gates"]["session_rotation"]["evidence"]["failing"] == [
        "staleTokenCannotMutateResumed"
    ]
    assert record["gates"]["stale_credentials"]["evidence"][
        "deliveredWithStaleToken"
    ]
    assert record["idleSend"] == PROVEN_IDLE_SEND
    assert record["busySend"] == "not-adopted"
    assert record["result"] == PINNED_RESULT


def test_harness_pins_the_documented_wire_facts() -> None:
    assert harness.SAME_MACHINE_FLOOR == (2, 1, 248)
    assert harness.MAX_LINE_CHARS == 1_048_576
    assert harness.FIRST_LINE_DEADLINE_SECONDS == 30.0
    assert _facts((2, 1, 248)).qualified
    assert not _facts((2, 1, 224)).qualified
    assert _facts((2, 1, 248)).version_text == "2.1.248"


def test_user_frame_rejects_undocumented_shapes() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        harness.user_frame("", msg_id="m1")
    with pytest.raises(ValueError, match="charset"):
        harness.user_frame("hello", msg_id="bad id")
    with pytest.raises(ValueError, match="priority"):
        harness.user_frame("hello", msg_id="m1", priority="whenever")
    frame = harness.user_frame("hello", msg_id="m1", session_id="s1")
    assert frame["type"] == "user"
    assert frame["message"] == {"role": "user", "content": "hello"}
    assert "priority" not in frame, "the documented default must never be invented"


def test_redaction_removes_tokens_before_any_fixture_write() -> None:
    token = "tok-do-not-record"
    frame = {"type": "auth", "token": token, "nested": [token]}
    cleaned = harness.redact(frame, (token,))
    assert cleaned == {"type": "auth", "token": "<redacted>", "nested": ["<redacted>"]}
    assert token not in json.dumps(cleaned)
    with pytest.raises(harness.ConformanceError, match="leak"):
        harness.assert_no_secrets(f"still carries {token}", (token,))
    harness.assert_no_secrets("clean text", (token,))


def test_gate_from_subcases_never_passes_on_unclassified_subcases() -> None:
    ok = harness.SubcaseResult("classified", classified=True, ok=True)
    bad = harness.SubcaseResult("classified", classified=True, ok=False)
    unclassified = harness.SubcaseResult("not-inducible", classified=False, ok=False)
    assert harness._gate_from_subcases("g", {"a": ok, "b": ok}).status == "pass"
    gate = harness._gate_from_subcases("g", {"a": ok, "b": bad})
    assert gate.status == "fail"
    assert gate.evidence["failing"] == ["b"]
    gate = harness._gate_from_subcases("g", {"a": ok, "b": unclassified})
    assert gate.status == "no-go"
    assert gate.evidence["unclassified"] == ["b"]


@pytest.mark.parametrize(
    ("mid_index", "marker_in_editor", "expected"),
    [
        (2, False, "consumed-between-tool-calls"),
        (4, False, "submitted-after-turn"),
        (None, True, "editor-buffered-not-submitted"),
        (None, False, "unclassified"),
    ],
)
def test_busy_behavior_classification(
    mid_index: int | None, marker_in_editor: bool, expected: str
) -> None:
    assert (
        harness._classify_busy_behavior(
            long_index=1,
            mid_index=mid_index,
            final_index=3,
            marker_in_editor=marker_in_editor,
        )
        == expected
    )


def test_busy_record_indices_use_terminal_stop_reason() -> None:
    records = [
        {"type": "user", "message": {"content": "long-marker"}},
        {
            "type": "assistant",
            "message": {"content": [], "stop_reason": "tool_use"},
        },
        {"type": "user", "message": {"content": "mid-marker"}},
        {
            "type": "assistant",
            "message": {"content": "different final text", "stop_reason": "end_turn"},
        },
    ]
    assert harness._busy_record_indices(records, "long-marker", "mid-marker") == (0, 2, 3)


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        (1, 1, "duplicate-suppressed"),
        (1, 2, "duplicate-created-second-input"),
        (1, 3, "unclassified-2"),
    ],
)
def test_duplicate_semantics(before: int, after: int, expected: str) -> None:
    assert harness._duplicate_semantics(before, after) == expected


def test_auth_held_receipts_are_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        harness,
        "_send_and_listen",
        lambda *args, **kwargs: ([{"status": "held"}], None),
    )
    monkeypatch.setattr(harness, "_marker_absent_through", lambda *args, **kwargs: True)
    bad_auth = harness._auth_rejected_credentials(object())  # type: ignore[arg-type]
    pre_auth = harness._auth_frame_before_auth(object())  # type: ignore[arg-type]
    assert bad_auth.classified and not bad_auth.ok
    assert pre_auth.classified and not pre_auth.ok


def test_session_mismatch_uses_a_mismatched_id(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str | None] = {}

    class Session:
        token = "token"
        live_session_id = "live-session"

    def send(*args, **kwargs):
        captured["session_id"] = kwargs.get("session_id")
        return [], None

    monkeypatch.setattr(harness, "_send_and_listen", send)
    monkeypatch.setattr(harness, "_marker_absent_through", lambda *args, **kwargs: True)
    result = harness._taxonomy_session_mismatch(Session())  # type: ignore[arg-type]
    assert captured["session_id"] != Session.live_session_id
    assert result["delivered"] is False


def test_assembled_record_cannot_claim_pass_without_all_gates() -> None:
    facts = _facts((2, 1, 248))
    passing = [harness.GateResult(name, "pass", {}) for name in GATE_NAMES]
    record: dict = harness.assemble_record(facts, passing, _outcome())
    assert record["result"] == (
        "pass: idle send only (claude 2.1.248; wire facts statically inspected at 2.1.248)"
    )
    assert record["inspected"]["version"] == "2.1.248"
    assert record["inspected"]["sha256"] == facts.sha256
    assert record["inspected"]["binary"] == "<resolved claude executable>"
    assert record["idleSend"]["frameProven"] is True
    failing = [
        harness.GateResult(name, "fail" if name == "busy_behavior" else "pass", {})
        for name in GATE_NAMES
    ]
    failing_record: dict = harness.assemble_record(facts, failing, _outcome())
    assert failing_record["result"].startswith("fail:")
    mixed = [
        harness.GateResult(name, "no-go" if name == "failure_taxonomy" else "pass", {})
        for name in GATE_NAMES
    ]
    nogo_record: dict = harness.assemble_record(facts, mixed, _outcome())
    assert nogo_record["result"] == (
        "no-go: required subcases unclassified: failure_taxonomy"
    )
    assert nogo_record["gates"]["failure_taxonomy"]["status"] == "no-go"
    combined = [
        harness.GateResult(
            name,
            (
                "no-go"
                if name == "failure_taxonomy"
                else "fail"
                if name in {"auth", "session_rotation", "stale_credentials"}
                else "pass"
            ),
            {},
        )
        for name in GATE_NAMES
    ]
    assert harness.assemble_record(facts, combined, _outcome())["result"] == PINNED_RESULT
    missing_record = harness.assemble_record(facts, passing[:-1], _outcome())
    assert str(missing_record["result"]).startswith("no-go: invalid gate set")
    duplicate_record = harness.assemble_record(facts, [*passing, passing[0]], _outcome())
    assert str(duplicate_record["result"]).startswith("no-go: invalid gate set")


def test_record_write_is_atomic_and_redacts_before_replacing(tmp_path: Path) -> None:
    out = tmp_path / "fresh" / "record.json"
    record = harness.assemble_record(_facts((2, 1, 248)), [], _outcome())
    record["leak"] = "tok-do-not-record"
    harness.write_record_atomic(out, record, ("tok-do-not-record",))
    written = json.loads(out.read_text())
    assert written["leak"] == "<redacted>"
    assert "tok-do-not-record" not in out.read_text()
    assert list(tmp_path.rglob("*.tmp-*")) == [], "no temp residue may survive the replace"
    with pytest.raises(harness.ConformanceError, match="fresh"):
        harness.write_record_atomic(out, record, ("tok-do-not-record",))
    assert written == json.loads(out.read_text())


def test_launch_argv_never_combines_session_id_and_resume(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    fresh = harness._launch_argv(
        "claude", settings_path=settings, session_id="new-session", resume=None
    )
    resumed = harness._launch_argv(
        "claude", settings_path=settings, session_id="unused", resume="old-session"
    )
    assert "--session-id=new-session" in fresh
    assert not any(arg.startswith("--resume=") for arg in fresh)
    assert "--resume=old-session" in resumed
    assert not any(arg.startswith("--session-id=") for arg in resumed)


def test_proof_session_preserves_transcript_for_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("{}\n")
    session = harness.ProofSession("claude", base_dir=tmp_path)
    session.facts = harness.SessionFacts(
        session_id="session",
        socket_path=str(tmp_path / "inbox.sock"),
        transcript_path=transcript,
        pid=123,
    )
    monkeypatch.setattr(session, "_tmux_alive", lambda: False)
    session.stop(remove_transcript=False)
    assert transcript.exists()
    session.stop()
    assert not transcript.exists()


def test_run_conformance_guards_its_output_path(tmp_path: Path) -> None:
    with pytest.raises(harness.ConformanceError, match="fixtures"):
        harness.run_conformance("claude", out_path=RECORD)
    stale = tmp_path / "stale.json"
    stale.write_text("{}")
    with pytest.raises(harness.ConformanceError, match="fresh"):
        harness.run_conformance("claude", out_path=stale)
    assert stale.read_text() == "{}", "a refused run must never touch the target"


def test_run_conformance_refuses_an_unqualified_binary(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(harness, "binary_facts", lambda path=None: _facts((2, 1, 220)))
    out = tmp_path / "never-written.json"
    with pytest.raises(harness.ConformanceError, match="same-machine floor"):
        harness.run_conformance("claude", out_path=out)
    assert not out.exists()


def _short_bind_dir() -> Path:
    """AF_UNIX bind paths must stay under the sun_path ceiling."""
    for base in (Path("/tmp"), Path(tempfile.gettempdir())):
        try:
            path = Path(tempfile.mkdtemp(prefix="theater-claude-", dir=base))
        except OSError:
            continue
        if len(str(path / "inbox.sock").encode()) < harness.SUN_PATH_LIMIT:
            return path
        shutil.rmtree(path, ignore_errors=True)
    pytest.fail("no directory short enough for an AF_UNIX bind path")
    raise AssertionError("unreachable")


class _InboxFixture:
    """One fake inbox plus a reply socket sharing its vetted directory."""

    def __init__(self, *, require_auth: bool = True) -> None:
        self.directory = _short_bind_dir()
        self.inbox = harness.FakeInbox(
            str(self.directory / "inbox.sock"),
            token="proof-token",
            session_id="proof-session",
            require_auth=require_auth,
        )
        self.inbox.start()
        self.replies = harness.ReplyListener(self.inbox.path)
        self.replies.start()

    def client(self) -> harness.MessagingClient:
        return harness.MessagingClient(self.inbox.path, token="proof-token")

    def close(self) -> None:
        self.inbox.stop()
        self.replies.stop()
        shutil.rmtree(self.directory, ignore_errors=True)


def test_selftest_client_auth_enables_held_delivery_with_receipt() -> None:
    fx = _InboxFixture()
    try:
        client = fx.client()
        client.connect()
        client.send(
            harness.user_frame(
                "hello",
                msg_id="m1",
                session_id="proof-session",
                from_addr=fx.replies.address,
            )
        )
        client.close()
        fx.inbox.serve_once()
        receipts = fx.replies.receipts()
        assert "auth-ok" in fx.inbox.events
        assert "held" in fx.inbox.events
        assert len(fx.inbox.frames) == 1
        assert receipts[0]["status"] == "held"
        assert receipts[0]["orig_msg_id"] == "m1"
    finally:
        fx.close()


def test_selftest_bad_auth_is_dropped_when_auth_is_required() -> None:
    fx = _InboxFixture(require_auth=True)
    try:
        client = harness.MessagingClient(fx.inbox.path, token="wrong-token")
        client.connect()
        fx.inbox.serve_once()
        assert "auth-bad" in fx.inbox.events
        assert not client.alive()
    finally:
        fx.close()


def test_selftest_blank_line_before_auth_is_dropped() -> None:
    fx = _InboxFixture(require_auth=True)
    try:
        client = harness.MessagingClient(fx.inbox.path)
        client.connect(auth_first=False)
        client.send("")
        fx.inbox.serve_once()
        assert "dropped-blank-before-auth" in fx.inbox.events
    finally:
        fx.close()


def test_selftest_session_mismatch_is_dropped_without_a_receipt() -> None:
    fx = _InboxFixture()
    try:
        client = fx.client()
        client.connect()
        client.send(
            harness.user_frame(
                "hello",
                msg_id="m2",
                session_id="another-session",
                from_addr=fx.replies.address,
            )
        )
        client.close()
        fx.inbox.serve_once()
        assert "dropped-session-id-mismatch" in fx.inbox.events
        assert fx.replies.receipts() == []
    finally:
        fx.close()


def test_selftest_empty_content_is_ignored() -> None:
    fx = _InboxFixture()
    try:
        client = fx.client()
        client.connect()
        client.send_raw(json.dumps({"type": "user", "message": {"role": "user", "content": ""}}))
        client.close()
        fx.inbox.serve_once()
        assert "ignored-empty-content" in fx.inbox.events
    finally:
        fx.close()


def test_selftest_oversized_line_destroys_the_connection() -> None:
    fx = _InboxFixture()
    try:
        client = fx.client()
        client.connect()
        serving = threading.Thread(target=fx.inbox.serve_once, daemon=True)
        serving.start()
        with contextlib.suppress(harness.ConformanceError):
            client.send_raw("x" * (harness.MAX_LINE_CHARS + 1))
        serving.join(timeout=10.0)
        assert not serving.is_alive()
        assert "dropped-oversized-line" in fx.inbox.events
        assert not client.alive()
    finally:
        fx.close()


def test_selftest_alive_probe_distinguishes_open_and_closed_peers() -> None:
    fx = _InboxFixture()
    try:
        client = fx.client()
        client.connect()
        client.send("")
        assert client.alive(), "the probe must not consume inbox bytes"
        fx.inbox.serve_once(timeout=0.5)
        assert not client.alive()
    finally:
        fx.close()


def test_selftest_hook_command_quotes_metacharacters_and_captures_privately() -> None:
    fx = _InboxFixture(require_auth=True)
    try:
        marker = 'mar\'k "er" $x (y); z'
        capture = fx.directory / "hook-capture.json"
        serving = threading.Thread(target=fx.inbox.serve_once, daemon=True)
        serving.start()
        payload = json.dumps({"session_id": "proof-session", "transcript_path": "/t"})
        subprocess.run(
            ["/bin/sh", "-c", harness._capture_hook_command(capture, marker)],
            input=payload,
            text=True,
            capture_output=True,
            env={
                **os.environ,
                "CLAUDE_CODE_MESSAGING_SOCKET": fx.inbox.path,
                "CLAUDE_CODE_MESSAGING_TOKEN": "proof-token",
            },
            timeout=10,
            check=True,
        )
        serving.join(timeout=10.0)
        assert not serving.is_alive()
        text = capture.read_text()
        assert f"SOCKET={fx.inbox.path}\n" in text
        assert "TOKEN=proof-token\n" in text
        assert payload in text
        assert not capture.with_name(f".{capture.name}.pending").exists()
        harness.verify_capture_artifact(capture)
        assert "auth-ok" in fx.inbox.events
        assert len(fx.inbox.frames) == 1
        assert fx.inbox.frames[0]["message"]["content"] == marker
        assert "held" in fx.inbox.events
    finally:
        fx.close()


def test_capture_privacy_invariants_are_enforced(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    good = private / "capture.json"
    good.write_text("{}")
    good.chmod(0o600)
    harness.verify_capture_artifact(good)
    loose = private / "loose.json"
    loose.write_text("{}")
    loose.chmod(0o644)
    with pytest.raises(harness.ConformanceError, match="0600"):
        harness.verify_capture_artifact(loose)
    link = private / "link.json"
    link.symlink_to(good)
    with pytest.raises(harness.ConformanceError, match="symlink"):
        harness.verify_capture_artifact(link)


@pytest.mark.skipif(not PROBE_ENABLED, reason=f"set {PROOF_ENV}=1 with a stock claude")
def test_live_conformance_writes_a_fresh_record_outside_the_fixture_tree(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    facts = harness.binary_facts()
    if facts is None or not facts.qualified:
        pytest.skip(f"no qualified stock binary at the {harness.SAME_MACHINE_FLOOR} floor")
        return
    before = RECORD.read_bytes()
    out = tmp_path_factory.mktemp("claude-proof") / "messaging_conformance.json"
    record = harness.run_conformance(facts.resolved, out_path=out)
    assert record["result"].startswith(harness.PASS_RESULT_PREFIX)
    assert out.exists()
    assert RECORD.read_bytes() == before, "fixture updates are a separate reviewed action"
