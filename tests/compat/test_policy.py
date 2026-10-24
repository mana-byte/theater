"""Policy derivation from evidence, and the guards that keep a proposal one reviewable line."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.harness_compat.contract import Result
from scripts.harness_compat.policy import (
    PolicyChangeError,
    Unproposable,
    admit_only,
    affected_harnesses,
    changed_gates,
    check_change,
    check_routine_change,
    derive,
    format_policy,
    load_policy,
    merge_pending,
    write_policy,
)
from theater.harness.contracts.version_policy import VersionPolicy

Make = Callable[..., Result]
Git = Callable[..., str]


def _policy(minimum: str = "1.0.0", maximum: str = "1.2.0", *excluded: str) -> VersionPolicy:
    return VersionPolicy(minimum, maximum, excluded)


def test_passes_advance_the_maximum_and_failures_are_excluded_even_above_it(
    result: Make,
) -> None:
    results = (
        result("1.0.0"),
        result("1.1.0", "red"),
        result("1.2.0"),
        result("1.3.0", "inconclusive"),
        result("1.4.0", "tooling"),
        result("1.5.0"),
        result("1.6.0", "red"),
    )

    proposed = derive(_policy(), results)

    assert proposed == _policy("1.0.0", "1.5.0", "1.1.0", "1.3.0", "1.4.0", "1.6.0")


def test_inconclusive_and_tooling_results_are_never_passes(result: Make) -> None:
    results = (result("1.0.0"), result("1.1.0", "inconclusive"), result("1.2.0", "tooling"))

    assert derive(_policy(), results) == _policy("1.0.0", "1.2.0", "1.1.0", "1.2.0")


def test_a_failure_at_the_maximum_adds_an_exclusion_and_never_shrinks_it(result: Make) -> None:
    results = (result("1.0.0"), result("1.1.0"), result("1.2.0", "red"))

    assert derive(_policy(), results) == _policy("1.0.0", "1.2.0", "1.2.0")


def test_a_previously_excluded_release_that_now_passes_is_admitted_again(result: Make) -> None:
    results = (result("1.0.0"), result("1.1.0"), result("1.2.0"))

    assert derive(_policy("1.0.0", "1.2.0", "1.1.0"), results) == _policy()


@pytest.mark.parametrize("verdict", ["red", "inconclusive", "tooling"])
def test_a_failing_minimum_is_excluded_without_blocking_later_passes(
    result: Make, verdict: str
) -> None:
    results = (result("1.0.0", verdict), result("1.1.0"), result("1.2.0"))
    assert derive(_policy(), results) == _policy("1.0.0", "1.2.0", "1.0.0")


def test_all_failures_propose_exclusions_without_changing_the_range(result: Make) -> None:
    assert derive(_policy(), (result("1.3.0", "red"), result("1.4.0", "inconclusive"))) == (
        _policy("1.0.0", "1.2.0", "1.3.0", "1.4.0")
    )


def test_the_file_is_one_compact_line() -> None:
    assert format_policy(_policy("1.0.0", "1.2.0", "1.1.0")) == (
        '{"min":"1.0.0","max":"1.2.0","excluded":["1.1.0"]}\n'
    )


def test_a_canonical_edit_that_keeps_the_minimum_is_accepted() -> None:
    check_change(format_policy(_policy()), format_policy(_policy("1.0.0", "1.9.0", "1.1.0")))


@pytest.mark.parametrize(
    "old_text",
    [
        '{"min":"1.0.0","min":"1.0.0","max":"1.2.0","excluded":[]}\n',
        json.dumps(_policy().to_json(), indent=2) + "\n",
    ],
)
def test_the_old_policy_must_also_be_unambiguous_and_one_line(old_text: str) -> None:
    with pytest.raises(PolicyChangeError):
        check_change(old_text, format_policy(_policy("1.0.0", "1.9.0")))


@pytest.mark.parametrize(
    ("new_text", "needle"),
    [
        (format_policy(_policy("1.0.1", "1.2.0")), "pinned minimum moved"),
        (format_policy(_policy("1.0.0", "1.1.0")), "maximum shrank"),
        (json.dumps(_policy().to_json(), indent=2) + "\n", "canonical single compact line"),
        ('{"min":"1.0.0","max":"1.2.0","excluded":[]}', "canonical single compact line"),
        ('{"min":"1.0.0","max":"1.2.0"}\n', "not valid"),
        ("not json\n", "not valid"),
    ],
)
def test_anything_but_a_canonical_edit_is_refused(new_text: str, needle: str) -> None:
    with pytest.raises(PolicyChangeError, match=needle):
        check_change(format_policy(_policy()), new_text)


def test_admitting_one_release_rewrites_only_the_given_tree(
    repo: Path, tmp_path: Path, git: Git
) -> None:
    admit_only(repo, "claude", "3.1.4")

    assert load_policy(repo, "claude") == VersionPolicy("3.1.4", "3.1.4")
    assert git(repo, "diff", "--numstat").split()[:2] == ["1", "1"]


def test_a_routine_gate_change_is_one_policy_file_and_nothing_else(repo: Path, git: Git) -> None:
    git(repo, "checkout", "-q", "-b", "chore/compat-claude")
    write_policy(repo, "claude", _policy("1.0.0", "1.4.0", "1.1.0"))
    git(repo, "commit", "-qam", "gate")

    assert check_routine_change(repo, "main") == "claude"
    assert changed_gates(repo, "main") == ["claude"]


def test_a_routine_gate_change_that_also_touches_code_is_refused(repo: Path, git: Git) -> None:
    git(repo, "checkout", "-q", "-b", "chore/compat-claude")
    write_policy(repo, "claude", _policy("1.0.0", "1.4.0"))
    (repo / "tracked.txt").write_text("edited\n", encoding="utf-8")
    git(repo, "commit", "-qam", "gate and code")

    with pytest.raises(PolicyChangeError, match=r"exactly one compatibility\.json"):
        check_routine_change(repo, "main")


def test_a_routine_gate_change_that_moves_the_minimum_is_refused(repo: Path, git: Git) -> None:
    git(repo, "checkout", "-q", "-b", "chore/compat-claude")
    write_policy(repo, "claude", _policy("1.0.1", "1.4.0"))
    git(repo, "commit", "-qam", "gate")

    with pytest.raises(PolicyChangeError, match="pinned minimum moved"):
        check_routine_change(repo, "main")


def test_a_change_to_no_policy_selects_no_harness(repo: Path, git: Git) -> None:
    git(repo, "checkout", "-q", "-b", "topic")
    (repo / "tracked.txt").write_text("edited\n", encoding="utf-8")
    git(repo, "commit", "-qam", "code")

    assert changed_gates(repo, "main") == []


def test_a_release_with_two_outcomes_proposes_nothing(result: Make) -> None:
    with pytest.raises(Unproposable, match="more than one outcome"):
        derive(_policy(), (result("1.0.0"), result("1.1.0"), result("1.1.0", "red")))


def test_an_unrelated_exclusion_is_preserved_without_retesting_it(result: Make) -> None:
    policy = _policy("1.0.0", "1.4.0", "1.1.0", "1.3.0")
    results = (result("1.0.0"), result("1.2.0"), result("1.3.0"), result("1.4.0"), result("1.5.0"))

    assert derive(policy, results) == _policy("1.0.0", "1.5.0", "1.1.0")


def test_partial_outcomes_preserve_untested_bounds_and_exclusions(
    result: Make,
) -> None:
    policy = _policy("1.0.0", "1.4.0", "1.1.0", "1.3.0")
    results = (result("1.0.0"), result("1.1.0", "red"), result("1.2.0"))

    assert derive(policy, results) == policy


def test_an_empty_run_has_no_policy_effect() -> None:
    assert derive(_policy(), ()) == _policy()


def test_pending_range_and_exclusion_deltas_survive_main_updates() -> None:
    original = _policy("1.0.0", "1.2.0", "1.1.0")
    current = _policy("1.0.0", "1.4.0", "1.1.0", "1.3.0")
    pending = _policy("1.0.0", "1.5.0", "1.6.0")
    assert merge_pending(current, original, pending) == _policy("1.0.0", "1.5.0", "1.3.0", "1.6.0")


def test_pending_updates_do_not_restore_an_exclusion_already_removed_on_main() -> None:
    original = _policy("1.0.0", "1.2.0", "1.1.0")
    current = _policy("1.0.0", "1.4.0", "1.3.0")
    pending = _policy("1.0.0", "1.5.0", "1.1.0", "1.6.0")
    assert merge_pending(current, original, pending) == _policy("1.0.0", "1.5.0", "1.3.0", "1.6.0")


def test_a_plugin_edit_selects_its_harness_for_a_retest_not_only_a_policy_edit(
    repo: Path, git: Git
) -> None:
    plugin = repo / "theater/harness/builtin/plugins"
    (plugin / "codex").mkdir()
    (plugin / "codex" / "launch.py").write_text("x = 1\n")
    (plugin / "pi").mkdir()
    (plugin / "pi" / "launch.py").write_text("x = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "plugins")
    git(repo, "checkout", "-q", "-b", "topic")
    (plugin / "codex" / "launch.py").write_text("x = 2\n")
    (plugin / "claude" / "observer.py").write_text("y = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "wiring")

    assert affected_harnesses(repo, "main") == ["claude", "codex"]
    assert changed_gates(repo, "main") == []


def test_edits_outside_every_plugin_select_no_harness(repo: Path, git: Git) -> None:
    git(repo, "checkout", "-q", "-b", "topic")
    (repo / "tracked.txt").write_text("edited\n", encoding="utf-8")
    git(repo, "commit", "-qam", "code")

    assert affected_harnesses(repo, "main") == []


@pytest.mark.parametrize(
    "path",
    [
        "theater/daemon/controls/service.py",
        "theater/harness/builtin/plugins/__init__.py",
        "packages/regie/src/regie/bridge.py",
        "scripts/harness_compat/qualify.py",
        "tests/support/live_rig.py",
        "tests/support/channel_oracles.py",
        "tests/support/process_table.py",
        "tests/conftest.py",
        "pyproject.toml",
    ],
)
def test_shared_code_and_suite_changes_retest_every_harness(repo: Path, git: Git, path: str):
    from scripts.harness_compat.adapters import ADAPTER_NAMES

    git(repo, "checkout", "-q", "-b", "topic")
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "shared change")
    assert affected_harnesses(repo, "main") == list(ADAPTER_NAMES)
