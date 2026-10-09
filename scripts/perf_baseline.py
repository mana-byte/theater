#!/usr/bin/env python3
"""Dev-only CPU/latency baseline for the daemon, the Régie bridge, and the Régie UI.

Not part of the test suite. Run from the repo root:

    uv run --all-packages --with psutil scripts/perf_baseline.py           # isolated, spawns
    uv run --all-packages --with psutil scripts/perf_baseline.py --attach  # running stack
    sudo -E uv run --all-packages --with psutil scripts/perf_baseline.py --py-spy  # macOS

Spawn mode builds a throwaway THEATER_HOME and tmux server (TMUX_TMPDIR), puts a tmux shim on
PATH to count every tmux fork, starts Régie inside a detached pane, then measures each scenario.
Metric names are the shared contract with docs/perf-baseline.md — keep them stable.
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

try:
    import psutil
except ImportError:  # pragma: no cover - dev script
    sys.exit("perf_baseline: psutil missing; run with `uv run --all-packages --with psutil ...`")

REPO = Path(__file__).resolve().parent.parent
ROLES = ("daemon", "bridge", "regie", "tmux")
DEFAULT_WORKING_PROMPT = (
    "This is a load-generation run. Using your shell tool, run `sleep 3; date` and report the "
    "output, then repeat that exact step again. Keep repeating until you have done it 200 times. "
    "Do not edit any files."
)
SHIM = """#!/bin/sh
printf '%s\\n' "$*" >> "{log}"
exec "{real}" "$@"
"""


@dataclass
class Scenario:
    name: str
    idle: int
    working: int


@dataclass
class RoleSample:
    pid: int
    cpu_seconds: float = 0.0
    percent: list[float] = field(default_factory=list)
    rss_mb: float = 0.0


@dataclass
class ScenarioResult:
    scenario: Scenario
    seconds: float
    roles: dict[str, RoleSample]
    tmux_forks: int | None
    tmux_by_command: Counter
    bus_kinds: Counter
    journal_kinds: Counter
    latency_ms: list[float]
    profiles: dict[str, list[tuple[str, int]]]
    profile_errors: dict[str, str]


class Stack:
    """The processes under measurement and the environment that reaches them."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.attach = args.attach
        self.tmp: Path | None = None
        self.env = dict(os.environ)
        self.tmux_log: Path | None = None
        self.real_tmux = shutil.which("tmux")
        self.spawned: list[str] = []
        if not self.attach:
            self.tmp = Path(tempfile.mkdtemp(prefix="theater-perf-"))
            home, tmux_dir, shim_dir = (self.tmp / n for n in ("home", "tmux", "bin"))
            for path in (home, tmux_dir, shim_dir):
                path.mkdir(mode=0o700)
            if args.config.is_file():  # model allowlists etc. live here
                shutil.copy(args.config, home / "config.toml")
            self.tmux_log = self.tmp / "tmux-forks.log"
            self.tmux_log.touch()
            if self.real_tmux is None:
                sys.exit("perf_baseline: tmux is not on PATH")
            shim = shim_dir / "tmux"
            shim.write_text(SHIM.format(log=self.tmux_log, real=self.real_tmux))
            shim.chmod(0o755)
            self.env.pop("TMUX", None)
            self.env.pop("TMUX_PANE", None)
            self.env.update(
                THEATER_HOME=str(home),
                TMUX_TMPDIR=str(tmux_dir),
                PATH=f"{shim_dir}{os.pathsep}{self.env.get('PATH', '')}",
            )
        self.home = Path(self.env.get("THEATER_HOME", Path.home() / ".theater"))
        self.workdir = self.tmp / "work" if self.tmp else Path.cwd()

    def theater(self, *argv: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "theater.cli", *argv],
            env=self.env,
            capture_output=True,
            text=True,
            check=check,
            timeout=120,
        )

    def start(self) -> None:
        if self.attach:
            return
        assert self.real_tmux is not None
        self.workdir.mkdir()
        subprocess.run(["git", "init", "-q", str(self.workdir)], check=True)
        subprocess.run(  # spawn inspects repository identity, which needs a HEAD
            ["git", "-C", str(self.workdir), "commit", "-q", "--allow-empty", "-m", "perf"],
            check=True,
        )
        self.theater("ls")  # autostarts the daemon under the isolated home
        regie = f"{sys.executable} -m regie --socket {self.home / 'var/run/daemon.sock'}"
        subprocess.run(
            [
                self.real_tmux,
                *("-f", "/dev/null", "start-server", ";"),
                *("set-option", "-g", "remain-on-exit", "on", ";"),
                *("new-session", "-d", "-s", "perf", "-x", "200", "-y", "50"),
                *("-c", str(self.workdir), regie),
            ],
            env=self.env,
            check=True,
        )
        try:
            self._wait(lambda: all(self.find(role) for role in ROLES), 60, "the stack")
        except SystemExit:
            missing = [role for role in ROLES if self.find(role) is None]
            pane = subprocess.run(
                [self.real_tmux, "capture-pane", "-p", "-t", "perf"],
                env=self.env,
                capture_output=True,
                text=True,
                check=False,
            )
            sys.exit(f"perf_baseline: missing {missing}; pane:\n{pane.stdout}{pane.stderr}")

    def spawn(self, count: int, prompt: str | None) -> None:
        for _ in range(count):
            argv = ["spawn", self.args.harness, "--approval", "yolo", "--cwd", str(self.workdir)]
            if self.args.model:
                argv += ["--model", self.args.model]
            argv += ["--json"] + ([prompt] if prompt else [])
            deadline = time.monotonic() + 60
            while True:
                done = self.theater(*argv, check=False)
                if done.returncode == 0:
                    with contextlib.suppress(ValueError, KeyError):
                        self.spawned.append(json.loads(done.stdout)["id"])
                    break
                if time.monotonic() > deadline:
                    sys.exit(f"perf_baseline: spawn failed: {done.stderr.strip()}")
                time.sleep(2)

    def find(self, role: str) -> psutil.Process | None:
        override = getattr(self.args, f"{role}_pid")
        if override:
            return psutil.Process(override)
        if role == "daemon":
            pidfile = self.home / "var/run/daemon.pid"
            with contextlib.suppress(OSError, ValueError, psutil.Error):
                return psutil.Process(int(pidfile.read_text().split()[0]))
        home = str(self.home)
        for proc in psutil.process_iter(["cmdline", "name", "ppid"]):
            argv = list(proc.info["cmdline"] or ())
            if role == "tmux":
                server = proc.info["name"] == "tmux" and proc.info["ppid"] == 1
                if server and (self.attach or "perf" in argv):
                    return proc
                continue
            if not _runs_regie(argv):
                continue
            ours = self.attach or any(home in arg for arg in argv)
            is_bridge = "_bridge-worker" in argv
            if ours and is_bridge == (role == "bridge") and "bridge" not in argv:
                return proc
        return None

    def tmux_lines(self) -> list[str]:
        if self.tmux_log is None:
            return []
        return self.tmux_log.read_text().splitlines()

    def stop(self) -> None:
        if self.attach or self.tmp is None:
            return
        for participant in self.spawned:
            self.theater("kill", participant, check=False)
        self.theater("stop", check=False)
        if self.real_tmux:
            subprocess.run(
                [self.real_tmux, "kill-server"], env=self.env, capture_output=True, check=False
            )
        if not self.args.keep_home:
            shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _wait(predicate, seconds: float, label: str) -> None:
        deadline = time.monotonic() + seconds
        while not predicate():
            if time.monotonic() > deadline:
                raise SystemExit(f"perf_baseline: timed out waiting for {label}")
            time.sleep(0.5)


