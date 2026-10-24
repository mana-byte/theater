"""Mutation canaries: the live suite must notice each deliberate breakage of a plugin's wiring.

Slow and real-binary: opt in with ``make canaries`` (``HARNESS_COMPAT_CANARIES=1``). They run when
the suite or a plugin changes and weekly, never per release.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from scripts.harness_compat.contract import SUITE_ENV_MUTATION
from scripts.harness_compat.sandbox import make_sandbox
from scripts.harness_compat.suite_runner import installed_version, run_suite
from tests.support.mutations import ANY, LOADED_BANNER, MUTATIONS, Mutation, applicable
from tests.support.obligations import required
from tests.support.paths import REPO_ROOT
from theater.harness import HARNESSES
from theater.harness.builtin import plugin_dir

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("HARNESS_COMPAT_CANARIES") != "1", reason="opt in with: make canaries"
    ),
]
#: Read from disk: the registry is only populated after collection.
SHIPPED = sorted(path.parent.name for path in plugin_dir().glob("*/manifest.py"))


@pytest.mark.parametrize("mutation", MUTATIONS, ids=lambda item: item.name)
@pytest.mark.parametrize("name", SHIPPED)
def test_the_suite_notices_a_deliberately_broken_plugin(name: str, mutation: Mutation) -> None:
    harness = HARNESSES[name]
    expected = applicable(mutation, required(harness))
    if not expected:
        pytest.skip(f"{mutation.name} breaks nothing this harness declares")
    binary = shutil.which(harness.binary)
    if binary is None:
        pytest.skip(f"no {harness.binary!r} binary on PATH")

    with make_sandbox(name) as sb:
        report = run_suite(
            REPO_ROOT,
            sb,
            harness=name,
            binary=Path(binary),
            version=installed_version(Path(binary)),
            pytest_args=["-k", mutation.select],
            extra_env={SUITE_ENV_MUTATION: mutation.name},
        )
        banner = LOADED_BANNER % mutation.name
        loaded = [
            log
            for log in sb.evidence_dir.glob("*/daemon.log")
            if banner in log.read_text(encoding="utf-8", errors="replace")
        ]

    assert loaded, "the mutated plugin was never loaded by the daemon, so nothing was proven"
    assert not report.problems
    ran = {
        check.name.removeprefix("conformance:"): check.status
        for check in report.checks
        if check.name.startswith("conformance:") and check.status != "skip"
    }
    if ANY in expected:
        assert any(status in ("fail", "error") for status in ran.values()), ran
        return
    for obligation, how in expected.items():
        allowed = {"fail"} if how == "fail" else {"fail", "error"}
        assert ran.get(obligation) in allowed, f"{obligation} was {ran.get(obligation)}: {ran}"
    untouched = {key: value for key, value in ran.items() if key not in expected}
    assert all(value == "pass" for value in untouched.values()), untouched
