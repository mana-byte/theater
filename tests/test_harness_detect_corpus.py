"""Corpus of real-world pane-command strings pinned against detection.

Every row in this file is a `pane_current_command` string actually observed on a
developer machine running all four harnesses under Nix.  The kernel truncates
process names at 15 characters in `pane_current_command`, which is what makes
`.opencode-wrapped` appear as `.opencode-wrapp` — too short for the unwrap
convention to recover.  These are not synthetic edge cases; they are what real
machines look like, and pinning them keeps the detection chain honest.

The load-bearing assertions are the safety-policy guards at the bottom: a pane
that the detection chain cannot identify must yield ``UNDETERMINED`` against a
registry row claiming any harness — never ``CONFLICT``.  That is the exact
regression that caused a production incident where a live `claude` participant
was misdetected as `unknown` and treated as a foreign harness, cascading into a
permanently-unusable checkpoint.  The judgement half was fixed in
``compare_detected_harness``; the policy guards pin that judgement so nobody
later "tightens" undetermined into conflict.  The guards call the judgement
function directly with a literal ``"unknown"``, not via ``detect_harness``, so
that improving detection (e.g. teaching it to resolve `python3.12` to `vibe`
via `ps -o args`) does not break a test whose purpose is to protect a safety
rule, not to pin today's detection capability.
"""

from __future__ import annotations

import pytest

from theater import proc
from theater.config import Config
from theater.daemon.harness_detect import (
    PaneHarnessVerdict,
    compare_detected_harness,
    compare_pane_harness,
    detect_harness,
    match_binary,
)
from theater.harness import HARNESSES, install


@pytest.fixture(autouse=True)
def _installed():
    install(Config())


@pytest.fixture(autouse=True)
def _no_process_evidence(monkeypatch):
    """Every corpus and guard test sees an empty process table by default.

    Without this, ``detect_harness(cmd, 999_999)`` reaches ``proc.comm`` and
    captures a live ``ps`` of the machine running the suite, making the
    assertion's premise invisible and the result dependent on pid 999999 not
    existing.  The fallback test overrides ``proc.comm`` and the snapshot
    locally to inject the evidence it needs.
    """
    monkeypatch.setattr(proc, "comm", lambda pid: "")


# ---- corpus: the four observed pane commands -------------------------------


CORPUS = [
    # (pane_current_command, expected_detect_result)
    (".claude-wrapped", "claude"),
    (".codex-wrapped", "codex"),
    (".opencode-wrapp", "unknown"),
    ("python3.12", "unknown"),
]


@pytest.mark.parametrize("pane_command, expected", CORPUS)
def test_corpus_detect_harness(pane_command, expected):
    """Each observed pane command must produce the expected detection result.

    With no process evidence at all (empty snapshot, no root comm), the command
    string alone yields the expected result.  The kernel truncates process
    names at 15 characters, so `.opencode-wrapped` never appears in full and
    the unwrap convention cannot recover it from the pane command alone.  It
    resolves only via the process-tree fallback (see
    ``test_corpus_opencode_wrapp_fallback``).
    """
    snapshot = proc.ProcessSnapshot()
    detected = detect_harness(pane_command, 999_999, snapshot=snapshot)
    assert detected == expected, (
        f"detect_harness({pane_command!r}) → {detected!r}, expected {expected!r}"
    )


@pytest.mark.parametrize("pane_command, expected", CORPUS)
def test_corpus_match_binary(pane_command, expected):
    """The direct match (no process tree) for each corpus row."""
    result = match_binary(pane_command, HARNESSES)
    assert result == (None if expected == "unknown" else expected), (
        f"match_binary({pane_command!r}) → {result!r}, expected "
        f"{'None' if expected == 'unknown' else expected!r}"
    )


# ---- process-tree fallback for .opencode-wrapp ------------------------------


def test_corpus_opencode_wrapp_fallback(monkeypatch):
    """`.opencode-wrapp` resolves to `opencode` via the process-tree fallback.

    The pane command is truncated at 15 characters, so the unwrap convention
    cannot strip `-wrapped`.  But the real binary `opencode` appears as a
    descendant in the process tree, and the descendant walk finds it.
    """
    monkeypatch.setattr(proc, "comm", lambda pid: "")
    snapshot = proc.ProcessSnapshot(_children={50000: [(50001, "opencode")]})
    assert detect_harness(".opencode-wrapp", 50000, snapshot=snapshot) == "opencode"


# ---- the load-bearing safety-policy guards ----------------------------------
#
# These call ``compare_detected_harness`` directly with a literal ``"unknown"``
# rather than deriving the detected value from ``detect_harness``.  The rule
# being pinned is a judgement-half rule: an unidentified pane is
# ``UNDETERMINED`` against any recorded harness, never ``CONFLICT``.  That
# rule must hold regardless of how good detection ever gets, and coupling it
# to today's detection capability would make the guard fail when someone
# correctly improves detection (e.g. resolves `python3.12` to `vibe`).


@pytest.mark.parametrize("recorded_harness", ["claude", "codex", "opencode", "vibe"])
def test_undetermined_never_conflict_for_python312(recorded_harness):
    """An unidentified pane must be UNDETERMINED against any recorded harness.

    `python3.12` is the pane command for vibe, which is a Python entry-point
    script — both the pane command and the process root are the interpreter,
    so the detection chain returns ``"unknown"``.  The judgement must be
    ``UNDETERMINED`` (absence of evidence), never ``CONFLICT``, because a
    conflict on a pane that cannot be identified is the exact regression that
    cascaded into a permanently-unusable checkpoint.
    """
    verdict = compare_detected_harness(recorded_harness, "unknown", "python3.12")
    assert verdict is PaneHarnessVerdict.UNDETERMINED, (
        f"an unidentified pane (detected 'unknown') against recorded "
        f"{recorded_harness!r} must be UNDETERMINED, got {verdict!r} — "
        f"tightening undetermined into conflict would recreate the "
        f"production incident"
    )


@pytest.mark.parametrize("recorded_harness", ["claude", "codex", "opencode", "vibe"])
def test_undetermined_never_conflict_for_opencode_wrapp(recorded_harness):
    """Same guard for `.opencode-wrapp`: unknown detection, non-shell foreground.

    The truncated command is not a shell, so the verdict is ``UNDETERMINED``,
    not ``HARNESS_GONE`` — and never ``CONFLICT``.
    """
    verdict = compare_detected_harness(recorded_harness, "unknown", ".opencode-wrapp")
    assert verdict is PaneHarnessVerdict.UNDETERMINED, (
        f"an unidentified pane (detected 'unknown') against recorded "
        f"{recorded_harness!r} must be UNDETERMINED, got {verdict!r}"
    )


def test_compare_pane_harness_undetermined_for_python312():
    """The one end-to-end path: detect then judge, with an empty process table.

    ``compare_pane_harness`` accepts a snapshot, so we pass an empty one to
    avoid a live ``ps``.  This is the only test that exercises the full
    detect-then-judge path for the incident scenario; the parametrised guards
    above pin the judgement half in isolation.
    """
    snapshot = proc.ProcessSnapshot()
    verdict = compare_pane_harness("vibe", "python3.12", 999_999, snapshot=snapshot)
    assert verdict is PaneHarnessVerdict.UNDETERMINED