def _runs_regie(argv: list[str]) -> bool:
    """`python -m regie ...` or the `regie` console script — not a prompt mentioning it."""
    if argv and Path(argv[0]).name == "regie":
        return True
    return any(a == "-m" and b == "regie" for a, b in itertools.pairwise(argv))


def _tmux_subcommand(line: str) -> str:
    words = iter(line.split())
    for word in words:
        if word in {"-S", "-L", "-f"}:
            next(words, None)
        elif not word.startswith("-"):
            return word
    return "(none)"


def _db(stack: Stack) -> sqlite3.Connection:
    path = stack.home / "var/state/theater.db"
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)


def _cursors(stack: Stack) -> tuple[int, int]:
    with contextlib.closing(_db(stack)) as db:
        bus = db.execute("SELECT COALESCE(MAX(id), 0) FROM bus").fetchone()[0]
        journal = db.execute("SELECT COALESCE(MAX(sequence), 0) FROM orchestration_events")
        return bus, journal.fetchone()[0]


def _events(stack: Stack, bus_after: int, journal_after: int):
    bus_kinds: Counter = Counter()
    latency: list[float] = []
    with contextlib.closing(_db(stack)) as db:
        rows = db.execute("SELECT ts, kind, payload FROM bus WHERE id > ?", (bus_after,))
        for ts, kind, payload in rows:
            bus_kinds[kind] += 1
            if not kind.startswith("agent."):
                continue
            with contextlib.suppress(ValueError, TypeError, KeyError):
                source_ts = json.loads(payload)["ts"]
                if isinstance(source_ts, (int, float)):
                    latency.append((ts - source_ts) * 1000)
        journal = Counter(
            dict(
                db.execute(
                    "SELECT kind, COUNT(*) FROM orchestration_events WHERE sequence > ? "
                    "GROUP BY kind",
                    (journal_after,),
                ).fetchall()
            )
        )
    return bus_kinds, journal, latency


