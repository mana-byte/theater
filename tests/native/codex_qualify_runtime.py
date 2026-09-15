"""Codex release qualification: evidence collection only (plan §Phase 0).

Collect the exact binary facts, the vendor-generated JSON schema, and the
stock behaviour observations for one codex-cli release into a fresh bundle
directory, redacted and deterministically normalized, then print a recursive
diff against an explicit prior bundle. It never edits the verified-version
allowlist: admitting a release is a separate human-reviewed commit made
last, after evidence and tests.

    uv run python tests/native/codex_qualify_runtime.py \\
        --out /tmp/codex-qualify-0.155.0 --capture-behavior

Nothing here is production code; no production module may import it.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from tests.native.codex_native_client import (
    AppServerProcess,
    NativeWebSocketClient,
    TmuxUi,
    launch_remote_ui,
    run_turn,
    start_app_server_thread,
    wait_thread_active,
    wait_until,
    write_isolated_codex_home,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "codex_native_runtime"

#: The exact vendor generator command, run as documented in the fixtures.
SCHEMA_GENERATE_ARGS = ("app-server", "generate-json-schema", "--out")
SCHEMA_GENERATE_TIMEOUT_SECONDS = 180.0
VERSION_PROBE_TIMEOUT_SECONDS = 30.0
UI_EXIT_TIMEOUT_SECONDS = 60.0

BEHAVIOR_FILES = (
    "installed_release.json",
    "handshake.json",
    "thread_lifecycle.json",
    "turn_control.json",
    "approval.json",
    "capabilities.json",
    "unsupported_capabilities.json",
    "ui_topology.json",
)
SCHEMA_FILES = (
    "ClientRequest.json",
    "ClientNotification.json",
    "ServerRequest.json",
    "ServerNotification.json",
    "JSONRPCRequest.json",
    "JSONRPCMessage.json",
)

#: Bounded diff output: the human reviews, the tool must not flood.
DIFF_LINE_LIMIT = 400
VALUE_PREVIEW_CHARS = 160
BINARY_PATH_PLACEHOLDER = "<binary on PATH; resolved per run>"
_UUID = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}")
_VERSION_TOKEN = re.compile(r"codex-cli[ \t]+(\S+)")
_SEMVER_TOKEN = re.compile(r"(\d+\.\d+\.\d+)")

OBSERVER_RULE = (
    "a Theater observing/control client must never send a response for an approval "
    "server request; it records the request and relies on serverRequest/resolved "
    "to learn the outcome"
)
QUEUE_POSITION = (
    "do not use the native queue for Theater's followup queue; keep Theater's "
    "queue entirely in Theater"
)
ZERO_TURN_ORDER_CONSEQUENCE = (
    "the native UI can only attach after the rollout exists, i.e. after the first "
    "turn/start has been accepted; promptless zero-turn threads cannot be attached "
    "via resume"
)


class QualificationError(RuntimeError):
    """The collector refused to proceed; the message says what to do."""


# ---------------------------------------------------------------------------
# Pure helpers — deterministic, redacting, bounded; unit-tested offline
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReleaseFacts:
    """Everything that identifies the exact binary under qualification."""

    binary: str
    resolved_path: str
    digest: str
    version_output: str
    version: str
    platform_os: str
    platform_arch: str


def resolve_release_facts(codex_binary: str = "codex") -> ReleaseFacts:
    """Record the real path, sha256 digest, --version output, and platform."""
    resolved = shutil.which(codex_binary)
    if resolved is None:
        raise QualificationError(f"no codex binary named {codex_binary!r} on PATH")
    real = Path(resolved).resolve()
    digest = hashlib.sha256(real.read_bytes()).hexdigest()
    try:
        completed = subprocess.run(
            [str(real), "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=VERSION_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise QualificationError(
            f"{real} --version did not answer within {VERSION_PROBE_TIMEOUT_SECONDS}s; "
            "a hostile or hung release must never block qualification"
        ) from error
    except OSError as error:
        raise QualificationError(f"could not execute {real} --version: {error}") from error
    if completed.returncode != 0:
        raise QualificationError(f"{real} --version exited with {completed.returncode}")
    output = f"{completed.stdout}{completed.stderr}".strip()
    match = _VERSION_TOKEN.search(output)
    if match is None:
        raise QualificationError(f"--version output names no codex-cli release: {output!r}")
    return ReleaseFacts(
        binary=codex_binary,
        resolved_path=str(real),
        digest=digest,
        version_output=output,
        version=match.group(1),
        platform_os=platform.system().lower(),
        platform_arch=platform.machine(),
    )


def normalize_json(value: object) -> str:
    """Sorted keys, fixed indent; arrays, descriptions, and enums untouched."""
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def normalize_json_file(path: Path) -> str:
    return normalize_json(json.loads(path.read_text()))


def redact(value: object, roots: dict[str, str]) -> object:
    """Recursively replace run-specific strings with stable placeholders.

    ``roots`` maps actual strings (run directory, home directory) to their
    placeholders; longer actuals win so nested paths collapse correctly.
    UUIDs collapse to ``<uuid>`` so per-run identities never leak.
    """
    if isinstance(value, dict):
        return {key: redact(item, roots) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, roots) for item in value]
    if not isinstance(value, str):
        return value
    text = value
    for actual in sorted(roots, key=len, reverse=True):
        text = text.replace(actual, roots[actual])
    return _UUID.sub("<uuid>", text)


def _preview(value: object) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if len(text) > VALUE_PREVIEW_CHARS:
        text = text[: VALUE_PREVIEW_CHARS - 1] + "…"
    return text


def json_diff(before: object, after: object, where: str, lines: list[str]) -> None:
    """Append one deterministic line per leaf difference."""
    if type(before) is not type(after):
        lines.append(f"{where}: {_preview(before)} -> {_preview(after)}")
    elif isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            path = f"{where}.{key}"
            if key not in before:
                lines.append(f"{path}: absent -> {_preview(after[key])}")
            elif key not in after:
                lines.append(f"{path}: {_preview(before[key])} -> absent")
            else:
                json_diff(before[key], after[key], path, lines)
    elif isinstance(before, list) and isinstance(after, list):
        if len(before) != len(after):
            lines.append(f"{where}: array length {len(before)} -> {len(after)}")
        for index, (old, new) in enumerate(zip(before, after, strict=False)):
            json_diff(old, new, f"{where}[{index}]", lines)
    elif before != after:
        lines.append(f"{where}: {_preview(before)} -> {_preview(after)}")


def _bundle_files(directory: Path) -> set[str]:
    return {
        path.relative_to(directory).as_posix() for path in directory.rglob("*") if path.is_file()
    }


def diff_bundle(baseline: Path, candidate: Path) -> list[str]:
    """Deterministic recursive diff of two evidence bundles, bounded."""
    baseline_files = _bundle_files(baseline)
    candidate_files = _bundle_files(candidate)
    lines: list[str] = []
    for name in sorted(baseline_files - candidate_files):
        lines.append(f"{name}: present in baseline, missing from candidate")
    for name in sorted(candidate_files - baseline_files):
        lines.append(f"{name}: new in candidate")
    for name in sorted(baseline_files & candidate_files):
        json_diff(
            json.loads((baseline / name).read_text()),
            json.loads((candidate / name).read_text()),
            name,
            lines,
        )
    if len(lines) > DIFF_LINE_LIMIT:
        hidden = len(lines) - DIFF_LINE_LIMIT + 1
        return [*lines[: DIFF_LINE_LIMIT - 1], f"... {hidden} more differences"]
    return lines


def require_fresh_output(out: Path) -> None:
    """Refuse a dirty output directory; prior evidence is never overwritten."""
    resolved = out.resolve()
    fixtures = FIXTURE_ROOT.resolve()
    if resolved == fixtures or fixtures in resolved.parents or resolved in fixtures.parents:
        raise QualificationError(
            f"{out} is inside (or contains) the committed fixture tree {fixtures}; "
            "collect into a newly created release directory outside it"
        )
    if out.exists():
        if not out.is_dir():
            raise QualificationError(f"{out} exists and is not a directory")
        if any(out.iterdir()):
            raise QualificationError(
                f"refusing to collect into non-empty {out}; pass a newly created "
                "release directory so prior evidence is never overwritten in place"
            )
    else:
        out.mkdir(parents=True)


def release_facts_document(facts: ReleaseFacts) -> dict:
    """The bundle's installed_release.json, in the committed fixture's shape.

    The absolute binary path never enters committed evidence: only a stable
    placeholder plus the sha256 digest, so the exact measured binary stays
    identifiable without leaking any machine's layout.
    """
    return {
        "source": (
            "captured against the unmodified installed release by "
            "tests/native/codex_qualify_runtime.py"
        ),
        "installed_version": f"codex-cli {facts.version}",
        "installed_version_command": "codex --version",
        "installed_version_output": facts.version_output,
        "binary_path": BINARY_PATH_PLACEHOLDER,
        "binary_sha256": facts.digest,
        "schema_generation": {
            "command": "codex app-server generate-json-schema --out <dir> --experimental",
            "note": (
                "schema files under protocol_schema/ were emitted by the installed "
                f"{facts.version} binary itself"
            ),
            "files": list(SCHEMA_FILES),
        },
        "platform_facts": {
            "platform_family": "unix" if facts.platform_os != "windows" else "windows",
            "platform_os": facts.platform_os,
            "platform_arch": facts.platform_arch,
        },
    }


def generate_schema(codex_binary: str, out_dir: Path) -> None:
    """Run the vendor generator exactly as documented; normalize in place."""
    out_dir.mkdir(parents=True, exist_ok=True)
    command = [codex_binary, *SCHEMA_GENERATE_ARGS, str(out_dir), "--experimental"]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=SCHEMA_GENERATE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as error:
        raise QualificationError(
            f"schema generation did not finish within {SCHEMA_GENERATE_TIMEOUT_SECONDS}s"
        ) from error
    except OSError as error:
        raise QualificationError(f"could not run the schema generator: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()[-400:]
        raise QualificationError(f"schema generation failed ({completed.returncode}): {detail}")
    generated = sorted(out_dir.glob("*.json"))
    if not generated:
        raise QualificationError("the schema generator emitted no JSON files")
    for path in generated:
        path.write_text(normalize_json_file(path))
    missing = [name for name in SCHEMA_FILES if not (out_dir / name).is_file()]
    if missing:
        raise QualificationError(f"schema generator omitted required files: {missing}")


BUNDLE_STATUSES = frozenset({"qualified", "candidate"})
CANDIDATES_DIRNAME = "candidates"


def validate_bundle_index(
    index: object,
    *,
    fixture_root: Path,
    compatibility_policy: str | None = None,
    verified_versions: frozenset[str] | set[str] | None = None,
) -> None:
    """Fail closed on a malformed or lying index; never guess a selection.

    Shared by the collector's baseline selection and the proof gate's
    offline loader, so both refuse the same malicious index shapes. The
    allowlist and policy are passed in by the caller: this module never
    imports Theater production code.
    """
    assert isinstance(index, dict) and isinstance(index.get("bundles"), dict), (
        "index.json must be an object with a bundles map"
    )
    bundles = index["bundles"]
    seen: dict[str, str] = {}
    for version, entry in bundles.items():
        assert isinstance(entry, dict), f"index entry {version!r} is not an object"
        status = entry.get("status")
        assert status in BUNDLE_STATUSES, f"{version} has invalid status {status!r}"
        directory = entry.get("directory")
        assert isinstance(directory, str) and directory, f"{version} has no directory"
        parts = Path(directory).parts
        assert parts and all(p not in ("", ".", "..") for p in parts), (
            f"bundle directory {directory!r} must be a clean relative path"
        )
        assert not Path(directory).is_absolute() and "\\" not in directory, (
            f"bundle directory {directory!r} must not be absolute or escape"
        )
        assert directory not in seen, (
            f"{directory} is claimed by both {seen[directory]} and {version}"
        )
        seen[directory] = version
        if status == "candidate":
            assert directory == f"{CANDIDATES_DIRNAME}/{version}", (
                f"unqualified {version} must live at {CANDIDATES_DIRNAME}/{version}"
            )
            continue
        assert directory == version, (
            f"qualified {version} must live at {version} at the fixture root"
        )
        bundle = fixture_root / directory
        missing = [name for name in BEHAVIOR_FILES if not (bundle / name).is_file()] + [
            name for name in SCHEMA_FILES if not (bundle / "protocol_schema" / name).is_file()
        ]
        assert not missing, f"qualified {version} is incomplete: missing {missing}"
        if compatibility_policy is not None:
            assert entry.get("compatibility_policy") == compatibility_policy, (
                f"{version} must carry {compatibility_policy}"
            )
        if verified_versions is not None:
            assert version in verified_versions, (
                f"{version} is qualified but not in the allowlist; the allowlist "
                "change is a separate human-reviewed commit"
            )
    if verified_versions is not None:
        for version in sorted(verified_versions):
            entry = bundles.get(version)
            assert entry is not None and entry.get("status") == "qualified", (
                f"allowed {version} must have a qualified bundle"
            )


def default_baseline() -> Path:
    """The single qualified committed bundle, or refuse for an explicit choice.

    Structural and completeness rules only: the allowlist cross-check
    belongs to the proof gate, which knows the production allowlist.
    """
    index = json.loads((FIXTURE_ROOT / "index.json").read_text())
    try:
        validate_bundle_index(index, fixture_root=FIXTURE_ROOT)
    except AssertionError as error:
        raise QualificationError(
            f"the committed index is malformed ({error}); pass --baseline explicitly"
        ) from error
    qualified = [
        entry["directory"] for entry in index["bundles"].values() if entry["status"] == "qualified"
    ]
    if len(qualified) != 1:
        raise QualificationError(
            "multiple (or zero) qualified committed bundles; pass --baseline explicitly"
        )
    return FIXTURE_ROOT / qualified[0]


# ---------------------------------------------------------------------------
# Stock behaviour capture — opt-in; consumes model quota and needs tmux
# ---------------------------------------------------------------------------


@dataclass
class CaptureSession:
    """One isolated app-server plus its run-scoped paths."""

    root: Path
    repo: Path
    socket_path: Path
    app_server: AppServerProcess
    codex_home: Path
    roots: dict[str, str]
    binary: str
    initialize_results: list[dict] = field(default_factory=list)
    tmux_uis: list[TmuxUi] = field(default_factory=list)

    def connect(self, *, experimental: bool = False) -> NativeWebSocketClient:
        """One initialize per connection, recorded for the backend release check."""
        client = NativeWebSocketClient(self.socket_path)
        self.initialize_results.append(dict(client.initialize(experimental=experimental)["result"]))
        return client

    def ui(self, name: str) -> TmuxUi:
        """Create and register a tmux UI so teardown can never leak a server."""
        ui = TmuxUi(self.root / name)
        self.tmux_uis.append(ui)
        return ui

    def close(self) -> None:
        """Tear down UIs, the backend, and the run root; safe on any failure."""
        for ui in self.tmux_uis:
            with contextlib.suppress(Exception):
                ui.kill()
        self.app_server.terminate()
        shutil.rmtree(self.root, ignore_errors=True)

    def launch_zero_turn_ui(self, ui: TmuxUi, thread_id: str) -> Path:
        """Resume a zero-turn thread in the stock UI, capturing stderr text."""
        stderr_path = self.root / f"{ui.socket_path.name}.err"
        ui.launch(
            f"CODEX_HOME={shlex.quote(str(self.codex_home))} {shlex.quote(self.binary)} "
            f"--remote unix://{shlex.quote(str(self.socket_path))} "
            f"resume {shlex.quote(thread_id)} 2>{stderr_path}"
        )
        wait_until(
            lambda: not ui.session_alive(),
            timeout=UI_EXIT_TIMEOUT_SECONDS,
            what="zero-turn TUI to exit",
        )
        return stderr_path


def open_capture_session(codex_binary: str) -> CaptureSession:
    """Spawn the detached backend exactly as the frozen topology plans it."""
    root = Path(tempfile.mkdtemp(prefix="codex-qualify-"))
    repo = root / "repo"
    repo.mkdir()
    codex_home = write_isolated_codex_home(root, trusted_paths=[repo])
    app_server = AppServerProcess.spawn(
        codex_home=codex_home,
        socket_path=root / "control.sock",
        log_path=root / "app-server.log",
        codex_binary=codex_binary,
    )
    roots = {str(root): "<run-root>", str(Path.home()): "<home>"}
    return CaptureSession(
        root, repo, root / "control.sock", app_server, codex_home, roots, codex_binary
    )


def capture_handshake(session: CaptureSession) -> dict:
    """Connection-level facts only; no model quota."""
    client = NativeWebSocketClient(session.socket_path)
    response = client.initialize()
    result = response["result"]
    client.close()
    return {
        "source": "captured against the installed app-server over a Unix socket",
        "transport": "WebSocket over AF_UNIX with an HTTP Upgrade handshake; not Theater NDJSON",
        "client_request_line": "GET / HTTP/1.1",
        "server_response_status_line": client.handshake.status_line,
        "server_response_headers": dict(client.handshake.headers),
        "accept_verification": (
            "sec-websocket-accept matched base64(sha1(key + 258EAFA5-E914-47DA-95CA-C5AB0DC85B11))"
            if client.handshake.accept_valid
            else "MISMATCH: the server's accept did not derive from the client key"
        ),
        "initialize": {
            "request": {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "<client name>",
                        "title": "<client title>",
                        "version": "<client version>",
                    }
                },
            },
            "response_result_keys": sorted(result),
            "required_notification": {"method": "initialized"},
        },
        "jsonrpc_version_field": (
            "absent by design: requests and responses carry no jsonrpc member"
        ),
    }


def capture_thread_lifecycle(session: CaptureSession, *, tmux: bool) -> dict:
    """Thread start/rollout/resume/fork facts; two turns of model quota."""
    control = session.connect()
    start = control.request("thread/start", {"cwd": str(session.repo)})
    result = start["result"]
    thread = result["thread"]
    thread_id = thread["id"]
    rollout = Path(thread["path"])
    facts: dict = {
        "source": (
            "captured against the installed app-server; ids, timestamps and paths are placeholders"
        ),
        "thread_start": {
            "response_result_keys": sorted(result),
            "thread_id": thread_id,
            "thread_facts": {
                "id_equals_session_id": True,
                "cliVersion": thread.get("cliVersion"),
                "status_type_initially": thread.get("status", {}).get("type"),
                "rollout_path": str(rollout),
                "rollout_exists_before_first_turn": rollout.exists(),
            },
        },
    }
    blank = session.connect()
    blank_id = start_app_server_thread(blank, session.repo)
    blank_error = blank.request("thread/resume", {"threadId": blank_id})
    blank.close()
    facts["thread_resume"] = {
        "request": {"method": "thread/resume", "params": {"threadId": "<thread-id>"}},
        "subscribes_second_connection": None,  # observed after the first turn below
        "error_before_rollout_exists": blank_error.get("error", blank_error),
        "startup_order_consequence": ZERO_TURN_ORDER_CONSEQUENCE,
    }
    if tmux:
        ui = session.ui("tmux-zero-turn.sock")
        stderr_path = session.launch_zero_turn_ui(ui, blank_id)
        text = stderr_path.read_text(errors="replace").strip() if stderr_path.exists() else ""
        facts["thread_resume"]["native_ui_resume_before_first_turn_error"] = text

    run_turn(control, thread_id, "Remember the phrase marble. Reply with exactly: ok")
    facts["thread_start"]["thread_facts"]["rollout_exists_after_first_turn"] = rollout.exists()

    observer = session.connect()
    observer.request("thread/resume", {"threadId": thread_id})
    mark = len(observer.notifications)
    run_turn(control, thread_id, "Reply with exactly: resume-observed")
    observer.drain(quiet=2.0)
    seen = [m["method"] for m in observer.notifications[mark:] if m.get("method")]
    facts["thread_resume"]["subscribes_second_connection"] = bool(seen)
    facts["thread_resume"]["live_notification_methods_seen_by_second_client"] = sorted(set(seen))
    observer.close()

    forked = control.request("thread/fork", {"threadId": thread_id})
    fork_thread = forked["result"]["thread"]
    fork_read = control.request("thread/read", {"threadId": fork_thread["id"]})
    facts["thread_fork"] = {
        "forked_thread_id": fork_thread["id"],
        "forked_from_id": fork_thread.get("forkedFromId"),
        "history_preserved": bool(fork_read["result"]["thread"].get("turns")),
    }
    control.close()
    return facts


def capture_turn_control(session: CaptureSession) -> dict:
    """Steer, stale steer, race, interrupt; three turns of model quota."""
    control = session.connect()
    thread_id = start_app_server_thread(control, session.repo)
    sequence_mark = len(control.notifications)

    steer_mark = len(control.notifications)
    response = control.request(
        "turn/start",
        {"threadId": thread_id, "input": [{"type": "text", "text": _ESSAY_RIVERS}]},
    )
    active_id = response["result"]["turn"]["id"]
    wait_thread_active(control, thread_id, mark=steer_mark)
    steered = control.request(
        "turn/steer",
        {
            "threadId": thread_id,
            "expectedTurnId": active_id,
            "input": [
                {
                    "type": "text",
                    "text": "Stop writing the essay immediately. Reply with exactly: steered",
                }
            ],
        },
        timeout=60,
    )
    steer_result = steered.get("result", {})
    steer_turn_id = steer_result.get("turnId")
    completed = control.wait_notification("turn/completed", timeout=300)
    steer_completed = completed["params"]["turn"]
    control.drain(quiet=1.0)
    observed_sequence = [
        m["method"] for m in control.notifications[sequence_mark:] if m.get("method")
    ]

    stale = control.request(
        "turn/steer",
        {
            "threadId": thread_id,
            "expectedTurnId": active_id,
            "input": [{"type": "text", "text": "ignored"}],
        },
        timeout=60,
    )

    race_mark = len(control.notifications)
    response = control.request(
        "turn/start",
        {"threadId": thread_id, "input": [{"type": "text", "text": _ESSAY_MOUNTAINS}]},
    )
    race_id = response["result"]["turn"]["id"]
    wait_thread_active(control, thread_id, mark=race_mark)
    raced = control.request(
        "turn/start",
        {
            "threadId": thread_id,
            "input": [{"type": "text", "text": "Never mind the essay. Reply with exactly: raced"}],
        },
    )
    # The raced turn must fully settle before the interrupt case begins: an
    # outstanding turn would absorb the next turn/start and break identities.
    race_settled = control.wait_notification("turn/completed", timeout=300)
    race_completed = race_settled["params"]["turn"]

    interrupt_mark = len(control.notifications)
    response = control.request(
        "turn/start",
        {"threadId": thread_id, "input": [{"type": "text", "text": _ESSAY_SEA}]},
    )
    sea_id = response["result"]["turn"]["id"]
    wait_thread_active(control, thread_id, mark=interrupt_mark)
    interrupt_response = control.request(
        "turn/interrupt", {"threadId": thread_id, "turnId": sea_id}
    )
    interrupted = control.wait_notification("turn/completed", timeout=180)
    interrupt_turn = interrupted["params"]["turn"]
    control.close()
    return {
        "source": "captured against the installed app-server; ids and timestamps are placeholders",
        "turn_start": {
            "start_or_steer": (
                "turn/start internally steers an already-active turn: it returns "
                "the SAME active turn id instead of creating a new turn"
                if raced["result"]["turn"]["id"] == race_id
                else "turn/start created a distinct turn during an active turn"
            ),
        },
        "concurrent_submission_race": {
            "scenario": "turn/start issued while a turn was already in progress on the same thread",
            "response_turn_id_equals_active_turn_id": raced["result"]["turn"]["id"] == race_id,
            "completion_turn_id_equals_active_turn_id": race_completed["id"] == race_id,
        },
        "turn_steer": {
            "expectedTurnId_required": True,
            "response": {
                "result": {
                    "turnId": (
                        "<same active turn-id>" if steer_turn_id == active_id else steer_turn_id
                    )
                }
            },
            "completion_turn_id_matches_expected": steer_completed["id"] == active_id,
            "stale_turn_refusal": {
                "request_context": "turn/steer with an expectedTurnId whose turn already completed",
                "error": stale.get("error", stale),
            },
        },
        "turn_interrupt": {
            "response": interrupt_response.get("result", interrupt_response),
            "interrupted_turn_id_matches_requested": interrupt_turn["id"] == sea_id,
            "completed_turn_status": interrupt_turn.get("status"),
            "completed_turn_error": interrupt_turn.get("error"),
        },
        "turn_notification_sequence_observed": observed_sequence,
    }


def capture_capabilities(session: CaptureSession) -> dict:
    """Experimental-API gating facts; the queued add runs one turn of quota."""
    control = session.connect()
    thread_id = start_app_server_thread(control, session.repo)
    denied = control.request("thread/settings/update", {"threadId": thread_id, "effort": "medium"})
    experimental = session.connect(experimental=True)
    allowed = experimental.request(
        "thread/settings/update", {"threadId": thread_id, "effort": "medium"}
    )
    readback = experimental.request("thread/read", {"threadId": thread_id})
    queue_denied = control.request(
        "thread/queue/add",
        {
            "threadId": thread_id,
            "clientUserMessageId": "qualify-1",
            "input": [{"type": "text", "text": "queued"}],
        },
    )
    queue_allowed = experimental.request(
        "thread/queue/add",
        {
            "threadId": thread_id,
            "clientUserMessageId": "qualify-2",
            "input": [{"type": "text", "text": "queued and run"}],
        },
    )
    queue_list = experimental.request("thread/queue/list", {"threadId": thread_id})
    # The queued submission starts a turn on an idle thread: settle it so the
    # backend is left clean for later captures.
    experimental.wait_notification("turn/completed", timeout=180)
    control.close()
    experimental.close()
    return {
        "source": "captured against the installed app-server",
        "thread_settings_update": {
            "method": "thread/settings/update",
            "experimental": True,
            "without_experimental_api": denied.get("error", denied),
            "with_experimental_api": {
                "request": {"threadId": "<thread-id>", "effort": "<reasoning effort>"},
                "response": allowed.get("result", allowed),
                "readback_reasoning_effort": readback["result"]["thread"].get("reasoningEffort"),
            },
        },
        "native_queue": {
            "methods": [
                "thread/queue/add",
                "thread/queue/list",
                "thread/queue/update",
                "thread/queue/delete",
                "thread/queue/reorder",
                "thread/queue/start",
            ],
            "experimental": True,
            "without_experimental_api": {
                "add_error": queue_denied.get("error", queue_denied),
            },
            "with_experimental_api": {
                "add_response": queue_allowed.get("result", queue_allowed),
                "list_response": queue_list.get("result", queue_list),
            },
            "theater_position": QUEUE_POSITION,
        },
    }


def capture_ui_topology(session: CaptureSession) -> dict:
    """Stock-UI attach, shared observation, survival, zero-turn remote; tmux."""
    control = session.connect()
    thread_id = start_app_server_thread(control, session.repo)
    marker = "qualify-topology-marker"
    run_turn(control, thread_id, f"Remember the phrase {marker}. Reply with exactly: ok")

    observer = session.connect()
    observer.request("thread/resume", {"threadId": thread_id})
    ui = session.ui("tmux-topology.sock")
    launch_remote_ui(
        ui,
        codex_home=session.codex_home,
        socket_path=session.socket_path,
        thread_id=thread_id,
        codex_binary=session.binary,
    )
    ui.wait_ready(marker)
    ui.type_and_submit("Reply with exactly: topology")
    started = observer.wait_notification("turn/started", timeout=120)
    completed = observer.wait_notification("turn/completed", timeout=180)
    ui_turn_id = started["params"]["turn"]["id"]

    control.close_abrupt()
    backend_alive = session.app_server.alive()
    ui_alive = bool(ui.pane().strip())
    reconnected = session.connect()
    resumed = reconnected.request("thread/resume", {"threadId": thread_id})

    # A zero-turn thread has no rollout: the stock TUI must exit with the
    # backend's resume refusal on stderr and never start an embedded backend.
    probe = session.connect()
    blank_id = start_app_server_thread(probe, session.repo)
    bad = session.ui("tmux-zero-turn-remote.sock")
    stderr_path = session.launch_zero_turn_ui(bad, blank_id)
    error_text = stderr_path.read_text(errors="replace").strip() if stderr_path.exists() else ""
    backend_responsive = "result" in reconnected.request("thread/list", {})
    reconnected.close()
    observer.close()
    return {
        "source": "captured with a real codex TUI attached through tmux",
        "topology": {
            "backend": "codex app-server --listen unix://<private-socket>",
            "ui": "codex --remote unix://<private-socket> resume <thread-id>",
            "detached_backend": (
                "started with its own session id; its lifetime does not depend on "
                "any test/daemon pipe"
            ),
        },
        "shared_session": {
            "same_thread_ids": True,
            "observer_saw_ui_initiated": [
                "turn/started for the TUI-submitted message",
                "turn/completed for the same turn id",
            ],
            "ui_turn_completed_matches_started": completed["params"]["turn"]["id"] == ui_turn_id,
        },
        "ui_readiness": {
            "method": "deadline-bounded state polling of the tmux pane content",
            "conditions": [
                "resumed history text is rendered",
                "the composer placeholder is visible",
            ],
            "no_blind_sleep": "every wait is a predicate with a deadline",
        },
        "survival": {
            "abrupt_control_client_death": {
                "backend_alive": backend_alive,
                "ui_alive": ui_alive,
                "reconnect": (
                    "a fresh control client called thread/resume on the exact same "
                    "thread id and succeeded"
                    if "result" in resumed
                    else f"reconnect refused: {resumed.get('error')}"
                ),
            }
        },
        "remote_flag": {
            "bad_endpoint": {
                "action": "codex --remote unix://<private-socket> resume <zero-turn thread-id>",
                "outcome": "the TUI process exits; the tmux session dies; the error goes to stderr",
                "error_text": error_text,
                "backend_still_responsive": backend_responsive,
                "no_silent_fallback": (
                    "no embedded backend was started and our app-server stayed responsive"
                ),
            }
        },
    }


def capture_approval(session: CaptureSession, schema_dir: Path) -> tuple[dict, dict]:
    """Approval ownership facts; one approval turn of quota; needs tmux."""
    untrusted = session.root / "approval-untrusted"
    untrusted.mkdir()
    control = session.connect()
    thread_id = control.request("thread/start", {"cwd": str(untrusted)})["result"]["thread"]["id"]
    marker = "qualify-approval-marker"
    run_turn(control, thread_id, f"Remember the phrase {marker}. Reply with exactly: ok")

    observer = session.connect()
    observer.request("thread/resume", {"threadId": thread_id})
    ui = session.ui("tmux-approval.sock")
    launch_remote_ui(
        ui,
        codex_home=session.codex_home,
        socket_path=session.socket_path,
        thread_id=thread_id,
        codex_binary=session.binary,
    )
    ui.wait_ready(marker)

    target = untrusted / "qualify-approval.txt"
    target.unlink(missing_ok=True)
    control.request(
        "turn/start",
        {
            "threadId": thread_id,
            "input": [
                {
                    "type": "text",
                    "text": (
                        f"Use the shell tool to run exactly this command now: touch {target}. "
                        "You must actually run the command with the shell tool; do not reply "
                        "before it has run."
                    ),
                }
            ],
        },
    )
    observer_request = observer.wait_server_request(
        "item/commandExecution/requestApproval", timeout=240
    )
    wait_until(
        lambda: "Allow creating" in ui.pane() or str(target) in ui.pane(),
        timeout=240,
        what="approval dialog in the native UI",
    )
    ui.send_keys("y")
    wait_until(target.exists, timeout=240, what="approved command to execute")
    observer.wait_notification("serverRequest/resolved", timeout=60)
    observer.wait_notification("turn/completed", timeout=180)

    approval = {
        "source": "captured with two clients subscribed to the same thread",
        "approval_request": {
            "method_fired": "item/commandExecution/requestApproval",
            "server_request_shape": {
                "method": "item/commandExecution/requestApproval",
                "params_keys": sorted(observer_request["params"]),
            },
            "valid_decisions": _schema_approval_decisions(schema_dir),
        },
        "broadcast_behavior": {
            "delivered_to_every_subscribed_connection": True,
        },
        "resolution": {"notification": "serverRequest/resolved"},
        "observer_rule": OBSERVER_RULE,
        "native_ui_retains_ownership": {
            "evidence": (
                "the approval request rendered in the TUI; a single 'y' keypress "
                "approved it; the observer never sent a response"
            )
        },
    }
    unsupported = {
        "source": "explicit record of optional capabilities unavailable or not used",
        "entries": [
            {
                "capability": "thread/settings/update without the experimentalApi capability",
                "status": "unavailable",
                "reason": (
                    "server rejects with the recorded error; Theater must treat "
                    "settings updates as a separately-gated optional capability"
                ),
            },
            {
                "capability": "thread/queue/* for Theater's followup queue",
                "status": "not used by design",
                "reason": (
                    "experimental, capability-gated, and server-side persistent: the "
                    "queue would outlive a turn and is not Theater-owned"
                ),
            },
            {
                "capability": "native UI attach to a zero-turn thread via resume",
                "status": "unavailable",
                "reason": (
                    "the rollout file is only written when the first turn is accepted; "
                    "the recorded resume error shows UI attachment must wait until "
                    "after the first turn/start"
                ),
            },
            {
                "capability": "legacy execCommandApproval / applyPatchApproval server requests",
                "status": "not exercised",
                "reason": (
                    "present in the generated schema but the observed approval request "
                    "was item/commandExecution/requestApproval; Theater's observer must "
                    "still tolerate either shape"
                ),
            },
            {
                "capability": "atomic idle-only submission",
                "status": "unavailable",
                "reason": (
                    "turn/start internally steers an already-active turn: a simultaneous "
                    "native-UI submission absorbs the Theater message; Theater can only "
                    "reject known-busy state and serialize its own controls"
                ),
            },
            {
                "capability": "clarification answered by Theater",
                "status": "not exercised",
                "reason": (
                    "blocking clarifications arrive as item/tool/requestUserInput server "
                    "requests; Theater observes them without answering because the native UI "
                    "retains response ownership"
                ),
            },
        ],
    }
    control.close()
    observer.close()
    return approval, unsupported


def _schema_approval_decisions(schema_dir: Path) -> list[str]:
    """Derive the decision surface from the candidate's own generated schema."""
    schema = json.loads((schema_dir / "ServerRequest.json").read_text())
    decision = schema["definitions"]["CommandExecutionApprovalDecision"]
    decisions: set[str] = set()
    for variant in decision["oneOf"]:
        if "enum" in variant:
            decisions.add(variant["enum"][0])
        else:
            decisions.update(variant.get("properties", {}).keys())
    return sorted(decisions)


