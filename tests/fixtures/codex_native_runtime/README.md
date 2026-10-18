# Codex native runtime evidence

Two bundles stay committed; `index.json` selects them by role, never by version.

- `floor/` — the oldest release inside the gate (`CODEX_RUNTIME_QUALIFIED_FLOOR`). It changes
  only when the floor moves.
- `latest/` — a recent qualified release. The daily compat pipeline rewrites it only when a
  newly captured bundle's evidence differs (every file except `installed_release.json`, which
  names the binary captured and differs on every release). A bump that finds the same
  behaviour and schema touches only the ceiling line.
- `candidates/` — a capture waiting to be composed; untracked, never committed.

Each bundle holds `installed_release.json`, the behaviour files and `protocol_schema/` as
written by `tests/native/codex_qualify_runtime.py` (`--capture-behavior --provider mock`).
Per-release bundles of earlier releases live in git history only.
