"""Launch the write-stamping writer; the prompt `rate=<records/s>` picks the rate (none = idle)."""

import re
import sys
from pathlib import Path

from theater.harness.contracts.callbacks import LaunchContext
from theater.harness.contracts.launch import LaunchPlan

from ._paths import WRITER

STREAM_DIR = ".synth"


def stream_path(cwd: str | Path, session_id: str) -> Path:
    return Path(cwd) / STREAM_DIR / f"{session_id}.jsonl"


def plan_launch(context: LaunchContext) -> LaunchPlan:
    if context.cwd is None:
        raise ValueError("synth harness needs an explicit --cwd")
    match = re.fullmatch(r"rate=([0-9.]+)", (context.prompt or "").strip())
    rate = match.group(1) if match else "0"
    path = stream_path(context.cwd, context.participant_id)
    return LaunchPlan(
        argv=[sys.executable, WRITER, "--file", str(path), "--rate", rate],
        session_id=context.participant_id,
    )
