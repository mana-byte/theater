"""One supplied release: install and identity, a disposable code copy, the suite decides."""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

from scripts.harness_compat import qualify
from scripts.harness_compat.cleanup import CleanupFailed
from scripts.harness_compat.codecopy import UnsafeCopy, code_copy
from scripts.harness_compat.contract import Check, InstallFailed, Sandbox
from scripts.harness_compat.policy import load_policy
from scripts.harness_compat.sandbox import make_sandbox
from scripts.harness_compat.suite_runner import SuiteReport, run_suite, verdict
from theater.harness.contracts.version_policy import VersionPolicy

Git = Callable[..., str]


class _Suite:
    """Stands in for the live suite and records the policy the code copy held when it ran."""

    def __init__(self, report: SuiteReport | Exception) -> None:
        self.report = report
        self.calls: list[dict] = []

    def __call__(self, root: Path, sb: Sandbox, **kwargs: object) -> SuiteReport:
        self.calls.append({"root": root, "policy": load_policy(root, "claude"), **kwargs})
        if isinstance(self.report, Exception):
            raise self.report
        return self.report


def _green() -> SuiteReport:
    return SuiteReport(
        (Check("tests/conformance/live", "pass", "3 passed"), Check("conformance:launch", "pass"))
    )


def test_the_copy_holds_tracked_and_untracked_files_but_not_ignored_ones_or_git(
    repo: Path,
) -> None:
    (repo / "new.txt").write_text("new\n", encoding="utf-8")
    (repo / "ignored").mkdir()
    (repo / "ignored" / "junk.txt").write_text("junk\n", encoding="utf-8")
    (repo / "tracked.txt").unlink()  # tracked, deleted in the working tree

    with code_copy(repo) as tree:
        files = {p.relative_to(tree).as_posix() for p in tree.rglob("*") if p.is_file()}
        assert "new.txt" in files and ".gitignore" in files
        assert "tracked.txt" not in files and "ignored/junk.txt" not in files
        assert not (tree / ".git").exists()
        (tree / "new.txt").write_text("edited in the copy\n", encoding="utf-8")
    assert (repo / "new.txt").read_text() == "new\n"
    assert not tree.exists()


def test_a_copy_can_be_copied_again_without_git_metadata_or_junk(repo: Path) -> None:
    with code_copy(repo) as outer:
        (outer / "__pycache__").mkdir()
        (outer / "__pycache__" / "x.pyc").write_text("junk")
        (outer / "nested").mkdir()
        (outer / "nested" / "keep.txt").write_text("keep")
        (outer / "link").symlink_to("nested")
        with code_copy(outer) as inner:
            files = {p.relative_to(inner).as_posix() for p in inner.rglob("*") if not p.is_dir()}
            assert "nested/keep.txt" in files and "tracked.txt" in files
            assert not any("__pycache__" in name for name in files)
            assert (inner / "link").is_symlink()


def test_the_suite_refuses_a_git_checkout(repo: Path) -> None:
    with make_sandbox("claude") as sb, pytest.raises(ValueError, match="git checkout"):
        run_suite(repo, sb, harness="claude", binary=Path("claude"), version="1.1.0")


