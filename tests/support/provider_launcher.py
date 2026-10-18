"""Launch the standalone provider process against a daemon and read what it did."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from tests.support.paths import REPO_ROOT, TESTS_DIR
from theater import protocol
from theater.daemon.plugins.credentials import credential_verifier

PROCESS = TESTS_DIR / "support" / "provider_process.py"


REPOSITORY = REPO_ROOT


@dataclass
class _ProviderProcess:
    process: asyncio.subprocess.Process
    root: Path
    ready: dict[str, object]

    @property
    def evidence_path(self) -> Path:
        value = self.ready["evidence_file"]
        assert isinstance(value, str)
        return Path(value)

    async def wait(self, *, timeout: float = 1) -> int:
        return await asyncio.wait_for(self.process.wait(), timeout=timeout)

    async def stop(self) -> None:
        if self.process.returncode is None:
            self.process.send_signal(signal.SIGTERM)
        returncode = await self.wait(timeout=2)
        stdout, stderr = await self.process.communicate()
        assert returncode == 0, (stdout.decode(), stderr.decode())

    async def wait_for_effects(self, *operation_ids: str) -> None:
        async with asyncio.timeout(2):
            while not set(operation_ids).issubset(
                event["operation_id"] for event in physical_events(self)
            ):
                await asyncio.sleep(0.005)

    def evidence(self) -> list[dict[str, object]]:
        if not self.evidence_path.exists():
            return []
        return [
            value
            for line in self.evidence_path.read_text(encoding="utf-8").splitlines()
            if isinstance(value := json.loads(line), dict)
        ]


@asynccontextmanager
async def provider_process(
    socket_path: Path,
    *,
    plan: Mapping[str, object] | None = None,
    callback_timeout: float = 30.0,
    provider_id: str = "fixture-provider",
    provider_credential: str = "fixture-credential",
) -> AsyncIterator[_ProviderProcess]:
    root = Path(tempfile.mkdtemp(prefix="r10pp-", dir="/tmp"))
    ready_file = root / "ready.json"
    plan_path = root / "plan.json"
    if plan is not None:
        plan_path.write_text(json.dumps(dict(plan)), encoding="utf-8")
    inherited = {
        **os.environ,
        "THEATER_HOME": "/control/theater",
        "THEATER_ID": "control-participant",
        "THEATER_RUNTIME_TOKEN": "control-token",
        "TMUX": "/private/tmp/tmux-501/default,123,0",
        "TMUX_PANE": "%99",
        "TMUX_TMPDIR": "/private/tmp",
        "CLAUDE_CODE_MESSAGING_SOCKET": "/control/claude.sock",
        "CLAUDE_CODE_MESSAGING_TOKEN": "control-token",
    }
    inherited.pop("PYTHONHOME", None)
    inherited.pop("PYTHONPATH", None)
    command = [
        sys.executable,
        str(PROCESS),
        "--socket",
        str(socket_path),
        "--root",
        str(root),
        "--ready-file",
        str(ready_file),
        "--provider-id",
        provider_id,
        "--provider-credential",
        provider_credential,
        "--client-id",
        "fixture-client",
        "--callback-timeout",
        str(callback_timeout),
    ]
    if plan is not None:
        command.extend(("--plan", str(plan_path)))
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=REPOSITORY,
        env=inherited,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    provider: _ProviderProcess | None = None
    try:
        ready = await _read_ready(process, ready_file)
        provider = _ProviderProcess(process, root, ready)
        yield provider
    finally:
        if provider is None:
            if process.returncode is None:
                process.kill()
            await process.communicate()
        elif process.returncode is None:
            await provider.stop()
        shutil.rmtree(root, ignore_errors=True)


async def _read_ready(process: asyncio.subprocess.Process, ready_file: Path) -> dict[str, object]:
    async with asyncio.timeout(2):
        while not ready_file.exists():
            if process.returncode is not None:
                stdout, stderr = await process.communicate()
                raise AssertionError((process.returncode, stdout.decode(), stderr.decode()))
            await asyncio.sleep(0.005)
    value = json.loads(ready_file.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


async def raw_register_provider(socket_path: Path, credential: str) -> str:
    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    try:
        requests = (
            {
                "id": 1,
                "method": "frontend.handshake",
                "params": {
                    "api": {"major": 1, "minor": 1},
                    "client_id": "fixture-operator",
                    "role": "operator",
                    "channel": "rpc",
                    "required_capabilities": [],
                },
            },
            {
                "id": 2,
                "method": "frontend.providers.register",
                "params": {
                    "selector": "fixture-candidate",
                    "kind": "test",
                    "credential_verifier": credential_verifier(credential),
                    "capabilities": ["terminal-provider.v1"],
                    "limits": {},
                },
                "idempotency_key": "fixture-candidate-registration",
            },
        )
        responses = []
        for request in requests:
            writer.write(protocol.encode(request))
            await writer.drain()
            responses.append(json.loads(await protocol.read_message(reader)))
        assert all(response["ok"] is True for response in responses)
        provider_id = responses[1]["result"]["provider_id"]
        assert isinstance(provider_id, str)
        return provider_id
    finally:
        writer.close()
        await writer.wait_closed()


def physical_events(provider: _ProviderProcess) -> list[dict[str, object]]:
    return [entry for entry in provider.evidence() if entry["event"] == "physical_side_effect"]
