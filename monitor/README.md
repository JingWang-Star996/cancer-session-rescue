# Codex Session Monitor 1.0

[Download Windows release](https://github.com/JingWang-Star996/cancer-session-rescue/releases/tag/monitor-v1.0.0) · [Agent integration](../docs/monitor-agent-api.md) · [中文功能说明](CHANGELOG.md)

The Windows x64 portable ZIP includes a native dashboard and a standalone JSON CLI. Extract it, then open **Codex 会话监控.exe**. No Python installation is needed. The separate EXE download runs the dashboard; download the ZIP for the standalone Agent CLI and documentation.

## Monitor

- Recent sessions first; 100 rows per page, search/sort/filter and CSV export across all matching pages.
- Sampling interval 2–60 seconds (default 5); catalog interval 30–600 seconds (default 60), applied without restarting.
- Up to 32 recent files plus the selected file, and 64 rotating cold-file checks during a regular sample. Changed files may temporarily join the recent set.
- A shared 2 MiB JSONL read budget per poll; cold histories are not all parsed on startup. Minimized windows sample no more often than every 30 seconds.
- Pause/resume, anonymous display/export, separate archived and unarchived sizes, and bounded diagnosis with an Agent maintenance plan.

File size is disk storage, not context tokens. The output rate is a 60-second average of observed logged token increments, not token streaming. Cold-session values can lag; unknown baselines show a dash. This tool never rewrites Codex logs or maintains pagination indexes automatically.

## Agent CLI

```powershell
.\codex-monitor-agent.exe capabilities
.\codex-monitor-agent.exe list --privacy --page 1 --limit 100
.\codex-monitor-agent.exe diagnose --session '12345678-1234-1234-1234-1234567890ab' --private --output diagnosis.json
.\codex-monitor-agent.exe plan --session '12345678-1234-1234-1234-1234567890ab' --private --output plan.json
```

Replace the example UUID with a real local session ID. Use `--codex-home 'path'` for a different data directory. UTF-8 JSON is written to stdout, or to a new file via `--output`; existing files and paths inside Codex home are rejected. `--private` allows identifying metadata; `--privacy` anonymizes it. CLI privacy flags are independent of the dashboard setting.

Diagnosis reads at most the first 16 MiB or two seconds of a selected log. Partial coverage is explicit. Lexical image markers and validated image candidates are separate statistics; neither is a guarantee of reclaimable storage. Plans preserve the original task and require backups, stopped writers, all history segments, index coordination, rollback, and native readback. No `apply` command is provided.

## Run from source / build

Python 3.11 with Tk is required for the dashboard. The standalone CLI uses only the standard library.

```powershell
python app.py
python agent_api.py capabilities
python -B -m unittest test_monitor_core test_integration test_agent_api
.\build.ps1
```

`build.ps1` creates a local build environment if absent and installs the pinned build dependency. Executables are produced in `dist/`. The monitor is independent of the existing `csr` v0.2.0 command; its version and safety boundaries must not be inferred from older compression examples.

Released under the repository's [MIT license](../LICENSE). Bundled runtime notices are included in the portable ZIP.
