# Codex 会话监控 1.1.1 · Agent API 1.0

产品版本是 `1.1.1`，JSON 协议仍为 `schema_version=1.0`。先执行 `capabilities` 核对 `product_version`、`commands` 和能力边界，再按需调用 `list → diagnose → plan`。

## 1.1.1 的 GUI 与 CLI 边界

本版 GUI 新增可选的 60 秒输入、缓存、持久累计记录，以及简化模式的七种汇总范围。这些属于运行中的桌面采样器；CLI 仍只有 `capabilities/list/diagnose/plan` 四个命令，没有读取 GUI 私有 `usage.sqlite3`、持续 Token 订阅、简化模式汇总或自动调用模型的接口。不要把一次 `list` 返回的元数据或尚未建立基线的速率当成完整用量。需要持续观察时，在 GUI 设置中开启 Token 记录，并按需选择简化模式范围和指标。

缓存 Token 属于输入 Token；缓存命中率使用有效配对的缓存输入之和除以输入之和。已记录累计不保证补齐旧日志计数重置，不代表账单。切勿把 GUI 的新增功能解释为 Agent API 的新增命令。

## 调用与返回

桌面配套文件夹提供 `codex-monitor-agent.exe`，无需 Python。源码入口为 `python agent_api.py capabilities`（Python 3.11 标准库）。将下方示例的 `python agent_api.py` 换成 `.\codex-monitor-agent.exe` 即可运行。`agent_api.diagnose_session(home, sid, privacy=False) -> dict` 与 `optimization.build_plan(home, sid, privacy=False) -> dict` 可直接调用。标准输出和错误输出均使用 UTF-8 JSON。

命令：

```powershell
python agent_api.py list --codex-home C:\Users\you\.codex --privacy --page 1 --limit 100
python agent_api.py diagnose --codex-home C:\Users\you\.codex --session 00000000-0000-0000-0000-000000000000 --private --output diagnosis.json
python agent_api.py plan --codex-home C:\Users\you\.codex --session 00000000-0000-0000-0000-000000000000 --privacy --output plan.json
```

`--private` 明确允许 ID、标题、路径进入输出；`--privacy` 只输出匿名别名与非身份统计。`--output` 只写报告，不改源日志；报告必须位于 Codex home 外，使用独占新建，已有文件不会被覆盖。`list` 默认每页 100，最多 100；它使用 MonitorEngine 的 catalog 元数据，不读取每个会话的完整日志。会话 ID 必须是 UUID 形式，并且源 JSONL 解析后位于指定 Codex home 的 `sessions` 或 `archived_sessions` 内。越界符号链接、越界目录链接和不匹配文件名被拒绝。

`diagnose` 读取目标日志开头最多 16 MiB 或 2 秒，逐块扫描；完整 JSON 事件单行最多 2 MiB。超长行仍以块扫描图片标记，但不解析事件类型。`coverage.status=partial` 表示未读区域、不完整尾行或扫描中源文件发生变化，不能据此断言没有图片。`image_estimate.count/decoded_bytes_approx` 是词法粗估，可能包含非图片字段；`eligible_image_count/eligible_image_bytes` 则只统计完整、无重复键的 JSON 中 `response_item.payload.output[*]` 的 `input_image.image_url`，要求 PNG/JPEG 的 base64 和文件签名有效。它仍不表示实际可释放量。文件字节、图片估算、上下文 Token、账户用量和账单是不同指标。

输出 `schema_version=1.0`，`kind` 为 `capabilities/session_list/diagnosis/plan`。诊断含 `session`、`coverage`、`image_estimate`、`events`、`warnings`；`coverage.notes` 是中文字符串数组。事件类型仅汇总已知类型，其他值计入 `unknown`，不回传原文。计划额外含按顺序执行的中文 `steps`、可复制给 Agent 的中文 `prompt`、`execution.apply_available=false`。本接口没有生产应用或恢复动作；原生历史读回是后续执行者必须完成的验收。
