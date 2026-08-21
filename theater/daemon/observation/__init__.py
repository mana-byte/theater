"""Observation package: watch what agents are doing by tailing transcripts.

The observer's two jobs — getting the text, and deciding what it means — are
split across this package. Getting the text is the replaceable per-harness
``Source`` seam (in ``theater.harness``); deciding what it means is the central
reducer, quiet timers, and job completion that lives here.

Submodules:
  turns      — Turn, TurnAccumulator, answers_prompt (pure value objects)
  screen     — tmux capture and screen-result mechanics
  identity   — ambiguity and ownership predicates
  completion — job completion and unmatched-turn tracking
  failures   — source errors, quarantine, identity-loss grace
  attachment — transcript ownership, receipt staging, attachment admission
  reducer    — QuietClock, _apply, _on_quiet, _settle, status policy
  service    — Observer lifecycle, supervision, watch orchestration
"""