def _start_profiles(stack: Stack, procs: dict[str, psutil.Process], seconds: float, out: Path):
    if not stack.args.py_spy:
        return {}
    pyspy = shutil.which("py-spy")
    started = {}
    for role, proc in procs.items():
        if role == "tmux":
            continue
        target = out / f"{role}.raw.txt"
        if pyspy is None:
            started[role] = (None, target, "py-spy not on PATH")
            continue
        argv = [pyspy, "record", "--nonblocking", "--format", "raw", "-r", "50"]
        argv += ["-d", str(int(seconds)), "-p", str(proc.pid), "-o", str(target)]
        started[role] = (
            subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True),
            target,
            "",
        )
    return started


def _finish_profiles(started) -> tuple[dict[str, list[tuple[str, int]]], dict[str, str]]:
    profiles: dict[str, list[tuple[str, int]]] = {}
    errors: dict[str, str] = {}
    for role, (popen, target, error) in started.items():
        if popen is not None:
            _, stderr = popen.communicate(timeout=60)
            if popen.returncode != 0:
                tail = (stderr or "").strip().splitlines()[-1:]
                errors[role] = tail[0] if tail else f"exit {popen.returncode}"
                continue
        if error or not target.exists():
            errors[role] = error or "no profile written"
            continue
        self_time: Counter = Counter()
        for line in target.read_text().splitlines():
            stack_text, _, count = line.rpartition(" ")
            if stack_text and count.isdigit():
                self_time[stack_text.rsplit(";", 1)[-1]] += int(count)
        profiles[role] = self_time.most_common(12)
    return profiles, errors


