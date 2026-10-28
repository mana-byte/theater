"""Mutation canaries: the live suite must notice each deliberate breakage of a plugin's wiring.

Slow and real-binary: enabled by the canary workflow. They run when the suite or a plugin changes
and weekly, never per release.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest

from harness_compat.adapters import load_adapter
from harness_compat.canaries import ENV_BINARY, ENV_ENABLE, ENV_HARNESS
from harness_compat.codecopy import code_copy
from harness_compat.contract import SUITE_ENV_MUTATION, Check
from harness_compat.sandbox import make_sandbox
from harness_compat.suite_runner import SuiteReport, installed_version, run_suite
from harness_compat.support.mutations import (
    ANY,
    LOADED_BANNER,
    MUTATIONS,
    Mutation,
    applicable,
    undecided,
)
from harness_compat.support.obligations import required
from tests.support.paths import REPO_ROOT
from theater.harness import HARNESSES
from theater.harness.builtin import plugin_dir

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get(ENV_ENABLE) != "1", reason="enabled by the canary workflow"),
]
#: A run that leaves a needed canary obligation at ``error`` (the bridge missed its start
#: deadline, a CLI never reached its prompt) proves nothing either way; it runs once more.
#: A miss never does.
_ATTEMPTS = 2
#: Read from disk: the registry is only populated after collection.
SHIPPED = sorted(path.parent.name for path in plugin_dir().glob("*/manifest.py"))
if selected := os.environ.get(ENV_HARNESS):
    if selected not in SHIPPED:
        raise pytest.UsageError(f"unknown canary harness: {selected}")
    SHIPPED = [selected]


def _binary(name: str, command: str) -> str | None:
    """The pipeline's installed release when it named this harness, else the one on PATH."""
    if os.environ.get(ENV_HARNESS) == name and os.environ.get(ENV_BINARY):
        return os.environ[ENV_BINARY]
    return shutil.which(command)


def _run(name: str, mutation: Mutation, binary: Path) -> tuple[SuiteReport, list[str]]:
    with make_sandbox(name) as sb, code_copy(REPO_ROOT) as tree:
        report = run_suite(
            tree,
            sb,
            harness=name,
            binary=binary,
            version=installed_version(binary),
            pytest_args=["-k", mutation.select],
            extra_env={SUITE_ENV_MUTATION: mutation.name},
        )
        logs = [
            log.read_text(encoding="utf-8", errors="replace")
            for log in sb.evidence_dir.rglob("daemon.log")
        ]
    return report, logs


def _checks(report: SuiteReport) -> dict[str, Check]:
    return {
        check.name.removeprefix("conformance:"): check
        for check in report.checks
        if check.name.startswith("conformance:") and check.status != "skip"
    }


@pytest.mark.parametrize("mutation", MUTATIONS, ids=lambda item: item.name)
@pytest.mark.parametrize("name", SHIPPED)
def test_the_suite_notices_a_deliberately_broken_plugin(name: str, mutation: Mutation) -> None:
    harness = HARNESSES[name]
    expected = applicable(mutation, required(harness), load_adapter(name).compat.canaries)
    if not expected:
        pytest.skip(f"{mutation.name} breaks nothing this harness declares")
    binary = _binary(name, harness.binary)
    if binary is None:
        pytest.skip(f"no {harness.binary!r} binary on PATH")

    for _ in range(_ATTEMPTS):
        report, logs = _run(name, mutation, Path(binary))
        checks = _checks(report)
        ran = {key: check.status for key, check in checks.items()}
        if not undecided(ran, expected):
            break

    banner = LOADED_BANNER % mutation.name
    assert any(banner in log for log in logs), (
        "the mutated plugin was never loaded by the daemon, so nothing was proven"
    )
    if mutation.effect and not any(re.search(mutation.effect, log) for log in logs):
        pytest.skip(f"{mutation.name} found nothing to break in the {name} launch plan")
    assert not report.problems
    if ANY in expected:
        assert any(status in ("fail", "error") for status in ran.values()), ran
        return
    for obligation, how in expected.items():
        allowed = {"fail"} if how == "fail" else {"fail", "error"}
        status = ran.get(obligation)
        assert status in allowed, (
            f"{obligation} was {status}: {checks[obligation].detail if status else ran}"
        )
    untouched = {key: value for key, value in ran.items() if key not in expected}
    assert all(value == "pass" for value in untouched.values()), {
        key: (value, checks[key].detail) for key, value in untouched.items() if value != "pass"
    }