def test_symlinks_that_could_reach_the_checkout_or_the_home_are_refused(
    repo: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    cases = {
        "absolute_into_checkout": repo / "tracked.txt",
        "absolute_into_home": Path.home(),
        "absolute_elsewhere": outside,
    }
    for name, target in cases.items():
        link = repo / name
        link.symlink_to(target)
        with pytest.raises(UnsafeCopy, match=name), code_copy(repo):
            pass
        link.unlink()


def test_a_relative_symlink_that_climbs_out_of_the_tree_is_refused(repo: Path, git: Git) -> None:
    (repo / "up").symlink_to("../..")

    with pytest.raises(UnsafeCopy, match="outside the tree"), code_copy(repo):
        pass


def test_a_chain_of_links_is_judged_by_where_it_ends(repo: Path, tmp_path: Path) -> None:
    (repo / "last").symlink_to(tmp_path)
    (repo / "first").symlink_to("last")

    with pytest.raises(UnsafeCopy), code_copy(repo):
        pass


def test_a_link_that_stays_inside_the_tree_is_copied(repo: Path) -> None:
    (repo / "docs").mkdir()
    (repo / "docs" / "guide.txt").write_text("guide\n")
    (repo / "docs_link").symlink_to("docs")
    (repo / "docs" / "back").symlink_to("../tracked.txt")
    (repo / "dangling").symlink_to("not-there")

    with code_copy(repo) as tree:
        assert (tree / "docs_link").is_symlink() and (tree / "docs_link" / "guide.txt").exists()
        assert (tree / "docs" / "back").resolve() == (tree / "tracked.txt").resolve()
        assert (tree / "dangling").is_symlink()


def test_a_refused_copy_leaves_nothing_behind(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "bad").symlink_to(tmp_path)
    bases: list[Path] = []
    real = tempfile.mkdtemp

    def record(*args: object, **kwargs: object) -> str:
        bases.append(Path(real(*args, **kwargs)))  # type: ignore[arg-type]
        return str(bases[-1])

    monkeypatch.setattr(tempfile, "mkdtemp", record)

    with pytest.raises(UnsafeCopy), code_copy(repo):
        pass

    assert bases and not bases[0].exists()


def test_the_copy_keeps_modes_and_symlinks(repo: Path, git: Git) -> None:
    script = repo / "run.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    (repo / "link").symlink_to("tracked.txt")

    with code_copy(repo) as tree:
        assert (tree / "run.sh").stat().st_mode & stat.S_IXUSR
        assert (tree / "link").is_symlink() and (tree / "link").readlink() == Path("tracked.txt")


def test_a_release_the_policy_refuses_is_admitted_alone_in_the_copy_only(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = _Suite(_green())
    monkeypatch.setattr(qualify, "run_suite", suite)
    binary = Path("/nowhere/claude")

    with make_sandbox("claude") as sb:
        result = qualify.qualify_binary(repo, sb, "claude", binary, "1.9.0")

    assert result.verdict == "green"
    assert suite.calls[0]["policy"] == VersionPolicy("1.9.0", "1.9.0")
    assert suite.calls[0]["root"] != repo
    assert load_policy(repo, "claude") == VersionPolicy("1.0.0", "1.2.0")


def test_an_admitted_release_runs_against_the_code_unchanged(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = _Suite(_green())
    monkeypatch.setattr(qualify, "run_suite", suite)

    with make_sandbox("claude") as sb:
        qualify.qualify_binary(repo, sb, "claude", Path("claude"), "1.1.0")

    assert suite.calls[0]["policy"] == VersionPolicy("1.0.0", "1.2.0")


def test_a_relative_binary_is_resolved_before_entering_the_code_copy(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = _Suite(_green())
    monkeypatch.setattr(qualify, "run_suite", suite)
    monkeypatch.chdir(repo)

    with make_sandbox("claude") as sb:
        qualify.qualify_binary(repo, sb, "claude", Path("bin/claude"), "1.1.0")

    assert suite.calls[0]["binary"] == repo / "bin/claude"


def test_an_excluded_release_is_admitted_alone_too(
    repo: Path, git: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    (repo / "theater/harness/builtin/plugins/claude/compatibility.json").write_text(
        '{"min":"1.0.0","max":"1.2.0","excluded":["1.1.0"]}\n', encoding="utf-8"
    )
    suite = _Suite(_green())
    monkeypatch.setattr(qualify, "run_suite", suite)

    with make_sandbox("claude") as sb:
        qualify.qualify_binary(repo, sb, "claude", Path("claude"), "1.1.0")

    assert suite.calls[0]["policy"] == VersionPolicy("1.1.0", "1.1.0")


def test_pytest_arguments_reach_the_suite(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    suite = _Suite(_green())
    monkeypatch.setattr(qualify, "run_suite", suite)

    with make_sandbox("claude") as sb:
        qualify.qualify_binary(
            repo, sb, "claude", Path("claude"), "1.1.0", pytest_args=["-k", "mcp"]
        )

    assert suite.calls[0]["pytest_args"] == ["-k", "mcp"]


def test_machinery_that_breaks_is_tooling_never_a_verdict_on_the_release(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(qualify, "run_suite", _Suite(RuntimeError("disk full")))

    with make_sandbox("claude") as sb:
        result = qualify.qualify_binary(repo, sb, "claude", Path("claude"), "1.1.0")

    assert result.verdict == "tooling"
    assert result.checks[0].name == "qualification"
    assert "RuntimeError: disk full" in result.checks[0].detail


def test_cleanup_failure_aborts_the_whole_run_instead_of_becoming_a_release_outcome(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(qualify, "run_suite", _Suite(CleanupFailed("temporary data remains")))

    with make_sandbox("claude") as sb, pytest.raises(CleanupFailed, match="data remains"):
        qualify.qualify_binary(repo, sb, "claude", Path("claude"), "1.1.0")


def _report(*checks: Check, problems: tuple[str, ...] = (), partial: bool = False) -> SuiteReport:
    return SuiteReport(checks, problems, partial)


SUITE = Check("tests/conformance/live", "pass", "")


@pytest.mark.parametrize(
    ("report", "expected"),
    [
        (_report(SUITE, Check("conformance:a", "pass"), Check("conformance:b", "pass")), "green"),
        (
            _report(SUITE, Check("conformance:a", "pass"), Check("conformance:b", "skip")),
            "inconclusive",
        ),
        (_report(SUITE), "inconclusive"),
        (_report(SUITE, Check("conformance:a", "pass"), Check("conformance:b", "fail")), "red"),
        (_report(Check(SUITE.name, "fail"), Check("conformance:a", "pass")), "red"),
        (
            _report(Check(SUITE.name, "error", "timed out"), Check("conformance:a", "pass")),
            "inconclusive",
        ),
        (_report(SUITE, Check("conformance:a", "error", "never ran")), "inconclusive"),
        (_report(SUITE, Check("conformance:a", "skip")), "inconclusive"),
        (
            _report(
                SUITE, Check("conformance:reconcile", "error", "x is missing"), problems=("x",)
            ),
            "tooling",
        ),
        (
            _report(
                Check(SUITE.name, "error", "crashed"),
                Check("conformance:reconcile", "error", "inventory.json is missing"),
                problems=("inventory.json is missing",),
            ),
            "inconclusive",
        ),
        (
            _report(
                SUITE,
                Check("conformance:a", "fail"),
                Check("conformance:reconcile", "error", "x"),
                problems=("x",),
            ),
            "red",
        ),
    ],
)  # fmt: skip
def test_the_suite_alone_decides_the_verdict(report: SuiteReport, expected: str) -> None:
    assert verdict(report) == expected


def test_a_filtered_run_is_never_green_but_a_failure_in_it_still_is_red() -> None:
    passing = _report(SUITE, Check("conformance:a", "pass"), partial=True)
    failing = _report(SUITE, Check("conformance:a", "fail"), partial=True)

    assert verdict(passing) == "inconclusive"
    assert verdict(failing) == "red"


class _Adapter:
    name = "claude"

    def __init__(self, binary: Callable[[Sandbox], Path] | Exception) -> None:
        self.binary = binary
        self.installed: list[str] = []

    def discover(self) -> list[str]:
        raise AssertionError("a supplied version is never discovered")

    def install(self, sb: Sandbox, version: str) -> Path:
        self.installed.append(version)
        if isinstance(self.binary, Exception):
            raise self.binary
        return self.binary(sb)


def _cli(report: str) -> Callable[[Sandbox], Path]:
    def make(sb: Sandbox) -> Path:
        script = sb.bin_dir / "claude"
        script.write_text(f"#!/bin/sh\necho '{report}'\n")
        script.chmod(0o755)
        return script

    return make


def test_the_installed_binary_must_report_the_requested_release(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = _Suite(_green())
    monkeypatch.setattr(qualify, "run_suite", suite)
    adapter = _Adapter(_cli("1.1.0 (Claude Code)"))

    result = qualify.qualify_release(repo, adapter, "1.1.0")  # type: ignore[arg-type]

    assert result.verdict == "green" and adapter.installed == ["1.1.0"]
    assert suite.calls[0]["binary"].name == "claude" and suite.calls[0]["version"] == "1.1.0"


def test_a_binary_reporting_another_release_is_inconclusive_and_never_reaches_the_suite(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = _Suite(_green())
    monkeypatch.setattr(qualify, "run_suite", suite)

    result = qualify.qualify_release(repo, _Adapter(_cli("1.0.9")), "1.1.0")  # type: ignore[arg-type]

    assert (result.verdict, result.checks[0].name) == ("inconclusive", "install")
    assert "reports 1.0.9" in result.checks[0].detail and not suite.calls


def test_a_failed_install_is_inconclusive_not_red(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    suite = _Suite(_green())
    monkeypatch.setattr(qualify, "run_suite", suite)

    result = qualify.qualify_release(repo, _Adapter(InstallFailed("npm exploded")), "1.1.0")  # type: ignore[arg-type]

    assert result.verdict == "inconclusive" and "npm exploded" in result.checks[0].detail
    assert not suite.calls


def test_a_binary_that_cannot_run_is_an_install_problem(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(qualify, "run_suite", _Suite(_green()))

    def missing(sb: Sandbox) -> Path:
        return sb.bin_dir / "absent"

    result = qualify.qualify_release(repo, _Adapter(missing), "1.1.0")  # type: ignore[arg-type]

    assert (
        result.verdict == "inconclusive" and "cannot report its version" in result.checks[0].detail
    )


def test_the_sandbox_is_gone_after_a_run(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    roots: list[Path] = []

    def suite(root: Path, sb: Sandbox, **kwargs: object) -> SuiteReport:
        roots.append(sb.root)
        return _green()

    monkeypatch.setattr(qualify, "run_suite", suite)

    qualify.qualify_release(repo, _Adapter(_cli("1.1.0")), "1.1.0")  # type: ignore[arg-type]

    assert roots and not roots[0].exists()
    assert os.environ.get("HARNESS_COMPAT_SANDBOX") is None


def test_nothing_in_the_checkout_changes(
    repo: Path, git: Git, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(qualify, "run_suite", _Suite(_green()))

    with make_sandbox("claude") as sb:
        qualify.qualify_binary(repo, sb, "claude", Path("claude"), "9.9.9")

    assert git(repo, "status", "--porcelain") == ""


_MINI_CONFTEST = """
import json, os, pathlib

evidence = pathlib.Path(os.environ["HARNESS_COMPAT_EVIDENCE_DIR"])
harness, version = os.environ["HARNESS_COMPAT_HARNESS"], os.environ["HARNESS_COMPAT_VERSION"]
row = {"id": "launch:yolo", "family": "launch", "scenario": "test_mini.py"}


def _document(rows):
    return json.dumps(
        {"schema": 1, "harness": harness, "version": version, "obligations": rows}
    )


(evidence / "required.json").write_text(_document([row]))


def pytest_sessionfinish(session):
    if os.environ.get("MINI_NO_INVENTORY"):
        return
    outcome = os.environ.get("MINI_OUTCOME", "passed")
    done = {**row, "status": "executed", "outcome": outcome, "detail": "from the mini suite"}
    (evidence / "inventory.json").write_text(_document([done]))
"""
_MINI_TEST = """
import json, os, pathlib

def test_the_copy_admits_only_the_candidate():
    policy = json.loads(
        pathlib.Path("theater/harness/builtin/plugins/claude/compatibility.json").read_text()
    )
    version = os.environ["HARNESS_COMPAT_VERSION"]
    assert policy["min"] == policy["max"] == version
    assert os.environ["HARNESS_COMPAT_SANDBOX"]
    assert os.environ.get("MINI_OUTCOME", "passed") == "passed"
"""


@pytest.fixture
def mini_suite(repo: Path, git: Git) -> Path:
    """The repository with a three-line stand-in for ``tests/conformance/live``."""
    live = repo / "tests/conformance/live"
    live.mkdir(parents=True)
    (live / "conftest.py").write_text(_MINI_CONFTEST, encoding="utf-8")
    (live / "test_mini.py").write_text(_MINI_TEST, encoding="utf-8")
    return repo


@pytest.mark.parametrize(
    ("env", "verdict"),
    [
        ({}, "green"),
        ({"MINI_OUTCOME": "failed"}, "red"),
        ({"MINI_NO_INVENTORY": "1"}, "tooling"),
    ],
)
def test_a_real_suite_run_in_the_copy_reaches_its_verdict(
    mini_suite: Path, env: dict[str, str], verdict: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = qualify.run_suite

    def configured(*args, **kwargs):
        return original(*args, **kwargs, extra_env=env)

    monkeypatch.setattr(qualify, "run_suite", configured)
    with make_sandbox("claude") as sb:
        result = qualify.qualify_binary(mini_suite, sb, "claude", Path("/nowhere/claude"), "1.9.0")

    assert result.verdict == verdict, result.checks
    assert load_policy(mini_suite, "claude") == VersionPolicy("1.0.0", "1.2.0")
    names = [check.name for check in result.checks]
    assert names[0] == "tests/conformance/live"
    assert ("conformance:launch:yolo" in names) == (verdict != "tooling")


def test_a_filtered_qualification_can_never_authorize(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(qualify, "run_suite", _Suite(SuiteReport(_green().checks, partial=True)))

    with make_sandbox("claude") as sb:
        result = qualify.qualify_binary(
            repo, sb, "claude", Path("claude"), "1.1.0", pytest_args=["-k", "launch"]
        )

    assert result.verdict == "inconclusive"


def test_a_filtered_qualification_that_fails_is_still_red(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = SuiteReport((Check("tests/conformance/live", "fail"), Check("conformance:a", "fail")))
    monkeypatch.setattr(qualify, "run_suite", _Suite(report))

    with make_sandbox("claude") as sb:
        result = qualify.qualify_binary(
            repo, sb, "claude", Path("claude"), "1.1.0", pytest_args=["-k", "a"]
        )

    assert result.verdict == "red"


def test_a_real_filtered_run_in_the_copy_is_inconclusive_even_when_everything_selected_passed(
    mini_suite: Path,
) -> None:
    with make_sandbox("claude") as sb:
        result = qualify.qualify_binary(
            mini_suite, sb, "claude", Path("/nowhere/claude"), "1.9.0", pytest_args=["-k", "copy"]
        )

    assert result.verdict == "inconclusive", result.checks


def test_an_unsafe_symlink_is_tooling_and_leaves_no_copy_behind(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (repo / "escape").symlink_to(tmp_path)
    suite = _Suite(_green())
    monkeypatch.setattr(qualify, "run_suite", suite)

    with make_sandbox("claude") as sb:
        result = qualify.qualify_binary(repo, sb, "claude", Path("claude"), "1.1.0")

    assert result.verdict == "tooling" and "UnsafeCopy" in result.checks[0].detail
    assert not suite.calls
