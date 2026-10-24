"""The live rig: a real daemon, Regie bridge and stock CLI in a sandbox, against a mock model.

Only the compatibility suite uses it, and only under the pipeline sandbox token: every path the
rig touches is derived from the sandbox root, and nothing falls back to a default home.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import os
import pwd
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scripts.harness_compat.contract import (
    SUITE_ENV_BINARY,
    SUITE_ENV_EVIDENCE_DIR,
    SUITE_ENV_HARNESS,
    SUITE_ENV_MUTATION,
    SUITE_ENV_SANDBOX_ROOT,
    SUITE_ENV_VERSION,
    Hold,
    MockProtocol,
    MockReply,
    RecordedRequest,
)
from scripts.harness_compat.mock import MockServer
from scripts.harness_compat.process import SANDBOX_ENV
from tests.support import mutations
from tests.support.paths import REPO_ROOT
from theater.frontend import FrontendClient
from theater.frontend.dto import Controls, Job, Participant
from theater.harness.contracts.compat import ProvisionContext, Provisioned
from theater.harness.contracts.harness import Harness
from theater.harness.contracts.observation import ScreenKind, ScreenReading

ENV_HARNESS = SUITE_ENV_HARNESS
ENV_BINARY = SUITE_ENV_BINARY
ENV_VERSION = SUITE_ENV_VERSION
ENV_SANDBOX_ROOT = SUITE_ENV_SANDBOX_ROOT
ENV_EVIDENCE_DIR = SUITE_ENV_EVIDENCE_DIR

#: Logical model names the suite requests; ``provision`` makes the CLI accept them.
LOGICAL_MODELS = ("mock-a", "mock-b")
CAPABILITIES = ("orchestration.v1", "state.follow.v1", "catalogs.v1")
_BOOT_SECONDS = 90.0
_STOP_SECONDS = 20.0
_PANE_CAPTURE_LINES = 200
_JOB_SLICE_SECONDS = 3.0
#: One short directory per scenario: unix socket paths cap near 100 bytes.
_ROOMS = itertools.count(1)
#: The probe sidecar's server (plugin) name and the one tool it serves; see ``compat_probe/``.
PROBE_SERVER = "compat_probe"
PROBE_TOOL = "compat_probe_mark"
_PROBE_SOURCE = Path(__file__).with_name("compat_probe")
#: Nothing listens on the dead proxy, so a CLI honouring it cannot leave the machine.
_DEAD_PROXY = "http://127.0.0.1:9"
_LOOPBACK = "127.0.0.1,localhost"


def inconclusive(reason: str) -> None:
    """An environmental or unattributed failure: an unexpected skip is inconclusive."""
    pytest.skip(f"inconclusive: {reason}")


def violation(message: str) -> None:
    """A proven contract violation: red, and a later pass never cancels it."""
    pytest.fail(message, pytrace=False)


@dataclass(frozen=True)
class LiveSession:
    """What the pipeline hands the suite: which harness, which binary, where to live."""

    harness: str
    binary: Path
    version: str
    sandbox_root: Path
    evidence_dir: Path
    token: str

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> LiveSession:
        environ = os.environ if environ is None else environ
        missing = [
            name
            for name in (
                SANDBOX_ENV,
                ENV_HARNESS,
                ENV_BINARY,
                ENV_VERSION,
                ENV_SANDBOX_ROOT,
                ENV_EVIDENCE_DIR,
            )
            if not environ.get(name)
        ]
        if missing:
            raise RuntimeError(f"the live suite needs the pipeline sandbox; missing {missing}")
        return cls(
            harness=environ[ENV_HARNESS],
            binary=Path(environ[ENV_BINARY]),
            version=environ[ENV_VERSION],
            sandbox_root=Path(environ[ENV_SANDBOX_ROOT]).resolve(),
            evidence_dir=Path(environ[ENV_EVIDENCE_DIR]).resolve(),
            token=environ[SANDBOX_ENV],
        )


def assert_isolated(root: Path, *paths: Path) -> None:
    """Abort the session unless every path lives under the sandbox root, away from real state."""
    real_home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    resolved_root = root.resolve()
    live = real_home / ".theater"
    if resolved_root == live or live in resolved_root.parents or resolved_root in live.parents:
        pytest.exit(f"refusing to run: the sandbox root {resolved_root} overlaps {live}", 2)
    for path in paths:
        resolved = path.resolve()
        if resolved_root not in (resolved, *resolved.parents):
            pytest.exit(f"refusing to run: {resolved} is outside the sandbox {resolved_root}", 2)


def hermetic_environment(
    root: Path, *, token: str, binary_dir: Path, extra: Mapping[str, str]
) -> dict[str, str]:
    """The only environment the daemon, provider and stock CLI ever see."""
    home = root / "h"
    path = os.pathsep.join(
        dict.fromkeys(
            [
                str(binary_dir),
                str(Path(sys.executable).parent),
                *os.environ["PATH"].split(os.pathsep),
            ]
        )
    )
    return {
        "HOME": str(home),
        "PATH": path,
        "TERM": "xterm-256color",
        "LANG": "en_US.UTF-8",
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "TMPDIR": str(root / "t"),
        "TMUX_TMPDIR": str(root / "t"),
        "THEATER_HOME": str(root / "th"),
        "NO_PROXY": _LOOPBACK,
        "no_proxy": _LOOPBACK,
        "HTTP_PROXY": _DEAD_PROXY,
        "HTTPS_PROXY": _DEAD_PROXY,
        "ALL_PROXY": _DEAD_PROXY,
        SANDBOX_ENV: token,
        **extra,
    }


def _config_toml(harness: str, provisioned: Provisioned, marker_dir: Path) -> str:
    lines = [f"[models]\n{harness} = {_toml_list(provisioned.models)}\n"]
    if provisioned.efforts:
        lines.append(f"[reasoning]\n{harness} = {_toml_list(provisioned.efforts)}\n")
    lines.append(
        f'[mcp]\nenabled = ["{PROBE_SERVER}"]\n\n'
        f'[mcp.plugins.{PROBE_SERVER}]\nmarker_dir = "{marker_dir}"\n'
    )
    return "\n".join(lines)


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:80] or "scenario"


def _toml_list(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(f'"{value}"' for value in values) + "]"


@dataclass(frozen=True)
class HeldTurn:
    """A turn whose model answer is held open, and the job that will carry its result."""

    hold: Hold
    handle: str


@dataclass
class LiveRig:
    """One scenario's world: boot it with :meth:`open`, drive it through the SDK."""

    session: LiveSession
    harness: Harness
    root: Path
    env: dict[str, str]
    provisioned: Provisioned
    mock: MockServer
    client: FrontendClient
    workdir: Path
    marker_dir: Path
    label: str
    spawned: list[str] = field(default_factory=list)

    @property
    def protocol(self) -> MockProtocol:
        return self.provisioned.protocol

    @property
    def approval(self) -> str:
        """The most restrictive approval this harness declares: the one that must gate."""
        approvals = self.harness.launch_parameter_support.approvals
        return "manual" if "manual" in approvals else approvals[0]

    @property
    def theater_home(self) -> Path:
        return self.root / "th"

    @classmethod
    @asynccontextmanager
    async def open(
        cls, session: LiveSession, harness: Harness, *, label: str
    ) -> AsyncIterator[LiveRig]:
        if harness.compat is None:
            raise RuntimeError(f"harness {session.harness!r} declares no compat manifest")
        label = _slug(label)
        root = session.sandbox_root / f"l{next(_ROOMS)}"
        workdir, marker_dir = root / "w", root / "m"
        for directory in (root / "h", root / "t", root / "th", workdir, marker_dir):
            directory.mkdir(parents=True, exist_ok=True)
        assert_isolated(session.sandbox_root, root, workdir, marker_dir)
        mock = MockServer(session.evidence_dir / f"{label}.requests.jsonl", models=LOGICAL_MODELS)
        mock.start()
        env: dict[str, str] = {}
        client: FrontendClient | None = None
        try:
            provisioned = harness.compat.provision(
                ProvisionContext(
                    sandbox_root=root,
                    home=root / "h",
                    config_dir=root / "h" / ".config",
                    workdir=workdir,
                    mock_urls=mock.base_urls,
                    binary=session.binary,
                    version=session.version,
                    models=LOGICAL_MODELS,
                )
            )
            for path, text in provisioned.files.items():
                assert_isolated(session.sandbox_root, path)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
            env = hermetic_environment(
                root, token=session.token, binary_dir=session.binary.parent, extra=provisioned.env
            )
            assert_isolated(session.sandbox_root, Path(env["THEATER_HOME"]), Path(env["HOME"]))
            theater_dir = root / "th"
            (theater_dir / "config.toml").write_text(
                _config_toml(session.harness, provisioned, marker_dir), encoding="utf-8"
            )
            shutil.copytree(_PROBE_SOURCE, theater_dir / "plugins" / PROBE_SERVER)
            if mutation := os.environ.get(SUITE_ENV_MUTATION):
                mutations.install(mutation, session.harness, theater_dir / "plugins")
            socket = theater_dir / "var" / "run" / "daemon.sock"
            failure = _boot(env, socket)
            if failure is not None:  # a loaded machine can miss the bridge's own start deadline
                _stop(env, theater_dir)
                failure = _boot(env, socket)
            if failure is not None:
                inconclusive(failure)
            client = FrontendClient(
                socket, client_id="compat-suite", required_capabilities=CAPABILITIES
            )
            await client.connect()
            rig = cls(
                session, harness, root, env, provisioned, mock, client, workdir, marker_dir, label
            )
            try:
                yield rig
            finally:
                rig.capture_evidence()
        finally:
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.close()
            _stop(env, root / "th")
            mock.stop()

    # -- driving the participant through the public SDK ---------------------------------------

    async def spawn(
        self,
        approval: str,
        *,
        prompt: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        resume: str | None = None,
    ) -> str:
        """Spawn through the real launch planner and return the participant id."""
        options: dict[str, object] = {"cwd": str(self.workdir)}
        if model is not None:
            options["model"] = model
        if reasoning_effort is not None:
            options["reasoning_effort"] = reasoning_effort
        if resume is not None:
            options["resume"] = resume
        accepted = (
            await self.client.participants.spawn(
                self.session.harness, prompt, approval, idempotency_key=uuid.uuid4().hex, **options
            )
        ).value
        operation = await self._settled(accepted.operation_id)
        if operation.state != "succeeded":
            detail = operation.error.to_wire() if operation.error is not None else operation.state
            violation(f"spawning with approval {approval!r} did not succeed: {detail}")
        assert accepted.participant_id is not None
        self.spawned.append(accepted.participant_id)
        return accepted.participant_id

    async def _settled(self, operation_id: str, *, seconds: float = 60.0):
        deadline = time.monotonic() + seconds
        while True:
            waited = await self.client.operations.wait(operation_id, wait_seconds=5)
            if waited.value.operation.state in ("succeeded", "failed"):
                return waited.value.operation
            if time.monotonic() > deadline:
                inconclusive(f"operation {operation_id} stayed {waited.value.operation.state}")

    async def participant(self, participant_id: str) -> Participant:
        return (await self.client.participants.get(participant_id)).value

    async def controls(self, participant_id: str) -> Controls:
        return (await self.client.controls.get(participant_id)).value

    async def ready(self, participant_id: str, *, seconds: float = 90.0) -> None:
        """Wait for the daemon to call a send admissible; classify why it never did."""
        deadline = time.monotonic() + seconds
        reason = "no controls reported"
        while time.monotonic() < deadline:
            participant = await self.participant(participant_id)
            if participant.status == "dead":
                self._classify_exit(participant_id)
            send = (await self.controls(participant_id)).actions.get("send")
            if send is not None and send.admissible:
                return
            reason = "no send action" if send is None else f"{send.reason}: {send.detail}"
            await asyncio.sleep(0.25)
        inconclusive(f"send never became admissible within {seconds:g}s ({reason})")

    async def until(
        self,
        participant_id: str,
        reached: Callable[[Participant], bool],
        *,
        what: str,
        seconds: float = 30.0,
    ) -> Participant:
        """Poll until ``reached``; a participant that never gets there is inconclusive."""
        deadline = time.monotonic() + seconds
        while True:
            participant = await self.participant(participant_id)
            if reached(participant):
                return participant
            if time.monotonic() > deadline:
                inconclusive(f"participant {participant_id} never {what} within {seconds:g}s")
            await asyncio.sleep(0.25)

    async def screen(self, participant_id: str) -> str:
        """The participant's pane as the plugin's classifier sees it."""
        route = (await self.participant(participant_id)).terminal_route
        if route is None:
            return ""
        return _tmux(self.env, "capture-pane", "-p", "-t", route.identity.terminal_id)

    async def reading(self, participant_id: str) -> ScreenReading:
        return self.harness.observer.screen_reading(await self.screen(participant_id))

    async def expect_screen(
        self, participant_id: str, kind: ScreenKind, *, seconds: float = 20.0
    ) -> None:
        """The plugin's own classifier must recognise the live screen as ``kind``."""
        deadline = time.monotonic() + seconds
        while True:
            reading = await self.reading(participant_id)
            if reading.kind is kind:
                return
            if time.monotonic() > deadline:
                shown = "\n".join((await self.screen(participant_id)).splitlines()[-8:])
                violation(
                    f"the screen classifier never read {kind.value} within {seconds:g}s "
                    f"(last reading {reading.kind.value}):\n{shown}"
                )
            await asyncio.sleep(0.3)

    def _classify_exit(self, participant_id: str) -> None:
        if not self.healthy():
            inconclusive("a suite component died before the CLI reached readiness")
        violation(f"the stock CLI exited before readiness (participant {participant_id})")

    async def send(
        self,
        participant_id: str,
        prompt: str,
        *,
        seconds: float = 90.0,
        guard: Callable[[], Awaitable[None]] | None = None,
    ) -> Job:
        accepted = (
            await self.client.controls.send(
                participant_id, prompt, idempotency_key=uuid.uuid4().hex
            )
        ).value
        assert accepted.job_handle is not None
        return await self.job(accepted.job_handle, seconds=seconds, guard=guard)

    async def job(
        self,
        handle: str,
        *,
        seconds: float = 90.0,
        guard: Callable[[], Awaitable[None]] | None = None,
    ) -> Job:
        """Await a job; ``guard`` runs between waits so a stuck turn can be attributed early."""
        deadline = time.monotonic() + seconds
        while True:
            slice_seconds = max(1.0, min(_JOB_SLICE_SECONDS, deadline - time.monotonic()))
            job = (await self.client.jobs.await_([handle], wait_seconds=slice_seconds)).value.jobs[
                0
            ]
            if job.state != "running":
                return job
            if guard is not None:
                await guard()
            if time.monotonic() >= deadline:
                inconclusive(f"job {handle} was still running after {seconds:g}s")

    async def interrupt(self, participant_id: str) -> None:
        accepted = (
            await self.client.controls.interrupt(participant_id, idempotency_key=uuid.uuid4().hex)
        ).value
        operation = await self._settled(accepted.operation_id)
        if operation.state != "succeeded":
            detail = operation.error.to_wire() if operation.error is not None else operation.state
            violation(f"interrupting participant {participant_id} did not succeed: {detail}")

    async def hold_turn(self, participant_id: str, prompt: str) -> HeldTurn:
        """Send ``prompt``, hold its answer until released, and wait until the CLI looks busy."""
        hold = self.hold()
        self.queue(MockReply("thinking", hold=hold))
        accepted = (
            await self.client.controls.send(
                participant_id, prompt, idempotency_key=uuid.uuid4().hex
            )
        ).value
        assert accepted.job_handle is not None
        if not await asyncio.to_thread(hold.wait_reached, 60):
            violation("the held request never reached the model")
        await self.expect_screen(participant_id, ScreenKind.WORKING)
        return HeldTurn(hold, accepted.job_handle)

    async def steer(self, participant_id: str, prompt: str) -> None:
        accepted = (
            await self.client.controls.steer(
                participant_id, prompt, idempotency_key=uuid.uuid4().hex
            )
        ).value
        operation = await self._settled(accepted.operation_id)
        if operation.state != "succeeded":
            detail = operation.error.to_wire() if operation.error is not None else operation.state
            violation(f"steering participant {participant_id} did not succeed: {detail}")

    async def retire(self, participant_id: str) -> None:
        """Terminate one participant and wait for it to be dead; the rig tears the rest down."""
        with contextlib.suppress(Exception):
            await self.client.participants.terminate(
                participant_id, idempotency_key=uuid.uuid4().hex
            )
        await self.until(participant_id, lambda item: item.status == "dead", what="died")

    def turns(self, prompt: str) -> list[RecordedRequest]:
        """The model requests carrying ``prompt``; one offering no tools is background work."""
        return [r for r in self.mock.requests if r.tools and r.carries(prompt)]

    def queue(self, reply: MockReply) -> None:
        self.mock.queue(self.protocol, reply)

    def hold(self) -> Hold:
        return self.mock.hold()

    # -- health, evidence --------------------------------------------------------------------

    def healthy(self) -> bool:
        """Whether every suite-controlled component is still alive."""
        pidfile = self.theater_home / "var" / "run" / "daemon.pid"
        try:
            os.kill(int(pidfile.read_text().split()[0]), 0)
        except (OSError, ValueError, IndexError):
            return False
        return self.client.connected

    def capture_evidence(self) -> None:
        """Copy logs and pane captures to the evidence directory; bounded and best effort."""
        target = self.session.evidence_dir / self.label
        target.mkdir(parents=True, exist_ok=True)
        for source in (
            self.theater_home / "var" / "logs" / "daemon" / "daemon.log",
            self.theater_home / "regie" / "var" / "logs" / "daemon-start.log",
            self.theater_home / "regie" / "var" / "logs" / "bridge.log",
        ):
            with contextlib.suppress(OSError):
                shutil.copyfile(source, target / source.name)
        for pane in _panes(self.env):
            capture = _tmux(
                self.env, "capture-pane", "-p", "-t", pane, "-S", f"-{_PANE_CAPTURE_LINES}"
            )
            (target / f"pane-{pane.lstrip('%')}.txt").write_text(capture, encoding="utf-8")


