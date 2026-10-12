"""Run one deterministic, duration-balanced CI test shard."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARD_COUNT = 3
TEST_ROOTS = (ROOT / "tests", ROOT / "packages/regie/tests")

# Files taking at least five seconds in the 2026-10-10 3.12 CI run; others get one second.
SLOW_SECONDS = {
    "tests/test_opencode_http.py": 60.2,
    "packages/regie/tests/test_ui.py": 50.6,
    "packages/regie/tests/test_usage.py": 44.1,
    "tests/test_daemon.py": 33.1,
    "packages/regie/tests/test_ambience.py": 21.5,
    "tests/test_runtime_live_recovery.py": 21.3,
    "tests/test_runtime_lifecycle_integration.py": 21.2,
    "tests/test_presence_await.py": 20.3,
    "tests/test_opencode_frontend.py": 18.2,
    "tests/test_daemon_lock.py": 15.8,
    "tests/test_mcp_server.py": 13.7,
    "tests/test_store_rpc.py": 13.0,
    "tests/test_control_rpc.py": 12.7,
    "tests/test_observability_runtime.py": 12.1,
    "tests/test_claude_receipts.py": 11.5,
    "tests/test_observer_lifecycle.py": 11.4,
    "tests/test_rc10_provider_controls.py": 10.3,
    "tests/test_registry.py": 10.3,
    "tests/test_store.py": 10.0,
    "tests/test_send.py": 9.9,
    "tests/test_control_service.py": 9.7,
    "tests/test_codex_native_runtime_proof.py": 9.2,
    "packages/regie/tests/test_rich_trajectory_view.py": 8.9,
    "tests/test_live_observation_integration.py": 8.9,
    "tests/test_runtime_storage.py": 8.5,
    "tests/test_rc10_workspaces.py": 8.4,
    "tests/test_observer.py": 7.7,
    "packages/regie/tests/test_tree_organization.py": 7.5,
    "tests/test_opencode_v2_drain.py": 7.3,
    "tests/test_jobs.py": 7.2,
    "tests/test_claude_native_control_proof.py": 7.1,
    "tests/test_recall.py": 6.9,
    "tests/test_resume_spawn.py": 6.4,
    "tests/test_gc.py": 6.4,
    "tests/test_harness_otel_channel.py": 6.2,
    "tests/test_transcript_collision.py": 6.1,
    "tests/test_stack_d.py": 5.9,
    "tests/test_rc10_provider_fencing.py": 5.8,
    "tests/test_bus_trajectory_foundation.py": 5.8,
    "tests/test_rc10_state_stream.py": 5.6,
    "tests/test_rc10_artifacts.py": 5.6,
    "tests/test_mcp_plugin_runtime.py": 5.3,
}


def test_files() -> list[str]:
    return sorted(
        str(path.relative_to(ROOT)) for root in TEST_ROOTS for path in root.rglob("test_*.py")
    )


def shards(files: list[str]) -> list[list[str]]:
    groups: list[list[str]] = [[] for _ in range(SHARD_COUNT)]
    weights = [0.0] * SHARD_COUNT
    for path in sorted(files, key=lambda name: (-SLOW_SECONDS.get(name, 1.0), name)):
        index = min(range(SHARD_COUNT), key=lambda index: (weights[index], index))
        groups[index].append(path)
        weights[index] += SLOW_SECONDS.get(path, 1.0)
    return [sorted(group) for group in groups]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard", type=int, choices=range(1, SHARD_COUNT + 1), required=True)
    parser.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    extra = args.pytest_args
    if extra and extra[0] == "--":
        extra = extra[1:]
    selected = shards(test_files())[args.shard - 1]
    if not selected:
        parser.error(f"shard {args.shard} contains no tests")
    print(f"CI test shard {args.shard}/{SHARD_COUNT}: {len(selected)} files", flush=True)
    return subprocess.call([sys.executable, "-m", "pytest", *extra, *selected], cwd=ROOT)


if __name__ == "__main__":
    raise SystemExit(main())
