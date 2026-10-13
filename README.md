# Codex Session Monitor 1.1.1 and cancer-session-rescue

[English](README.md) | [简体中文](README.zh-CN.md)

**Codex Session Monitor 1.1.1** is a Windows desktop viewer for local Codex sessions. It shows session growth, supports bounded diagnosis, and prepares a plan for a human or Agent to review. It does not rewrite session logs, apply repairs, call a model, or restart Codex.

This repository also contains the existing **cancer-session-rescue (`csr`) v0.2.0** command-line tool for WorkBuddy, Codex, and generic JSONL logs. Its separate commands can rewrite files when explicitly run with `--apply`. Monitor 1.1.1 does not expose that capability.

## Download Monitor 1.1.1 for Windows

Download from the [Monitor 1.1.1 release](https://github.com/JingWang-Star996/cancer-session-rescue/releases/tag/monitor-v1.1.1):

| Asset | Use |
|---|---|
| `CodexSessionMonitor-1.1.1-windows-x64.zip` | Portable package with the desktop app and `codex-monitor-agent.exe`; extract and run without installing Python. |
| `CodexSessionMonitor-1.1.1.exe` | Standalone desktop GUI. Use the ZIP if you also need the Agent CLI. |
| `SHA256SUMS.txt` | SHA-256 hashes for checking downloads. |

Monitor requires Windows x64. Extract the ZIP to a writable folder and run its desktop executable, or run the standalone EXE directly. The app reads `CODEX_HOME` when set, otherwise the current user's `.codex` directory. See the [English](docs/monitor-1.1.1-release.md) and [Chinese](docs/monitor-1.1.1-release.zh-CN.md) release notes.

## New in 1.1.1

- **Compact group totals:** keep a single session or select one of seven aggregate scopes: output in the last 60 seconds, activity in the past 24 hours, all unarchived sessions, parent sessions, child sessions, the current list filter, or the full catalog including archived sessions. The first five group scopes exclude archived sessions. Current-filter totals cover every matching page and refresh from the latest snapshot.
- **Choose only the metrics you need and keep the window on top.** For an overall output watch, choose all unarchived sessions and the 60-second output metric. Scope and selected session are retained when switching modes; privacy applies to both views. Ctrl+M switches modes; Ctrl+T toggles always-on-top.
- **Opt-in input/cache and persistent totals:** in Settings → Token & compact mode, enable input, cached-input, and cumulative Token recording. Recording is off by default. The GUI shows 60-second input/cache, cache hit ratio, and recorded input/output/cache totals. Turning recording off stops updates and keeps existing records.
- **More usable small windows:** the session list takes priority, detail panels expand on demand, and summary cards collapse in smaller windows. Existing sampling and page-size settings remain available.

Totals reuse the existing sampling snapshot, without extra log scans. Counts show known coverage and partial subtotals; all-unknown values remain `—`. Cache hit ratio is summed cached input divided by summed input over valid paired records, not the average of individual percentages. Cached input is already part of input.

Recorded totals initialize from the latest available cumulative log record, then add confirmed changes. The local store at `%LOCALAPPDATA%/CodexSessionMonitor/usage.sqlite3` contains session IDs, counts, and reconciliation state, not conversation content. Earlier resets cannot be reconstructed by bounded tail reads; cold sessions may be delayed. Totals across sessions do not deduplicate shared history and are not account billing. The 60-second window is separate from the 2–60 second sampling interval; logs can report usage in batches.

The Agent JSON schema stays at 1.0. The CLI does not expose GUI persistent counters, live subscriptions, or compact aggregate scopes. See the [Agent contract](docs/monitor-agent-api.md).

## Monitor quick start

1. Open the app. The default view shows unarchived sessions active in the past 24 hours, 100 per page. Switch to all sessions or include archived sessions for older work.
2. Select a session to see file size, recent growth, and details. **Diagnose & Optimize** inspects bounded evidence and prepares an Agent plan for review; it does not run a repair.
3. Turn on privacy mode before exporting or sharing when identity or paths should be hidden. Save CSV exports under a new filename outside Codex home; an existing file is not overwritten. An anonymous plan cannot identify the original session for an Agent; provide the target separately if needed.

Sampling defaults to 5 seconds (configurable 2–60); catalog refresh defaults to 60 seconds (30–600). Each sampling round has a shared 2 MiB JSONL read budget: up to 32 recent files, the selected session, and 64 rotating older files. Changed older files can temporarily gain priority. When minimized, the sampling interval is at least 30 seconds. Older activity can appear after rotation or a catalog refresh. File bytes and image estimates are not model context tokens, account usage, or billing.

## Agent CLI

The ZIP includes a separate UTF-8 JSON CLI. In PowerShell from the extracted folder:

```powershell
.\codex-monitor-agent.exe capabilities
.\codex-monitor-agent.exe list --privacy --page 1 --limit 100
.\codex-monitor-agent.exe diagnose --session '<session-uuid>' --private --output .\diagnosis.json
.\codex-monitor-agent.exe plan --session '<session-uuid>' --privacy --output .\plan.json
```

Replace the quoted placeholder with the actual session UUID. Use `--privacy` for anonymized output or `--private` to allow identifying metadata. The CLI does not read the GUI privacy setting. `--output` must point outside Codex home to a new file; it never overwrites a report. Diagnosis reads at most the first 16 MiB or 2 seconds of the selected log. Partial coverage cannot establish that unscanned content contains no images. See the [Agent API contract](docs/monitor-agent-api.md) for fields, limits, and source invocation.

## Existing `csr` v0.2.0 CLI

`csr` is this repository's earlier session triage and slimming tool. Install it from a clone with `pip install -e .` (Python 3.8+), then inspect `csr --help`.

```bash
csr triage codex
csr triage workbuddy
csr triage generic --dir /path/to/jsonl
csr slim codex /path/to/rollout.jsonl --sanitize
```

The existing `csr` tool has five processing and protection mechanisms:

1. **Image stripping:** replaces eligible embedded image data with a size marker.
2. **Tool-traffic truncation:** shortens oversized tool output and call arguments while preserving message records.
3. **Optional reasoning stripping (`--deep`):** removes selected reasoning and raw-content fields outside the conversation text.
4. **Line-ending preservation:** detects and retains a file's newline style when rewriting JSONL, including Windows CRLF.
5. **Optional path sanitization (`--sanitize`):** replaces username paths; it does not remove every possible secret.

These are **`csr` features**, not Monitor features. `csr slim` defaults to a dry run; `csr slim ... --apply` and `csr slim-dir ... --apply` are separate write operations. Review the source and backup behavior before using them on important sessions.

The [v0.2.0-era README at commit `44e68b0`](https://github.com/JingWang-Star996/cancer-session-rescue/blob/44e68b0/README.md) retains the earlier examples and case history. Its `csr --apply` examples are for the older tool and are not Monitor instructions.

The Monitor plan does not authorize or execute `csr --apply`. Any later repair workflow must verify the current Codex runtime, session state, source and index backups, and native history readback independently. Monitor 1.1.1 has no production log rewrite command.

## Source and license

Monitor source is under [`monitor/`](monitor/); existing `csr` code is under [`src/cancer_session_rescue/`](src/cancer_session_rescue/). See the [Agent API](docs/monitor-agent-api.md) and [license](LICENSE).