def measure(stack: Stack, scenario: Scenario, out: Path) -> ScenarioResult:
    procs = {role: proc for role in ROLES if (proc := stack.find(role)) is not None}
    time.sleep(stack.args.warmup)
    bus_after, journal_after = _cursors(stack)
    tmux_before = len(stack.tmux_lines())
    samples = {role: RoleSample(pid=proc.pid) for role, proc in procs.items()}
    start_cpu = {}
    for role, proc in procs.items():
        times = proc.cpu_times()
        start_cpu[role] = times.user + times.system
        proc.cpu_percent(None)
    profiling = _start_profiles(stack, procs, stack.args.duration, out)
    started = time.monotonic()
    while time.monotonic() - started < stack.args.duration:
        time.sleep(1.0)
        for role, proc in procs.items():
            with contextlib.suppress(psutil.Error):
                samples[role].percent.append(proc.cpu_percent(None))
    elapsed = time.monotonic() - started
    for role, proc in procs.items():
        with contextlib.suppress(psutil.Error):
            times = proc.cpu_times()
            samples[role].cpu_seconds = times.user + times.system - start_cpu[role]
            samples[role].rss_mb = proc.memory_info().rss / 2**20
    forks = stack.tmux_lines()[tmux_before:]
    bus_kinds, journal_kinds, latency = _events(stack, bus_after, journal_after)
    profiles, profile_errors = _finish_profiles(profiling)
    return ScenarioResult(
        scenario=scenario,
        seconds=elapsed,
        roles=samples,
        tmux_forks=None if stack.attach else len(forks),
        tmux_by_command=Counter(_tmux_subcommand(line) for line in forks),
        bus_kinds=bus_kinds,
        journal_kinds=journal_kinds,
        latency_ms=latency,
        profiles=profiles,
        profile_errors=profile_errors,
    )


