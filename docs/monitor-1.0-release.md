# Codex Session Monitor 1.0.0

[English README](https://github.com/JingWang-Star996/cancer-session-rescue/blob/monitor-v1.0.0/README.md) · [中文说明](https://github.com/JingWang-Star996/cancer-session-rescue/blob/monitor-v1.0.0/docs/monitor-1.0-release.zh-CN.md) · [Agent API](https://github.com/JingWang-Star996/cancer-session-rescue/blob/monitor-v1.0.0/docs/monitor-agent-api.md)

Monitor 1.0 brings a portable Windows x64 desktop view of local Codex sessions to this repository. The default page shows the past 24 hours of unarchived sessions, 100 per page; search, sorting, archived-session inclusion, and CSV export help inspect older work. CSV export requires a new filename outside Codex home and will not overwrite an existing file.

Sampling defaults to 5 seconds (range 2–60) and catalog refresh to 60 seconds (30–600). A round shares a 2 MiB JSONL read budget across up to 32 recent files, the selected session, and 64 rotating older files; changed older files can temporarily gain priority. Minimized sampling runs no more often than every 30 seconds, so older changes may appear after rotation or refresh. Privacy mode anonymizes GUI views and exports.

The desktop app provides bounded, read-only diagnosis and an Agent task plan. The accompanying `codex-monitor-agent.exe` offers UTF-8 JSON `capabilities`, `list`, `diagnose`, and `plan` commands. Diagnosis reads at most the first 16 MiB or 2 seconds of a target log and reports coverage. Plans do not apply repairs; Monitor does not rewrite Codex logs or automatically call a model. Disk bytes and image estimates do not measure model context, account usage, or billing.

**Downloads:** `CodexSessionMonitor-1.0.0-windows-x64.zip` (portable GUI and Agent CLI), `CodexSessionMonitor-1.0.0.exe` (standalone GUI), and `SHA256SUMS.txt`. Extract the ZIP and run its GUI executable, or run the standalone EXE directly. The existing `csr` v0.2.0 CLI remains a separate tool with its own explicit `--apply` commands.
