"""The legacy 2.x bootstrap end to end: the real generated launcher, a fake opencode.

The fake `opencode` is a Python script on PATH: `serve` announces a banner and answers
the `/api` routes the bootstrap gates on; anything else is the TUI. Every invocation
and request is logged, so the tests assert behaviour — one private server, the plugin
proven on that same server, the session admitted, and only then one TUI — plus the
failure paths: no TUI on refusal, no orphan backend, no leaked credential, and the
password never in argv.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from shipped import OpenCodeHarness

from theater.harness.builtin.plugins.opencode.dialect import OpenCodeDialect
from theater.harness.builtin.plugins.opencode.legacy_bootstrap_v2 import render_legacy_bootstrap
from theater.harness.builtin.plugins.opencode.native_plugin_v2 import plugin_dir

V2 = OpenCodeDialect.V2

_FAKE = """#!{executable}
import base64
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

CALLS = os.environ["FAKE_CALLS"]


def log(event):
    with open(CALLS, "a") as handle:
        handle.write(json.dumps(event) + "\\n")


def serve():
    with open(os.environ["FAKE_PID_FILE"], "w") as handle:
        handle.write(str(os.getpid()))
    if os.environ.get("FAKE_SERVE_MODE") == "hang":
        log({"hang": True})
        threading.Event().wait()
    state = os.environ.get("FAKE_PLUGIN_STATE", "active")
    preseed = os.environ.get("FAKE_PRESEED", "")
    sessions = {{sid: {{"id": sid}} for sid in preseed.split(",") if sid}}
    password = os.environ["OPENCODE_SERVER_PASSWORD"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _json(self, status, obj):
            payload = json.dumps(obj).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _answer(self, method, body):
            expected = "Basic " + base64.b64encode(f"opencode:{password}".encode()).decode()
            authorized = self.headers.get("Authorization") == expected
            log({{"http": method + " " + self.path.split("?")[0], "authorized": authorized}})
            if not authorized:
                self._json(401, {{"error": "unauthorized"}})
                return
            path = self.path.split("?")[0]
            if path == "/api/plugin":
                entry = {{
                    "id": "theater.opencode-session",
                    "source": {{"type": "local", "path": os.environ["FAKE_PLUGIN_PATH"]}},
                    "state": {{"status": "failed", "error": "boom"}} if state == "failed"
                    else {{"status": "active"}},
                }}
                self._json(200, {{"data": [entry] if state != "missing" else []}})
            elif path == "/api/session" and method == "POST":
                if os.environ.get("FAKE_CREATE_FAIL"):
                    self._json(500, {{"error": "boom"}})
                    return
                payload = json.loads(body or b"{{}}")
                sid = payload.get("id") or "ses_fake_1"
                sessions[sid] = {{"id": sid}}
                self._json(200, {{"data": sessions[sid]}})
            elif path.startswith("/api/session/"):
                sid = path.split("/")[3]
                if sid not in sessions:
                    self._json(404, {{"error": "nope"}})
                    return
                self._json(200, {{"data": sessions[sid]}})
            else:
                self._json(404, {{"error": "nope"}})

        def do_GET(self):
            self._answer("GET", b"")

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            self._answer("POST", self.rfile.read(length))

    server = HTTPServer(("127.0.0.1", 0), Handler)
    print(f"server listening on http://127.0.0.1:{server.server_address[1]}", flush=True)
    log({{"listening": server.server_address[1]}})
    server.serve_forever()


argv = sys.argv[1:]
log({{
    "argv": argv,
    "db": os.environ.get("OPENCODE_DB"),
    "password_env": bool(os.environ.get("OPENCODE_SERVER_PASSWORD")),
}})
if argv[:1] == ["serve"]:
    serve()
"""


@pytest.fixture
def fake_opencode(tmp_path, monkeypatch):
    binary = tmp_path / "bin" / "opencode"
    binary.parent.mkdir()
    # _FAKE is a plain string: collapse the doubled braces meant for an f-string.
    binary.write_text(
        _FAKE.replace("{{", "{").replace("}}", "}").replace("{executable}", sys.executable)
    )
    binary.chmod(0o755)
    monkeypatch.setenv("PATH", f"{binary.parent}:{os.environ['PATH']}")
    return binary


def _plan(tmp_path, *, approval="manual", prompt="say hello", resume=None):
    config = tmp_path / "abc.json"
    plan = OpenCodeHarness(dialect=V2).plan_launch(
        participant_id="abc123",
        prompt=prompt,
        config_path=config,
        approval=approval,
        resume=resume,
    )
    for path, content in plan.files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return plan


def _run_bootstrap(plan, tmp_path, workdir, extra_env=None):
    calls = tmp_path / "calls.jsonl"
    env = {
        **os.environ,
        "FAKE_CALLS": str(calls),
        "FAKE_PID_FILE": str(tmp_path / "serve.pid"),
        "FAKE_PLUGIN_PATH": str(
            (plugin_dir(Path(plan.env["OPENCODE_CONFIG"])) / "server.js").resolve()
        ),
        **(extra_env or {}),
    }
    return subprocess.run(
        plan.argv,
        env=env,
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    ), calls


def _events(calls: Path) -> list[dict]:
    return [json.loads(line) for line in calls.read_text().splitlines()]


def _wait_for(calls: Path, predicate, timeout: float = 10.0) -> list[dict]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if calls.exists():
            events = _events(calls)
            if predicate(events):
                return events
        time.sleep(0.05)
    raise AssertionError(f"no matching event in {calls}")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_one_verified_server_then_one_tui_with_the_prompt(tmp_path, fake_opencode, monkeypatch):
    monkeypatch.setenv("THEATER_OPENCODE_VERSION", "2.0.18")
    workdir = tmp_path / "work"
    workdir.mkdir()
    plan = _plan(tmp_path)
    run, calls = _run_bootstrap(plan, tmp_path, workdir)
    assert run.returncode == 0, run.stderr

    events = _events(calls)
    invocations = [e for e in events if "argv" in e]
    serve = next(e for e in invocations if e["argv"][:1] == ["serve"])
    tui = next(e for e in invocations if e["argv"][:1] != ["serve"])
    # Exactly one private server and one TUI, in that order.
    assert invocations.index(serve) < invocations.index(tui)
    assert len(invocations) == 2
    # The same backend endpoint reaches the plugin check, the session create, and the TUI.
    listening = next(e["listening"] for e in events if "listening" in e)
    assert f"http://127.0.0.1:{listening}" in tui["argv"]
    assert tui["argv"][tui["argv"].index("--server") + 1] == f"http://127.0.0.1:{listening}"
    session = tui["argv"][tui["argv"].index("-s") + 1]
    assert session == plan.session_id
    assert tui["argv"][tui["argv"].index("--prompt") + 1] == "say hello"
    assert tui["argv"].count("--prompt") == 1
    https = [e["http"] for e in events if "http" in e]
    assert "GET /api/plugin" in https
    assert "POST /api/session" in https
    assert f"GET /api/session/{session}" in https
    assert all(e["authorized"] for e in events if "http" in e)
    # The credential is minted for the backend and the TUI, never in argv, and reaped.
    assert serve["password_env"] and tui["password_env"]
    assert all(
        "opencode:" not in json.dumps(e["argv"]) and "Basic" not in json.dumps(e["argv"])
        for e in invocations
    )
    assert not _pid_alive(int((tmp_path / "serve.pid").read_text()))
    assert not Path(_credential_of(plan)).exists()


def test_a_promptless_launch_precreates_its_session(tmp_path, fake_opencode, monkeypatch):
    monkeypatch.setenv("THEATER_OPENCODE_VERSION", "2.0.18")
    workdir = tmp_path / "work"
    workdir.mkdir()
    plan = _plan(tmp_path, prompt="")
    run, calls = _run_bootstrap(plan, tmp_path, workdir)
    assert run.returncode == 0, run.stderr
    tui = next(e for e in _events(calls) if "argv" in e and e["argv"][:1] != ["serve"])
    assert "--prompt" not in tui["argv"]
    assert "-s" in tui["argv"]
    assert "POST /api/session" in [e["http"] for e in _events(calls) if "http" in e]


def test_a_resume_continues_the_exact_session_on_the_overlay_database(
    tmp_path, fake_opencode, monkeypatch
):
    monkeypatch.setenv("THEATER_OPENCODE_VERSION", "2.0.18")
    workdir = tmp_path / "work"
    workdir.mkdir()
    plan = _plan(tmp_path, prompt="", resume="ses_resume")
    run, calls = _run_bootstrap(
        plan,
        tmp_path,
        workdir,
        extra_env={"OPENCODE_DB": "/overlay/lineage.db", "FAKE_PRESEED": "ses_resume"},
    )
    assert run.returncode == 0, run.stderr
    events = _events(calls)
    serve = next(e for e in events if "argv" in e and e["argv"][:1] == ["serve"])
    # The validated overlay database wins over the participant's own.
    assert serve["db"] == "/overlay/lineage.db"
    tui = next(e for e in events if "argv" in e and e["argv"][:1] != ["serve"])
    assert tui["argv"][tui["argv"].index("-s") + 1] == "ses_resume"
    assert "--prompt" not in tui["argv"]
    https = [e["http"] for e in events if "http" in e]
    assert "POST /api/session" not in https
    assert "GET /api/session/ses_resume" in https


def test_a_failed_plugin_refuses_before_any_tui(tmp_path, fake_opencode, monkeypatch):
    monkeypatch.setenv("THEATER_OPENCODE_VERSION", "2.0.18")
    workdir = tmp_path / "work"
    workdir.mkdir()
    plan = _plan(tmp_path)
    run, calls = _run_bootstrap(plan, tmp_path, workdir, extra_env={"FAKE_PLUGIN_STATE": "failed"})
    assert run.returncode == 1
    assert "refusing to open OpenCode" in run.stderr
    events = _events(calls)
    assert not [e for e in events if "argv" in e and e["argv"][:1] != ["serve"]]
    assert "POST /api/session" not in [e["http"] for e in events if "http" in e]
    assert not _pid_alive(int((tmp_path / "serve.pid").read_text()))
    assert not Path(_credential_of(plan)).exists()


def _settings_of(plan) -> dict:
    script = Path(plan.argv[1]).read_text()
    literal = script.rsplit("main(json.loads(", 1)[1].removesuffix(")))\n")
    return json.loads(json.loads(literal))


def _credential_of(plan) -> str:
    return str(_settings_of(plan)["credential"])


def test_a_failed_session_create_refuses_before_any_tui(tmp_path, fake_opencode, monkeypatch):
    monkeypatch.setenv("THEATER_OPENCODE_VERSION", "2.0.18")
    workdir = tmp_path / "work"
    workdir.mkdir()
    plan = _plan(tmp_path)
    run, calls = _run_bootstrap(plan, tmp_path, workdir, extra_env={"FAKE_CREATE_FAIL": "1"})
    assert run.returncode == 1
    events = _events(calls)
    assert not [e for e in events if "argv" in e and e["argv"][:1] != ["serve"]]
    assert not _pid_alive(int((tmp_path / "serve.pid").read_text()))
    assert not Path(_credential_of(plan)).exists()


def test_a_sigterm_during_startup_reaps_the_backend_and_the_credential(
    tmp_path, fake_opencode, monkeypatch
):
    monkeypatch.setenv("THEATER_OPENCODE_VERSION", "2.0.18")
    workdir = tmp_path / "work"
    workdir.mkdir()
    plan = _plan(tmp_path)
    calls = tmp_path / "calls.jsonl"
    env = {
        **os.environ,
        "FAKE_CALLS": str(calls),
        "FAKE_PID_FILE": str(tmp_path / "serve.pid"),
        "FAKE_PLUGIN_PATH": str(
            (plugin_dir(Path(plan.env["OPENCODE_CONFIG"])) / "server.js").resolve()
        ),
        "FAKE_SERVE_MODE": "hang",
    }
    process = subprocess.Popen(
        plan.argv, env=env, cwd=workdir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    _wait_for(calls, lambda events: any("hang" in e for e in events))
    pid_file = tmp_path / "serve.pid"
    if not pid_file.exists():
        _wait_for(calls, lambda events: pid_file.exists() and bool(events))
    serve_pid = int(pid_file.read_text())
    os.kill(process.pid, signal.SIGTERM)
    assert process.wait(timeout=15) != 0
    assert not _pid_alive(serve_pid)
    assert not Path(_credential_of(plan)).exists()


def test_the_launcher_survives_apostrophes_and_json_literals(tmp_path, monkeypatch):
    """A parent path with an apostrophe and JSON null/true/false all render executable."""
    import theater.harness.builtin.plugins.opencode.legacy_bootstrap_v2 as bootstrap

    weird = tmp_path / "l'oven"
    weird.mkdir()
    monkeypatch.setattr(bootstrap.theater, "__file__", str(weird / "theater" / "__init__.py"))
    settings = {"binary": "/x/opencode", "prompt": None, "resume": None, "auto": True}
    rendered = render_legacy_bootstrap(settings)
    compile(rendered, "<launcher>", "exec")
    literal = rendered.rsplit("main(json.loads(", 1)[1].removesuffix(")))\n")
    assert json.loads(json.loads(literal)) == settings