def _pct(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def _per_hour(count: int, seconds: float) -> float:
    return count * 3600 / seconds if seconds else 0.0


def render(results: list[ScenarioResult], args: argparse.Namespace) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    commit = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        cwd=REPO,
        check=False,
    ).stdout.strip()
    lines = [
        "# Theater performance baseline",
        "",
        f"Generated by `scripts/perf_baseline.py` at {stamp} on commit `{commit}`.",
        f"Mode: {'attach (existing stack)' if args.attach else 'isolated spawn'}; "
        f"harness `{args.harness}`; warmup {args.warmup:.0f}s; window {args.duration:.0f}s; "
        f"host {os.uname().sysname} {os.uname().machine}, {psutil.cpu_count()} CPUs.",
        "",
        "CPU % is one core = 100 %. `cpu_pct_mean` = process CPU seconds / wall seconds.",
        "",
        "## Summary",
        "",
        "| scenario | role | cpu_pct_mean | cpu_pct_p95 | rss_mb |",
        "|---|---|---:|---:|---:|",
    ]
    for result in results:
        for role in ROLES:
            sample = result.roles.get(role)
            if sample is None:
                lines.append(f"| {result.scenario.name} | {role} | n/a | n/a | n/a |")
                continue
            mean = 100 * sample.cpu_seconds / result.seconds
            lines.append(
                f"| {result.scenario.name} | {role} | {mean:.2f} | "
                f"{_pct(sample.percent, 0.95):.1f} | {sample.rss_mb:.0f} |"
            )
    lines += [
        "",
        "| scenario | tmux_forks_per_s | bus_events_per_h | journal_events_per_h | "
        "controls_changed_per_h_per_agent | transcript_to_bus_p50_ms | "
        "transcript_to_bus_p95_ms |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        agents = max(1, result.scenario.idle + result.scenario.working)
        forks = "n/a" if result.tmux_forks is None else f"{result.tmux_forks / result.seconds:.2f}"
        controls = result.journal_kinds.get("participant.controls_changed", 0)
        lines.append(
            f"| {result.scenario.name} | {forks} | "
            f"{_per_hour(sum(result.bus_kinds.values()), result.seconds):.0f} | "
            f"{_per_hour(sum(result.journal_kinds.values()), result.seconds):.0f} | "
            f"{_per_hour(controls, result.seconds) / agents:.1f} | "
            f"{_pct(result.latency_ms, 0.5):.1f} | {_pct(result.latency_ms, 0.95):.1f} |"
        )
    for result in results:
        lines += ["", f"## Scenario `{result.scenario.name}`", ""]
        lines.append(
            f"{result.scenario.idle} idle + {result.scenario.working} working agents, "
            f"{result.seconds:.0f}s window, {len(result.latency_ms)} latency samples "
            f"(max {max(result.latency_ms, default=float('nan')):.0f} ms)."
        )
        for title, counter in (
            ("Journal events/hour by kind", result.journal_kinds),
            ("Bus events/hour by kind", result.bus_kinds),
            ("tmux forks/hour by subcommand", result.tmux_by_command),
        ):
            if not counter:
                continue
            lines += ["", f"### {title}", "", "| kind | per hour |", "|---|---:|"]
            for kind, count in counter.most_common():
                lines.append(f"| `{kind}` | {_per_hour(count, result.seconds):.0f} |")
        for role, top in result.profiles.items():
            total = sum(count for _, count in top) or 1
            lines += ["", f"### py-spy top self frames — {role}", "", "| frame | share |"]
            lines.append("|---|---:|")
            for frame, count in top:
                lines.append(f"| `{frame.replace('|', '/')}` | {100 * count / total:.1f}% |")
        for role, error in result.profile_errors.items():
            lines.append(f"\npy-spy {role}: unavailable ({error}).")
    lines += [
        "",
        "## Caveats",
        "",
        "- Spawn mode runs Régie in a detached tmux session: no attached client, so presence "
        "is absent and nothing repaints a real terminal.",
        "- `transcript_to_bus` is bus `ts` minus the harness record `ts`; it includes harness "
        "write delay and clock granularity (ms).",
        "- CPU excludes short-lived children (`ps`, `lsof`, `tmux`); count them via forks.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--attach", action="store_true", help="measure the running stack")
    parser.add_argument("--idle", type=int, default=10)
    parser.add_argument("--working", type=int, default=5)
    parser.add_argument("--harness", default="claude")
    parser.add_argument("--model", default="haiku")
    parser.add_argument("--working-prompt", default=DEFAULT_WORKING_PROMPT)
    parser.add_argument("--warmup", type=float, default=20.0)
    parser.add_argument("--duration", type=float, default=120.0)
    parser.add_argument("--py-spy", action="store_true", help="profile (macOS needs sudo)")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("THEATER_HOME", Path.home() / ".theater")) / "config.toml",
        help="config.toml copied into the isolated home",
    )
    parser.add_argument("--keep-home", action="store_true", help="keep the temp home")
    parser.add_argument("--out", type=Path, default=REPO / "docs/perf-baseline.md")
    for role in ROLES:
        parser.add_argument(f"--{role}-pid", type=int, default=None)
    args = parser.parse_args()

    stack = Stack(args)
    artifacts = args.out.with_suffix("")
    artifacts.mkdir(parents=True, exist_ok=True)
    results: list[ScenarioResult] = []
    try:
        stack.start()
        if args.attach:
            scenarios = [Scenario("attached", 0, 0)]
        else:
            scenarios = [Scenario("empty", 0, 0)]
            if args.idle:
                scenarios.append(Scenario(f"idle{args.idle}", args.idle, 0))
            if args.working:
                scenarios.append(
                    Scenario(f"idle{args.idle}+working{args.working}", args.idle, args.working)
                )
        for scenario in scenarios:
            previous = results[-1].scenario if results else Scenario("", 0, 0)
            stack.spawn(scenario.idle - previous.idle, None)
            stack.spawn(scenario.working - previous.working, args.working_prompt)
            print(f"perf_baseline: measuring {scenario.name}", file=sys.stderr)
            results.append(measure(stack, scenario, artifacts))
    finally:
        stack.stop()
    args.out.write_text(render(results, args))
    print(f"perf_baseline: wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
