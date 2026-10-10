"""The synthetic write-stamped transcript writer behind `perf_baseline.py --synthetic`."""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
import threading
import time
import types
from argparse import Namespace
from pathlib import Path

import pytest

from theater.harness.contracts.callbacks import LaunchContext
from theater.harness.contracts.context import ParticipantObservationContext
from theater.harness.loading.discovery import discover
from theater.harness.loading.importer import load_plugin

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def writer():
    return _load("perf_synthetic_writer")


@pytest.fixture
def perf(monkeypatch):
    if importlib.util.find_spec("psutil") is None:  # dev script dependency, not a project one
        monkeypatch.setitem(sys.modules, "psutil", types.ModuleType("psutil"))
    return _load("perf_baseline")


def _write(writer, path: Path, rate: float, count: int):
    return writer.write_records(path, rate, stop=threading.Event(), count=count)


def test_records_are_stamped_at_write_and_rate_is_honored(writer, tmp_path: Path) -> None:
    path = tmp_path / ".synth" / "p1.jsonl"
    before = time.time()
    stats = _write(writer, path, rate=40.0, count=21)
    after = time.time()
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["seq"] for r in records] == list(range(21))
    assert all(r["type"] == "assistant" and r["text"] for r in records)
    stamps = [r["ts"] for r in records]
    assert before <= stamps[0] and stamps[-1] <= after and stamps == sorted(stamps)
    # Stamped at write, not precomputed: gaps follow the 25 ms schedule.
    assert 0.8 * 0.5 <= stamps[-1] - stamps[0] <= 1.3 * 0.5
    assert stats.written == 21 and stats.skipped == 0
    assert 0.8 * 40 <= stats.achieved_rate <= 1.2 * 40


def test_zero_rate_creates_an_empty_stream_and_idles(writer, tmp_path: Path) -> None:
    path = tmp_path / "idle.jsonl"
    stop = threading.Event()
    worker = threading.Thread(target=writer.write_records, args=(path, 0.0), kwargs={"stop": stop})
    worker.start()
    deadline = time.monotonic() + 5
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    worker.join(5)
    assert path.read_bytes() == b"" and not worker.is_alive()


def test_slow_writer_skips_missed_slots_instead_of_bursting(writer, tmp_path: Path) -> None:
    ticks = iter([0.0, 0.0, 0.0, 0.5, 0.5, 0.5, 0.5, 0.5])  # one slow iteration at rate 10/s
    stats = writer.write_records(
        tmp_path / "s.jsonl",
        10.0,
        stop=threading.Event(),
        count=2,
        monotonic=lambda: next(ticks, 0.5),
    )
    assert stats.written == 2 and stats.skipped >= 1


def test_synthetic_option_selects_plugin_and_validates(perf) -> None:
    args = Namespace(synthetic=0.8, attach=False, harness="claude", model="haiku")
    perf.apply_synthetic(args)
    assert (args.harness, args.model) == ("synth", None)
    assert perf.synthetic_prompt(0.8) == "rate=0.8"
    with pytest.raises(SystemExit):
        perf.apply_synthetic(Namespace(synthetic=0, attach=False))
    with pytest.raises(SystemExit):
        perf.apply_synthetic(Namespace(synthetic=1.0, attach=True))
    untouched = Namespace(synthetic=None, attach=False, harness="claude", model="haiku")
    perf.apply_synthetic(untouched)
    assert (untouched.harness, untouched.model) == ("claude", "haiku")


def test_install_pins_writer_and_plugin_compiles_and_plans(perf, tmp_path: Path) -> None:
    home, bin_dir = tmp_path / "home", tmp_path / "bin"
    bin_dir.mkdir()
    perf.install_synthetic_plugin(home, bin_dir)
    assert (bin_dir / "synth").stat().st_mode & 0o100
    (found,) = discover(home / "plugins", source="local")
    loaded = load_plugin(found)
    assert loaded.error is None and loaded.manifest is not None
    context = LaunchContext(
        participant_id="p1", prompt="rate=0.8", config_path=tmp_path / "c.json",
        approval="yolo", cwd=tmp_path / "work",
    )  # fmt: skip
    plan = loaded.manifest.launch.planner(context)
    assert plan.argv[1:] == [
        str(SCRIPTS / "perf_synthetic_writer.py"),
        *("--file", str(tmp_path / "work/.synth/p1.jsonl"), "--rate", "0.8"),
    ]
    assert plan.session_id == "p1"
    idle = loaded.manifest.launch.planner(
        LaunchContext(
            participant_id="p2",
            prompt="",
            config_path=tmp_path / "c.json",
            approval="yolo",
            cwd=tmp_path / "work",
        )
    )
    assert idle.argv[-1] == "0"


def test_records_parse_with_plugin_and_yield_write_to_bus_latency(
    perf, writer, tmp_path: Path
) -> None:
    (tmp_path / "bin").mkdir()
    perf.install_synthetic_plugin(tmp_path / "home", tmp_path / "bin")
    (found,) = discover(tmp_path / "home" / "plugins", source="local")
    manifest = load_plugin(found).manifest
    assert manifest is not None
    stream = tmp_path / "work" / ".synth" / "p1.jsonl"
    start = time.time()
    _write(writer, stream, rate=100.0, count=5)
    end = time.time()
    source = manifest.observation.primary.factory(
        ParticipantObservationContext(
            participant_id="p1", cwd=str(tmp_path / "work"), session_id="p1"
        )
    )
    observer = source._observer
    samples = []
    for index, line in enumerate(stream.read_text().splitlines()):
        (event,) = observer.parse(line, index)
        assert event.kind == "assistant" and start <= event.ts <= end
        payload = json.dumps({"ts": event.ts, "read_at": event.ts + 0.002})
        bus_ts = event.ts + 0.003  # what the daemon would stamp
        transcript_ms, observe_ms = perf._latency_samples(bus_ts, "agent.assistant", payload)
        samples.append((transcript_ms, observe_ms))
    assert all(
        t == pytest.approx(3.0, abs=0.01) and o == pytest.approx(1.0, abs=0.01) for t, o in samples
    )
    stats = perf.synthetic_stats(tmp_path / "work", 100.0, start, end)
    assert (stats.writers, stats.records) == (1, 5)
    assert perf.synthetic_stats(tmp_path / "work", 100.0, end + 1, end + 2).records == 0


def test_plugin_depends_only_on_apis_present_at_the_baseline_commit() -> None:
    """Baseline 34625699 has the same `contracts`/`transcript` surface; keep imports inside it."""
    allowed = ("theater.harness.contracts.", "theater.harness.transcript")
    for module in (SCRIPTS / "perf_synth_plugin").glob("*.py"):
        for node in ast.walk(ast.parse(module.read_text())):
            external = isinstance(node, ast.ImportFrom) and node.level == 0 and node.module
            if external and node.module.startswith("theater"):
                assert node.module.startswith(allowed), (module.name, node.module)
