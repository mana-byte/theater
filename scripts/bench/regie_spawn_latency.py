#!/usr/bin/env python3
"""Measure Régie palette spawn latency in an isolated Theater and tmux stack.

Run from any checkout while selecting the code under test explicitly:

    python scripts/bench/regie_spawn_latency.py --tree /path/to/checkout --runs 20
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import shlex
import shutil
import signal
import sqlite3
import statistics
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

POLL_SECONDS = 0.05
WAIT_SECONDS = 90
METRICS = (
    "admitted_ms",
    "settled_ms",
    "snapshot_ms",
    "rendered_ms",
    "focused_ms",
    "after_observation_ms",
)
LOG_TIME = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3})\s+.*$")
PHASE = re.compile(r"action\.spawn\.(admitted|observed|snapshot|rendered)\s+([\d.]+)ms\b(.*)")
OPERATION = re.compile(r"\boperation=([^\s]+)")
AFTER_OBSERVATION = re.compile(r"\bafter_observation_ms=([\d.]+)")
PANE_ID = re.compile(r"%([0-9]+)")


@dataclass(frozen=True, slots=True)
class Event:
    timestamp: datetime
    message: str


@dataclass(frozen=True, slots=True)
class Measurement:
    operation_id: str
    row: dict[str, float | int | str | None]
    rendered_line: int


class Stack:
    """Own one temporary Theater home, tmux server, and their child processes."""

    def __init__(self, tree: Path, keep_home: bool) -> None:
        self.tree = tree
        self.python = tree / ".venv" / "bin" / "python"
        self.keep_home = keep_home
        self.tmp = Path(tempfile.mkdtemp(prefix="rsb-", dir="/tmp"))
        self.home = self.tmp / "home"
        self.tmux_dir = self.tmp / "tmux"
        self.bin_dir = self.tmp / "bin"
        self.workdir = self.tmp / "work"
        for path in (self.home, self.tmux_dir, self.bin_dir, self.workdir):
            path.mkdir(mode=0o700)
        self.env = dict(os.environ)
        self.env.pop("TMUX", None)
        self.env.pop("TMUX_PANE", None)
        self.env.update(
            THEATER_HOME=str(self.home),
            TMUX_TMPDIR=str(self.tmux_dir),
            PATH=f"{self.bin_dir}{os.pathsep}{self.env.get('PATH', '')}",
        )
        self.tmux_binary = shutil.which("tmux")
        self.ui_pane: str | None = None
        self.ui_log: Path | None = None
        self.spawned: set[str] = set()
        self.stopped = False

    def theater(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.python), "-m", "theater.cli", *args],
            cwd=self.tree,
            env=self.env,
            capture_output=True,
            text=True,
            check=check,
            timeout=120,
        )

    def regie(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.python), "-m", "regie", *args],
            cwd=self.tree,
            env=self.env,
            capture_output=True,
            text=True,
            check=check,
            timeout=60,
        )

    def tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        assert self.tmux_binary is not None
        result = subprocess.run(
            [self.tmux_binary, "-f", "/dev/null", *args],
            cwd=self.tree,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        if check and result.returncode:
            raise RuntimeError(f"tmux {' '.join(args)} failed: {result.stderr.strip()}")
        return result

    def participants(self) -> dict[str, dict[str, object]]:
        result = self.theater("ls", "--json")
        rows = json.loads(result.stdout)["participants"]
        return {str(row["id"]): row for row in rows}

    def install_synth(self) -> None:
        source = self.tree / "scripts" / "perf_synth_plugin"
        writer = self.tree / "scripts" / "perf_synthetic_writer.py"
        if not source.is_dir() or not writer.is_file():
            raise RuntimeError(f"synthetic harness assets missing under {self.tree / 'scripts'}")
        stub = self.bin_dir / "synth"
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
        plugin = self.home / "plugins" / "synth"
        plugin.parent.mkdir(mode=0o700)
        shutil.copytree(source, plugin, ignore=shutil.ignore_patterns("__pycache__"))
        (plugin / "_paths.py").write_text(f"WRITER = {str(writer)!r}\n")

    def start(self, background: int) -> None:
        if self.tmux_binary is None:
            raise RuntimeError("tmux is not on PATH")
        if not self.python.is_file():
            raise RuntimeError(f"target checkout has no virtualenv Python: {self.python}")
        self.install_synth()
        self.theater("ls")  # This starts the daemon using the isolated home.
        command = " ".join(
            shlex.quote(value)
            for value in (
                str(self.python),
                "-m",
                "regie",
                "--socket",
                str(self.home / "var/run/daemon.sock"),
            )
        )
        self.tmux(
            "start-server",
            ";",
            "set-option",
            "-g",
            "remain-on-exit",
            "on",
            ";",
            "new-session",
            "-d",
            "-s",
            "bench",
            "-x",
            "200",
            "-y",
            "50",
            "-c",
            str(self.tree),
            command,
        )
        pane = self.tmux("display-message", "-p", "-t", "bench:0.0", "#{pane_id}").stdout.strip()
        match = PANE_ID.fullmatch(pane)
        if match is None:
            raise RuntimeError(f"could not identify Régie tmux pane: {pane!r}")
        self.ui_pane = pane
        self.ui_log = self.home / "regie/var/logs/ui" / f"pane-{match.group(1)}.log"
        self.wait_for(lambda: self.ui_log is not None and self.ui_log.is_file(), "Régie UI log")
        self.wait_for(lambda: bool(self.capture()), "Régie screen")
        for _ in range(background):
            result = self.theater(
                "spawn",
                "synth",
                "--approval",
                "yolo",
                "--cwd",
                str(self.workdir),
                "--json",
                check=False,
            )
            if result.returncode != 0:
                raise RuntimeError(f"background spawn failed: {result.stderr.strip()}")
            participant = json.loads(result.stdout)
            self.spawned.add(str(participant["id"]))
        expected = set(self.participants())
        if len(expected) < background:
            raise RuntimeError(
                f"expected {background} background participants, found {len(expected)}"
            )
        self.wait_for(lambda: self.is_clean(expected), "Régie idle after background spawns")

    def seed_history(self, workspaces: int, usage_rows: int) -> None:
        """Add inert rows a long-lived home accumulates; every full snapshot reads them all."""
        database = sqlite3.connect(self.home / "var/state/theater.db", timeout=30)
        stamp = time.time()
        with contextlib.closing(database), database:
            database.executemany(
                "INSERT INTO workspaces (workspace_id, ownership_kind, owner_id, path, state,"
                " created_at, updated_at) VALUES (?, 'borrowed', 'local_operator', ?, 'active',"
                " ?, ?)",
                [
                    (f"seed{index:08d}", str(self.tmp / "seed" / str(index)), stamp, stamp)
                    for index in range(workspaces)
                ],
            )
            database.executemany(
                "INSERT INTO usage (participant_id, usage_key, ts, harness, input_tokens,"
                " output_tokens, cost_microcents) VALUES ('seed', ?, ?, 'codex', 1000, 100, 5000)",
                [(str(index), stamp - index) for index in range(usage_rows)],
            )

    def capture(self) -> str:
        if self.ui_pane is None:
            return ""
        result = self.tmux("capture-pane", "-p", "-t", self.ui_pane, check=False)
        return result.stdout if result.returncode == 0 else ""

    def current_pane(self) -> str:
        result = self.tmux("display-message", "-p", "-t", "bench", "#{pane_id}", check=False)
        return result.stdout.strip() if result.returncode == 0 else ""

    def is_clean(self, expected_ids: set[str]) -> bool:
        screen = self.capture().casefold()
        return (
            set(self.participants()) == expected_ids
            and self.current_pane() == self.ui_pane
            and "esc cancel" not in screen
        )

    def wait_for(
        self, predicate: Callable[[], bool], label: str, timeout: float = WAIT_SECONDS
    ) -> None:
        deadline = time.monotonic() + timeout
        while not predicate():
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out waiting for {label}; pane:\n{self.capture()}")
            time.sleep(POLL_SECONDS)

    def run_ui_spawn(
        self, run_number: int, expected_ids: set[str]
    ) -> dict[str, float | int | str | None]:
        assert self.ui_pane is not None and self.ui_log is not None
        offset = self.ui_log.stat().st_size
        self.tmux("send-keys", "-t", self.ui_pane, "C-p")
        self.wait_for(
            lambda: "search for commands" in self.capture().casefold(),
            "root command palette",
        )
        self.tmux("send-keys", "-l", "-t", self.ui_pane, "spawn")
        self.tmux("send-keys", "-t", self.ui_pane, "Enter")
        self.wait_for(lambda: "spawn synth" in self.capture().casefold(), "synth palette choice")
        self.tmux("send-keys", "-t", self.ui_pane, "C-a")
        self.tmux("send-keys", "-l", "-t", self.ui_pane, "synth")
        self.tmux("send-keys", "-t", self.ui_pane, "Enter")
        self.wait_for(
            lambda: "esc cancel" in self.capture().casefold(),
            "spawn directory editor",
        )
        self.tmux("send-keys", "-t", self.ui_pane, "C-a")
        self.tmux("send-keys", "-t", self.ui_pane, "C-k")
        self.tmux("send-keys", "-l", "-t", self.ui_pane, str(self.workdir))
        self.wait_for(
            lambda: str(self.workdir) in self.capture(),
            "temporary spawn directory",
        )
        self.tmux("send-keys", "-t", self.ui_pane, "Enter")
        measurement = self.wait_for_measurement(offset)
        participant_ids = self.wait_for_new_participant(expected_ids)
        if len(participant_ids) != 1:
            raise RuntimeError(
                f"run {run_number}: expected one new participant, found {participant_ids}"
            )
        participant_id = next(iter(participant_ids))
        measurement["run"] = run_number
        measurement["participant_id"] = participant_id
        self.spawned.add(participant_id)
        self.theater("kill", participant_id)
        self.wait_for(lambda: self.is_clean(expected_ids), f"tree clean after run {run_number}")
        return measurement

    def wait_for_measurement(self, offset: int) -> dict[str, float | int | str | None]:
        assert self.ui_log is not None
        measurement: Measurement | None = None

        def found() -> bool:
            nonlocal measurement
            with self.ui_log.open("r", encoding="utf-8", errors="replace") as log:
                log.seek(offset)
                measurement = parse_measurement(log.read().splitlines())
            return measurement is not None

        self.wait_for(found, "spawn rendered and focused")
        assert measurement is not None
        return measurement.row

    def wait_for_new_participant(self, expected_ids: set[str]) -> set[str]:
        found: set[str] = set()

        def appeared() -> bool:
            nonlocal found
            found = set(self.participants()) - expected_ids
            return bool(found)

        self.wait_for(appeared, "spawned participant")
        return found

    def cleanup(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        for participant_id in set(self.spawned) | set(self._listed_participant_ids()):
            self._cleanup_step(
                lambda participant_id=participant_id: self.theater(
                    "kill", participant_id, check=False
                )
            )
        self._cleanup_step(lambda: self.regie("bridge", "stop", check=False))
        self._cleanup_step(lambda: self.theater("stop", check=False))
        if self.tmux_binary is not None:
            self._cleanup_step(lambda: self.tmux("kill-server", check=False))
        self._cleanup_step(self._kill_leftovers)
        if not self.keep_home:
            shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _cleanup_step(action: Callable[[], object]) -> None:
        with contextlib.suppress(BaseException):
            action()

    def _listed_participant_ids(self) -> set[str]:
        try:
            return set(self.participants())
        except BaseException:
            return set()

    def _kill_leftovers(self) -> None:
        result = subprocess.run(
            ["ps", "-axo", "pid=,command="],
            cwd=self.tree,
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
        pids = []
        for line in result.stdout.splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) == 2 and str(self.tmp) in fields[1]:
                try:
                    pid = int(fields[0])
                except ValueError:
                    continue
                if pid == os.getpid():
                    continue
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGTERM)
                    pids.append(pid)
        deadline = time.monotonic() + 2
        while pids and time.monotonic() < deadline:
            pids = [pid for pid in pids if _process_exists(pid)]
            if pids:
                time.sleep(POLL_SECONDS)
        for pid in pids:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _event(line: str) -> Event | None:
    match = LOG_TIME.match(line)
    if match is None:
        return None
    timestamp = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S,%f")
    return Event(timestamp, line[match.end(1) :].strip())


def parse_measurement(lines: list[str]) -> Measurement | None:
    events = [(index, event) for index, line in enumerate(lines) if (event := _event(line))]
    admitted = None
    operation_id = None
    admitted_ms = None
    for index, event in events:
        match = PHASE.search(event.message)
        if match is not None and match.group(1) == "admitted":
            operation = OPERATION.search(match.group(3))
            if operation is not None:
                admitted = (index, event.timestamp)
                operation_id = operation.group(1)
                admitted_ms = float(match.group(2))
                break
    if admitted is None or operation_id is None or admitted_ms is None:
        return None
    phases: dict[str, tuple[int, Event, float]] = {}
    rendered_after_observation = None
    rendered_line = -1
    focused = None
    for index, event in events:
        match = PHASE.search(event.message)
        if match is not None and OPERATION.search(match.group(3)):
            operation = OPERATION.search(match.group(3))
            assert operation is not None
            if operation.group(1) == operation_id:
                phase = match.group(1)
                phases.setdefault(phase, (index, event, float(match.group(2))))
                if phase == "rendered":
                    rendered_line = index
                    after = AFTER_OBSERVATION.search(match.group(3))
                    if after is not None:
                        rendered_after_observation = float(after.group(1))
        elif index > rendered_line >= 0 and "presentation.open.execute" in event.message:
            if focused is None:
                focused = event.timestamp
    required = {"observed", "rendered"}
    if not required.issubset(phases) or focused is None or rendered_after_observation is None:
        return None
    _rendered_index, rendered_event, rendered_ms = phases["rendered"]
    observed_ms = phases["observed"][2]
    snapshot_ms = phases.get("snapshot", (0, rendered_event, None))[2]
    submitted_at = admitted[1] - timedelta(milliseconds=admitted_ms)
    focused_ms = (focused - submitted_at).total_seconds() * 1000
    row: dict[str, float | int | str | None] = {
        "operation_id": operation_id,
        "admitted_ms": admitted_ms,
        "settled_ms": observed_ms,
        "snapshot_ms": snapshot_ms,
        "rendered_ms": rendered_ms,
        "focused_ms": focused_ms,
        "after_observation_ms": rendered_after_observation,
    }
    return Measurement(operation_id, row, rendered_line)


def summarize(rows: list[dict[str, float | int | str | None]]) -> dict[str, object]:
    summary: dict[str, object] = {}
    for metric in METRICS:
        values = sorted(float(row[metric]) for row in rows if row[metric] is not None)
        if not values:
            summary[metric] = None
            continue
        summary[metric] = {
            "median": round(statistics.median(values), 1),
            "p90": round(values[math.ceil(0.9 * len(values)) - 1], 1),
            "mean": round(statistics.mean(values), 1),
            "min": round(min(values), 1),
            "max": round(max(values), 1),
        }
    return summary


def print_text(result: dict[str, object]) -> None:
    print("metric                  median     p90    mean     min     max (ms)")
    summary = result["summary"]
    assert isinstance(summary, dict)
    for metric in METRICS:
        stats = summary[metric]
        if stats is None:
            print(f"{metric:24} no samples")
            continue
        assert isinstance(stats, dict)
        print(
            f"{metric:24} {stats['median']:>6.1f} {stats['p90']:>7.1f}"
            f" {stats['mean']:>7.1f} {stats['min']:>7.1f} {stats['max']:>7.1f}"
        )
    print(f"runs without snapshot phase: {result['no_snapshot_runs']}/{result['runs']}")


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "--tree", type=Path, required=True, help="checkout under test (must have .venv)"
    )
    result.add_argument("--runs", type=int, default=20)
    result.add_argument("--warmup", type=int, default=2)
    result.add_argument("--background", type=int, default=6)
    result.add_argument(
        "--workspaces", type=int, default=0, help="inert workspaces to seed (a real home: ~150)"
    )
    result.add_argument(
        "--usage-rows", type=int, default=0, help="inert usage rows to seed (a real home: ~160k)"
    )
    result.add_argument("--json", action="store_true")
    result.add_argument("--keep-home", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    if args.runs < 1 or args.warmup < 0 or args.background < 0:
        parser().error("--runs must be positive; --warmup and --background cannot be negative")
    tree = args.tree.expanduser().resolve()
    stack = Stack(tree, args.keep_home)
    try:
        stack.start(args.background)
        stack.seed_history(args.workspaces, args.usage_rows)
        expected_ids = set(stack.participants())
        samples = []
        total = args.warmup + args.runs
        for index in range(total):
            row = stack.run_ui_spawn(index + 1, expected_ids)
            if index >= args.warmup:
                samples.append(row)
        result: dict[str, object] = {
            "tree": str(tree),
            "runs": args.runs,
            "warmup": args.warmup,
            "background": args.background,
            "workspaces": args.workspaces,
            "usage_rows": args.usage_rows,
            "summary": summarize(samples),
            "no_snapshot_runs": sum(row["snapshot_ms"] is None for row in samples),
            "samples": samples,
        }
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print_text(result)
        return 0
    finally:
        stack.cleanup()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit("interrupted") from None