def _regie(env: Mapping[str, str], *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
    socket = Path(env["THEATER_HOME"]) / "var" / "run" / "daemon.sock"
    return subprocess.run(
        [sys.executable, "-m", "regie", "--socket", str(socket), *args],
        env=dict(env),
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _boot(env: Mapping[str, str], socket: Path) -> str | None:
    """Start the bridge, which starts the daemon and the private tmux server; why not, if not."""
    result = _regie(env, "bridge", "start", timeout=_BOOT_SECONDS)
    if result.returncode != 0:
        return f"the Regie bridge did not start: {result.stderr.strip()[-400:]}"
    if not socket.exists():
        return "the daemon socket never appeared"
    return None


def _stop(env: Mapping[str, str], theater_home: Path) -> None:
    """Stop the bridge, the daemon and the private tmux server; every step is attempted."""
    if not env:
        return
    with contextlib.suppress(Exception):
        _regie(env, "bridge", "stop", timeout=_STOP_SECONDS)
    pidfile = theater_home / "var" / "run" / "daemon.pid"
    with contextlib.suppress(OSError, ValueError, IndexError):
        pid = int(pidfile.read_text().split()[0])
        os.kill(pid, signal.SIGTERM)
        deadline = time.monotonic() + _STOP_SECONDS
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.1)
        else:
            os.kill(pid, signal.SIGKILL)
    with contextlib.suppress(Exception):
        _tmux(env, "kill-server")


def _tmux(env: Mapping[str, str], *args: str) -> str:
    result = subprocess.run(
        ["tmux", *args], env=dict(env), capture_output=True, text=True, timeout=10, check=False
    )
    return result.stdout


def _panes(env: Mapping[str, str]) -> list[str]:
    return _tmux(env, "list-panes", "-a", "-F", "#{pane_id}").split()
