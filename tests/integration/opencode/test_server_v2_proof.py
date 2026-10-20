"""Opt-in stock proof of the OpenCode 2.x server surface Theater consumes, without a model.

The same surface the plugin documents as verified model-free on stock 2.0.x: the banner, Basic
auth, `/api/info`, `/api/session` with a location directory, durable prompt admission, and the
`session_message` rows the lineage database projects into the shared 1.x shapes.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import http.client
import os
import secrets
import selectors
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from theater.harness.builtin.plugins.opencode import ids_v2, store
from theater.harness.builtin.plugins.opencode.constants import V2_DB_NAME
from theater.harness.builtin.plugins.opencode.http_v2 import OpenCodeV2Client, session_directory
from theater.harness.builtin.plugins.opencode.runtime_plan import (
    OPENCODE_SERVER_V2_MAX_VERSION,
    OPENCODE_SERVER_V2_MIN_VERSION,
    parse_opencode_version,
    release_text,
)
from theater.harness.builtin.plugins.opencode.server_discovery import parse_server_stdout_endpoint

_PROOF_ENV = "THEATER_OPENCODE_V2_SERVER_CONFORMANCE"


def _binary_version() -> str | None:
    try:
        run = subprocess.run(
            ["opencode", "--version"], capture_output=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return run.stdout.decode("utf-8", errors="replace").strip() or None


_BINARY_VERSION = _binary_version()
_PARSED_VERSION = parse_opencode_version(_BINARY_VERSION or "")
_ADMITTED_STOCK = (
    _PARSED_VERSION is not None
    and OPENCODE_SERVER_V2_MIN_VERSION <= _PARSED_VERSION < OPENCODE_SERVER_V2_MAX_VERSION
)
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get(_PROOF_ENV) != "1" or not _ADMITTED_STOCK,
        reason=(
            f"set {_PROOF_ENV}=1 with stock OpenCode "
            f"{release_text(OPENCODE_SERVER_V2_MIN_VERSION)} up to "
            f"{release_text(OPENCODE_SERVER_V2_MAX_VERSION)} (found {_BINARY_VERSION!r})"
        ),
    ),
]


class StockServer:
    """One isolated stock 2.x serve process with its private credential file."""

    def __init__(self, process: subprocess.Popen[bytes], workdir: Path, endpoint: str) -> None:
        self.process = process
        self.workdir = workdir
        self.endpoint = endpoint
        self.token_file = workdir / "runtime.token"
        self.database = workdir / V2_DB_NAME

    def stop(self) -> None:
        # serve forks workers: signal the whole group so none is orphaned.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.process.pid, signal.SIGTERM)
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait()
        shutil.rmtree(self.workdir, ignore_errors=True)

    def client(self) -> OpenCodeV2Client:
        return OpenCodeV2Client(endpoint=self.endpoint, token_file=self.token_file)


def _read_banner_endpoint(process: subprocess.Popen[bytes]) -> str:
    """Block until serve announces its port-0 loopback origin."""
    assert process.stdout is not None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + 30.0
    try:
        while time.monotonic() < deadline:
            if selector.select(timeout=0.2):
                line = process.stdout.readline().decode("utf-8", errors="replace")
                if not line:
                    break
                endpoint = parse_server_stdout_endpoint(line)
                if endpoint is not None:
                    return endpoint
    finally:
        selector.close()
    raise AssertionError("stock 2.x serve printed no parseable listening banner")


def _status(endpoint: str, path: str, *, password: str | None = None) -> int:
    parts = urlsplit(endpoint)
    assert parts.hostname is not None and parts.port is not None
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10.0)
    headers = {}
    if password is not None:
        token = base64.b64encode(f"opencode:{password}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"
    try:
        conn.request("GET", path, headers=headers)
        return conn.getresponse().status
    finally:
        conn.close()


@pytest.fixture(scope="module")
def stock() -> Iterator[StockServer]:
    workdir = Path(tempfile.mkdtemp(prefix="theater-ocv2proof-"))
    password = secrets.token_urlsafe(24)
    token_file = workdir / "runtime.token"
    token_file.write_text(password, encoding="utf-8")
    token_file.chmod(0o600)
    process = subprocess.Popen(
        ["opencode", "serve", "--port", "0", "--hostname", "127.0.0.1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        cwd=workdir,
        env={
            **os.environ,
            "OPENCODE_SERVER_PASSWORD": password,
            "OPENCODE_DB": str(workdir / V2_DB_NAME),
        },
    )
    server = StockServer(process, workdir, "http://127.0.0.1:1")
    try:
        server.endpoint = _read_banner_endpoint(process)
        yield server
    finally:
        server.stop()


def test_v2_banner_basic_auth_and_info(stock: StockServer) -> None:
    password = stock.token_file.read_text(encoding="utf-8")
    assert _status(stock.endpoint, "/api/info") == 401
    assert _status(stock.endpoint, "/api/info", password=password) == 200
    assert asyncio.run(stock.client().version()) == release_text(_PARSED_VERSION or (0, 0, 0))


def test_v2_session_location_and_idle_absence(stock: StockServer) -> None:
    async def scenario() -> None:
        client = stock.client()
        session_id = await client.create_session(directory=str(stock.workdir))
        session = await client.read_session(session_id)
        assert session["id"] == session_id
        assert session_directory(session) == os.path.realpath(stock.workdir)
        assert session_id not in await client.active()  # absence proves idle

    asyncio.run(scenario())


def test_v2_prompt_admission_is_durable_and_projects_into_the_lineage_database(
    stock: StockServer,
) -> None:
    async def admit() -> tuple[str, str]:
        client = stock.client()
        session_id = await client.create_session(directory=str(stock.workdir))
        message_id = ids_v2.message_id()
        admitted = await client.prompt(
            session_id, message_id=message_id, text="harness compat proof", delivery="queue"
        )
        assert admitted["id"] == message_id and admitted["sessionID"] == session_id
        return session_id, message_id

    session_id, message_id = asyncio.run(admit())
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        conn = store.open_readonly(stock.database)
        if conn is not None:
            try:
                rows = [
                    dict(zip(("id", "info"), row, strict=True))
                    for row in store.history_messages(conn, session_id)
                ]
            finally:
                conn.close()
            if any(row["id"] == message_id for row in rows):
                info = next(row["info"] for row in rows if row["id"] == message_id)
                assert '"role": "user"' in info
                return
        time.sleep(0.2)
    raise AssertionError("the admitted prompt never reached the lineage database")
