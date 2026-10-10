# Performance campaign — behavior-change ledger

Every behavior-adjacent change shipped by the performance campaign
(origin/main..main, Waves 1-3 + remediations), its adjudication status, and
who accepted it. Written after a post-campaign adversarial audit.

## Adjudicated during review (accepted)

| Change | Commit | Adjudication |
|---|---|---|
| WORKING screen checks speeded to min(base, 0.75s) | c83b05e0 | More capture-pane during WORKING; traded for the 2s approval-detection target. Sol-adjudicated justified (screen capture is the only approval signal). |
| Hidden leaf cost count-up settles instantly when hidden | a5e56844 | Displayed value can jump on show-after-hide; end state matches baseline. Sol-adjudicated. |
| Régie presence reports carry scoped invalidated_terminals | 94409cc8 + focus-scope merge | Less invalidation than wholesale; omitted = invalidate-all on trust loss. Fail-closed adjudicated by two reviewers. |
| SQLite synchronous=FULL restored | c7ea0275 | Durability increased intentionally; user decision. |
| `read_at` added to bus payloads | 2c05c560 + 1c659037 | Omitted from both display sets; CLI/Régie output byte-identical (tested). |
| Wake signal exists for every watched participant | 3fd7a0cd | Internal; polling fallback unchanged. |
| Empty observation batches skip the write unit | 575ad338 | Sol-verified semantics table; no write or acknowledgement skipped. |
| Identity-loss probes use fresh uncoalesced scans | 4c3657c8 | Fail-closed preserved; joined walks only for non-admission consumers. |
| kqueue watch gates skip stat/open for quiet files | b981084c + 243466c4 | 5s safety net; polling fallback on any watcher failure. Sol-adjudicated. |

## Adjudicated post-audit (accepted, previously unrecorded)

These were flagged by the post-campaign audit as lacking adjudication
notes. They are now adjudicated as accepted:

| Change | Commit | Adjudication |
|---|---|---|
| Stable heartbeats no longer publish provider.updated; the projected `last_report_revision` tracks first contact only | 2eccfe89 | Offline and reconcile transitions publish their own events (connections.py:171, :510), so no stale-health path exists. The 358/h journal-spam elimination outweighs the projection field's reduced freshness. If a consumer needs live `last_report_revision`, that is a named follow-up. |
| Dead participants' bindings are no longer health-updated | a0ba197d | Dead participants are not addressable; their binding rows are historical. No consumer found. |
| Presence revision means "state changed", not "refresh happened" | 70d9cf3f | awaiting.py:298-317 retries a failed refresh on revision change; with state-changed semantics a retry fires when state actually changes. No hang found; the per-commit review verified UNKNOWN remains observable. Consumers that need refresh-notifications must not rely on revision bumps. |
| `bus_interval` user config is silently ignored | e7164dfe | The bus reader is now event-driven (long-poll), so the interval has no effect. Deprecated: contracts.py:18 marks it obsolete (kept so old configs load); config.example.toml documents the long-poll. |
| SQLite cache_size 64MiB + mmap 256MiB | bc82573b | Memory-for-reads trade; measured RSS effectively unchanged (99->100MB idle, 125->129MB working). |

## Known gaps with named follow-ups

- observe_to_bus measures read-to-publish only; detection delay (write ->
  observer wake) is excluded. The synthetic-writer A/B is the instrument that
  covers both.
- UnifiedVibeSource wake binding and the vibe inner-swap fd release are being
  fixed in the close-out (audit findings 1-2).
