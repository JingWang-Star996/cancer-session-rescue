# Codex Session Monitor 1.1.1

Portable Windows x64 release, including all changes since public Monitor 1.0.

## Changes

- Compact mode now supports a single session plus seven group scopes: output within 60 seconds, activity within 24 hours, all unarchived, parents, children, current list filter, and the whole catalog including archived sessions. The first five groups exclude archived sessions; the current filter covers all matching pages.
- Opt-in 60-second input/cache counts, cache hit ratio, and persistent confirmed input/output/cache totals. Enable recording in Settings; it is off by default.
- Choose compact metrics, keep the window on top, retain the selected session and scope, and apply privacy across both views.
- Small windows prioritize the session list and show details on demand.
- Aggregates reuse the existing sampling snapshot, show known coverage and partial subtotals, and calculate cache ratio from valid input/cache pairs.

## Download and upgrade

Use `CodexSessionMonitor-1.1.1-windows-x64.zip` for the GUI, Agent CLI, source, docs, and licenses; extract into a writable folder and run without Python. `CodexSessionMonitor-1.1.1.exe` is the standalone GUI. Verify downloads with `SHA256SUMS.txt`.

Close the old monitor before replacing it, or extract into a new folder. Preferences and optional counters remain under `%LOCALAPPDATA%/CodexSessionMonitor/`. This is a portable package, not an installer; no Codex installation changes or private log copies are needed.

## Agent contract and limits

Product version is 1.1.1; Agent JSON schema remains 1.0 with `capabilities/list/diagnose/plan`. GUI persistent counters, live subscriptions, and compact aggregates are not CLI endpoints. See the [Agent contract](https://github.com/JingWang-Star996/cancer-session-rescue/blob/monitor-v1.1.1/docs/monitor-agent-api.md).

Cached input is included in input. Bounded sampling cannot reconstruct earlier resets, cross-session totals do not deduplicate shared history, and cold sessions can lag. Metrics are neither live character streaming nor billing. The monitor reads source logs and optionally writes its own counter store; it does not apply repairs or call models. The older `csr --apply` is a separate tool.

## Validation

64 regression tests: 63 passed, one directory-symlink test skipped because of platform permissions. Nine native widget size/scaling combinations passed; 960×600 showed 9–11 rows. Aggregate totals passed with 5,000 synthetic sessions. Frozen CLI commands, UTF-8, privacy, and source overwrite rejection passed. The unchanged 1.1.0 recording core previously passed a 75-second live recording and restart recovery check. These observations do not guarantee performance on every machine. No private sessions or counter database are included.
