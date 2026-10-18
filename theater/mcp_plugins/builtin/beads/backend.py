"""Bounded, local Beads CLI access for one embedded play board."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any


class BeadsError(ValueError):
    """A refused board operation or unavailable Beads runtime."""


class Board:
    def __init__(self, state_dir: Path, instance_id: str, bd_path: str | None) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", instance_id):
            raise BeadsError("invalid play instance ID")
        self.instance_id = instance_id
        self.state_dir = state_dir.resolve()
        self.path = self.state_dir / "plays" / instance_id
        self.prefix = "tb" + hashlib.sha256(instance_id.encode()).hexdigest()[:12]
        self.bd_path = bd_path

    def local_id(self, task_id: object) -> bool:
        return (
            isinstance(task_id, str)
            and len(task_id) <= 128
            and re.fullmatch(rf"{self.prefix}-[a-z0-9]+(?:\.[0-9]+)*", task_id) is not None
        )

    def require_id(self, task_id: str, rows: dict[str, dict[str, Any]]) -> None:
        if not self.local_id(task_id) or task_id not in rows:
            raise BeadsError("task ID must identify an existing task in this play")

    def _binary(self) -> str:
        binary = self.bd_path or shutil.which("bd")
        if not binary or not Path(binary).is_file():
            raise BeadsError(
                "Beads bd v1.3.1 is required; run `brew install beads` or "
                "`npm install -g @beads/bd`"
            )
        return str(Path(binary).resolve())

    def _env(self) -> dict[str, str]:
        home = self.state_dir / "home"
        home.mkdir(parents=True, exist_ok=True)
        return {
            "PATH": os.defpath,
            "HOME": str(home),
            "BEADS_DIR": str(self.path / ".beads"),
            "BD_NON_INTERACTIVE": "1",
            "BD_LAST_TOUCHED_FALLBACK": "0",
            "BD_NO_DEP_TYPE_WARNING": "1",
            "DO_NOT_TRACK": "1",
            "NO_COLOR": "1",
            "LANG": "C.UTF-8",
        }

    def _run(self, *args: str, actor: str | None = None, json_output: bool = True) -> Any:
        argv = [self._binary(), *args]
        if json_output:
            argv.append("--json")
        if actor is not None:
            argv.extend(("--actor", actor))
        result = subprocess.run(
            argv,
            cwd=self.path,
            env=self._env(),
            text=True,
            capture_output=True,
            timeout=120,
            check=False,
        )
        if result.returncode:
            message = result.stderr.strip() or result.stdout.strip() or "unknown Beads error"
            raise BeadsError(message[:1000])
        if not json_output:
            return result.stdout.strip()
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise BeadsError("Beads returned invalid JSON") from exc

    def ensure(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        version = self._run("version", json_output=False)
        if re.search(r"\b1\.3\.1\b", version) is None:
            raise BeadsError(
                f"unsupported Beads version {version!r}; install bd v1.3.1 with "
                "`brew install beads` or `npm install -g @beads/bd`"
            )
        if not (self.path / ".beads" / "metadata.json").is_file():
            self._run(
                "init",
                "--non-interactive",
                "--skip-hooks",
                "--skip-agents",
                "--prefix",
                self.prefix,
                json_output=False,
            )

    def rows(self) -> dict[str, dict[str, Any]]:
        value = self._run("list", "--all", "--flat", "--limit", "0", "--max-rows", "10000")
        if not isinstance(value, list):
            raise BeadsError("Beads list returned an invalid shape")
        return {
            row["id"]: row
            for row in value
            if isinstance(row, dict) and self.local_id(row.get("id"))
        }

    def ready_ids(self) -> set[str]:
        value = self._run("list", "--ready", "--flat", "--limit", "0", "--max-rows", "10000")
        if not isinstance(value, list):
            raise BeadsError("Beads ready list returned an invalid shape")
        return {
            row["id"] for row in value if isinstance(row, dict) and self.local_id(row.get("id"))
        }

    def command(self, *args: str, actor: str) -> Any:
        return self._run(*args, actor=actor)
