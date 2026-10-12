# Codex Session Monitor 1.0 Agent API

[README](../README.md) · [中文 README](../README.zh-CN.md) · [Release](monitor-1.0-release.md)

The Monitor Agent CLI is a local, read-only interface that emits UTF-8 JSON on both standard output and standard error. The portable Windows ZIP includes `codex-monitor-agent.exe`; no Python installation is needed. From source, use Python 3.11 and run `python monitor/agent_api.py` with the same arguments. This interface does not call a model, change Codex logs or indexes, restart Codex, or apply a repair.

```powershell
.\codex-monitor-agent.exe capabilities
.\codex-monitor-agent.exe list --privacy --page 1 --limit 100
.\codex-monitor-agent.exe diagnose --session '<session-uuid>' --private --output .\diagnosis.json
.\codex-monitor-agent.exe plan --session '<session-uuid>' --privacy --output .\plan.json
```

Replace the quoted session placeholder with the actual UUID. Use `--codex-home '<directory>'` to select a Codex home; the default is `CODEX_HOME`, then the current user's `.codex` directory. Every command except `capabilities` requires an explicit output policy: `--privacy` anonymizes session identifiers, titles and paths, while `--private` permits identifying fields. The CLI has its own policy and does not inherit the GUI privacy toggle. A private report should be handled as a local private file. An anonymous plan needs the target to be supplied separately before an Agent can identify a session.

`--output <file>` creates a report only outside Codex home and only when the target does not exist. It never overwrites a report or modifies the source log. Without `--output`, JSON goes to standard output. `list` uses catalog metadata instead of reading every session log in full. Its page is one-based, `--limit` defaults to 100 and permits 1–100. Session selection requires a UUID and a matching JSONL file within the selected home's `sessions` or `archived_sessions`; paths escaping that home are rejected.

All successful payloads use `schema_version: "1.0"`. `kind` is `capabilities`, `session_list`, `diagnosis`, or `plan`. The list payload includes `page`, `limit`, `total`, and `sessions`. Diagnosis includes `session`, `coverage`, `image_estimate`, `events`, and `warnings`; the plan adds ordered `steps`, a copyable `prompt`, and `execution.apply_available: false`. Consumers should inspect `kind`, schema version, and coverage before using any estimate. Error output is JSON, but it is not a substitute for checking the process exit code.

`diagnose` scans at most the beginning 16 MiB or 2 seconds of the selected JSONL. Complete JSON event lines are parsed only up to 2 MiB; longer lines can still yield image markers from chunk scanning without reliable event classification. `coverage.status: "partial"` can mean an unread region, incomplete tail line, or source change during scanning. It cannot prove absence of images elsewhere. `image_estimate.count` and `decoded_bytes_approx` are lexical estimates. `eligible_image_count` and `eligible_image_bytes` count only complete JSON without duplicate keys, in `response_item.payload.output[*].input_image.image_url`, with valid PNG/JPEG base64 signatures. Eligibility is not actual recoverable disk space. File bytes, image estimates, model context tokens, account usage, and billing are distinct quantities.

The plan describes checkpoints and gates for a separate human or Agent workflow. Do not treat it as permission to run `csr --apply`: that belongs to the older `csr` CLI and has different behavior. A subsequent repair must independently verify the current runtime and session, source and index backups, absence of active writers, and native Codex history readback. Monitor 1.0 provides no production log rewrite command.

---

# Agent 接口简要说明

Windows 便携包内的 `codex-monitor-agent.exe` 输出 UTF-8 JSON；源码入口是 `python monitor/agent_api.py`（Python 3.11）。接口只读，不自动调用模型或修复会话。`capabilities`、`list`、`diagnose`、`plan` 用法见上方示例。

除 `capabilities` 外，须明确使用 `--privacy`（匿名）或 `--private`（允许身份字段）。CLI 不沿用图形界面的隐私开关。`--output` 只能在 Codex home 外独占新建报告，不能覆盖已有文件。`list` 默认每页 100，最多 100；`diagnose` 最多读取开头 16 MiB 或 2 秒。`coverage.status=partial` 表示不能对未读部分下结论。图片估算、文件字节、上下文 Token、用量和账单不可互换。计划中的 `execution.apply_available=false` 表示监控器没有生产写入功能；任何后续动作需要另行核查与授权。
