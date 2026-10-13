# Codex Session Monitor 1.1.1 Agent API

[README](../README.md) · [中文 README](../README.zh-CN.md) · [Release](monitor-1.1.1-release.md)

## Product version and protocol compatibility

`capabilities` returns `product_version: "1.1.1"`; successful payloads still use `schema_version: "1.0"`. Commands remain `capabilities`, `list`, `diagnose`, and `plan`. GUI input/cache recording, persistent usage totals, and compact aggregate scopes do **not** add Agent commands. This CLI does not open the GUI's private `usage.sqlite3`, provide live Token subscriptions, or return compact-mode aggregates. A one-shot list cannot establish a continuous rate baseline or complete historical usage.

For an Agent handoff: inspect capabilities, find the target with `list --private` locally, diagnose that exact UUID, inspect `coverage` and `warnings`, then generate a plan. Provide the original task goal and target separately to the receiving Agent when sharing an anonymous plan. The plan is a review artifact; it does not run or authorize repairs. Use a fresh report path for each call.

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

The plan describes checkpoints and gates for a separate human or Agent workflow. Do not treat it as permission to run `csr --apply`: that belongs to the older `csr` CLI and has different behavior. A subsequent repair must independently verify the current runtime and session, source and index backups, absence of active writers, and native Codex history readback. Monitor 1.1.1 provides no production log rewrite command.

---

# Agent 接口简要说明

Windows 便携包内的 `codex-monitor-agent.exe` 输出 UTF-8 JSON；源码入口是 `python monitor/agent_api.py`（Python 3.11）。接口只读，不自动调用模型或修复会话。`capabilities`、`list`、`diagnose`、`plan` 用法见上方示例。

除 `capabilities` 外，须明确使用 `--privacy`（匿名）或 `--private`（允许身份字段）。CLI 不沿用图形界面的隐私开关。`--output` 只能在 Codex home 外独占新建报告，不能覆盖已有文件。`list` 默认每页 100，最多 100；`diagnose` 最多读取开头 16 MiB 或 2 秒。`coverage.status=partial` 表示不能对未读部分下结论。图片估算、文件字节、上下文 Token、用量和账单不可互换。计划中的 `execution.apply_available=false` 表示监控器没有生产写入功能；任何后续动作需要另行核查与授权。


## 1.1.1 调用边界与建议流程

产品版本为 `1.1.1`，JSON 协议仍为 `1.0`。先调用 `capabilities` 核对版本和命令，再在本机用 `list --private` 定位真实 UUID，依次 `diagnose`、检查 `coverage/warnings`、`plan`。每次报告使用新的文件名。向其他 Agent 提供匿名任务包时，须另行提供目标和原始任务约束。

GUI 的 60 秒输入／缓存、持久累计和七种小窗汇总不是新增 CLI 能力；CLI 不读取 GUI 私有累计库、不提供持续 Token 订阅或汇总范围参数。需要持续观察时使用 GUI 设置开关。缓存输入包含在总输入中；有界采样不能补齐旧重置，跨会话合计未去重共享历史，不能据此计算账户账单。监控器不自动调用其他 Agent。
