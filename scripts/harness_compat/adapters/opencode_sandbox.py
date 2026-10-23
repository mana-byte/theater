"""Credential-free wiring for the model-backed OpenCode server proof.

One online serve primes caches so the proof can run offline.
"""

from __future__ import annotations

import base64
import http.client
import json
import selectors
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from ..contract import InstallFailed, Sandbox
from ..process import run_pipe_group as run_group
from ..process import spawn_group, terminate_pipe_group

_BANNER_PREFIX = "opencode server listening on "
_PREWARM_PASSWORD = "harness-compat-prewarm"
_CATALOG = Path("opencode/models.json")
_CATALOG_SECONDS = 30.0


def npm_install(sb: Sandbox, package: str, version: str) -> Path:
    """npm-install exactly ``package@version`` and expose its ``opencode`` bin under the sandbox."""
    prefix = sb.root / "npm-opencode"
    try:
        run = run_group(
            ["npm", "install", "--prefix", str(prefix), f"{package}@{version}"],
            env=dict(sb.env),
            timeout=600,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise InstallFailed(f"npm install {package}@{version} did not run: {exc}") from exc
    if run.returncode != 0:
        raise InstallFailed(
            f"npm install {package}@{version} exited {run.returncode}: "
            f"{(run.stderr or run.stdout).strip()[-400:]}"
        )
    packaged = prefix / "node_modules" / ".bin" / "opencode"
    if not packaged.exists():
        raise InstallFailed(f"npm install {package}@{version} produced no bin/opencode")
    exposed = sb.bin_dir / "opencode"
    exposed.unlink(missing_ok=True)
    exposed.symlink_to(packaged)
    try:
        check = run_group([str(exposed), "--version"], env=sb.offline_env, timeout=60)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        raise InstallFailed(f"installed opencode version check failed: {exc}") from exc
    output = f"{check.stdout}{check.stderr}".strip()
    if check.returncode != 0 or version not in output:
        raise InstallFailed(f"installed opencode did not report {version!r}: {output!r}")
    return exposed


def write_provider_override(sb: Sandbox) -> None:
    """Point the proof's mistral model at the sandbox mock; no real credentials."""
    config = sb.config_dir / "opencode"
    config.mkdir(parents=True, exist_ok=True)
    (config / "opencode.json").write_text(
        json.dumps(
            {
                "$schema": "https://opencode.ai/config.json",
                "provider": {
                    "mistral": {
                        "options": {
                            "baseURL": sb.mock_base_urls["openai"],
                            "apiKey": "mock-key",
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def prewarm_catalog(sb: Sandbox, *, plugin: Path | None = None, timeout: float = 90.0) -> bool:
    """Prime the catalog and first plugin load before offline proofs."""
    work = sb.root / "opencode-prewarm"
    work.mkdir(parents=True, exist_ok=True)
    config: dict[str, object] = {
        "$schema": "https://opencode.ai/config.json",
        "model": "mistral/mistral-large-latest",
    }
    if plugin is not None:
        config["plugin"] = [str(plugin)]
    config_path = work / "prewarm.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    process = spawn_group(
        ["opencode", "serve", "--port", "0", "--hostname", "127.0.0.1"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env={
            **sb.env,
            "OPENCODE_SERVER_PASSWORD": _PREWARM_PASSWORD,
            "OPENCODE_DB": str(work / "prewarm.sqlite"),
            "OPENCODE_CONFIG": str(config_path),
        },
        cwd=work,
    )
    try:
        endpoint = _await_banner(process, timeout)
        if endpoint is None:
            return False
        if _post(endpoint, "/session", timeout=timeout) != 200:
            return False
        return _await_catalog(sb, timeout=_CATALOG_SECONDS)
    finally:
        terminate_pipe_group(process)
        if process.stdout is not None:
            process.stdout.close()


def _await_catalog(sb: Sandbox, *, timeout: float) -> bool:
    """The catalog lands on disk a beat after the session response; poll for it."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (Path(sb.env["XDG_CACHE_HOME"]) / _CATALOG).is_file():
            return True
        time.sleep(0.2)
    return False


def _await_banner(process: subprocess.Popen[bytes], timeout: float) -> str | None:
    """Block until serve announces its port-0 loopback origin, or give up."""
    if process.stdout is None:
        return None
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline and process.poll() is None:
            if not selector.select(timeout=0.2):
                continue
            line = process.stdout.readline().decode("utf-8", errors="replace")
            if not line:
                return None
            stripped = line.strip()
            if stripped.startswith(_BANNER_PREFIX):
                return stripped[len(_BANNER_PREFIX) :].strip() or None
    finally:
        selector.close()
    return None


def _post(endpoint: str, path: str, *, timeout: float) -> int | None:
    """One authenticated request; the status code, or None when it never lands."""
    parts = urlsplit(endpoint)
    if parts.scheme != "http" or parts.hostname != "127.0.0.1" or parts.port is None:
        return None
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=timeout)
    token = base64.b64encode(f"opencode:{_PREWARM_PASSWORD}".encode()).decode()
    try:
        conn.request("POST", path, headers={"Authorization": f"Basic {token}"})
        return conn.getresponse().status
    except OSError:
        return None
    finally:
        conn.close()