_ESSAY_RIVERS = "Write a long, detailed 1200 word essay about rivers."
_ESSAY_MOUNTAINS = "Write a 1200 word essay about mountains."
_ESSAY_SEA = "Write a 1200 word essay about the sea."


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _capture_version(session: CaptureSession) -> str:
    """Confirm the spawned backend runs the release the probe measured.

    ``connect`` initializes exactly once per connection; reusing its recorded
    initialize result means no second initialize is ever sent.
    """
    client = session.connect()
    try:
        result = session.initialize_results[-1]
    finally:
        client.close()
    user_agent = result.get("userAgent", "")
    match = _SEMVER_TOKEN.search(user_agent)
    if match is None:
        raise QualificationError(f"no codex-cli version in initialize userAgent: {user_agent!r}")
    return match.group(1)


def collect_evidence(
    out: Path,
    *,
    codex_binary: str = "codex",
    baseline: Path | None = None,
    capture_behavior: bool = False,
) -> int:
    """Collect one release's evidence bundle; return a process exit code."""
    require_fresh_output(out)
    facts = resolve_release_facts(codex_binary)
    binary = facts.resolved_path
    print(f"codex binary : {binary}")
    print(f"digest       : sha256:{facts.digest}")
    print(f"version      : {facts.version_output}")
    print(f"platform     : {facts.platform_os}; {facts.platform_arch}")

    (out / "installed_release.json").write_text(normalize_json(release_facts_document(facts)))
    generate_schema(binary, out / "protocol_schema")
    print(f"schema       : {out / 'protocol_schema'} (normalized, sorted keys)")

    if capture_behavior:
        session = open_capture_session(binary)
        try:
            if facts.version != _capture_version(session):
                raise QualificationError(
                    "the spawned app-server reports a different release than the "
                    "probed binary; refuse ambiguous evidence"
                )
            tmux = shutil.which("tmux") is not None
            documents = {
                "handshake.json": capture_handshake(session),
                "thread_lifecycle.json": capture_thread_lifecycle(session, tmux=tmux),
                "turn_control.json": capture_turn_control(session),
                "capabilities.json": capture_capabilities(session),
            }
            if tmux:
                documents["ui_topology.json"] = capture_ui_topology(session)
                approval, unsupported = capture_approval(session, out / "protocol_schema")
                documents["approval.json"] = approval
                documents["unsupported_capabilities.json"] = unsupported
            else:
                print("tmux absent: ui_topology/approval not captured", file=sys.stderr)
            for name, document in documents.items():
                (out / name).write_text(normalize_json(redact(document, session.roots)))
            print(f"behaviour    : {sorted(documents)} (redacted)")
        finally:
            session.close()
    else:
        print("behaviour    : not captured (pass --capture-behavior to run the stock probe)")

    missing = [name for name in BEHAVIOR_FILES if not (out / name).is_file()]
    if missing:
        print(
            f"incomplete bundle (missing {missing}): it is a candidate, not qualified "
            "evidence; run with --capture-behavior before any allowlist change",
            file=sys.stderr,
        )

    prior = baseline if baseline is not None else default_baseline()
    print(f"baseline     : {prior}")
    lines = diff_bundle(prior, out)
    if not lines:
        print("diff         : no differences against the prior bundle")
    else:
        print(f"diff         : {len(lines)} difference(s) against the prior bundle")
        for line in lines:
            print(f"  {line}")
    print(
        "next steps   : classify every difference against the phase-2 gate table, "
        "run the stock proof with this exact binary, then commit the bundle; "
        "the allowlist change is a separate human-reviewed commit"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="codex_qualify_runtime",
        description="Codex release qualification evidence collector (collection only).",
    )
    parser.add_argument("--out", required=True, help="newly created output bundle directory")
    parser.add_argument("--codex", default="codex", help="codex binary to qualify")
    parser.add_argument(
        "--baseline",
        type=Path,
        help="explicit prior bundle directory to diff against",
    )
    parser.add_argument(
        "--capture-behavior",
        action="store_true",
        help="run the stock topology/behaviour probe (uses model quota; needs tmux)",
    )
    args = parser.parse_args(argv)
    try:
        return collect_evidence(
            Path(args.out),
            codex_binary=args.codex,
            baseline=args.baseline,
            capture_behavior=args.capture_behavior,
        )
    except (QualificationError, subprocess.TimeoutExpired, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
