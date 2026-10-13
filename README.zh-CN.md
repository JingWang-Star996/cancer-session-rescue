# Codex 会话监控 1.1.1 与 cancer-session-rescue

[English](README.md) | [简体中文](README.zh-CN.md)

**Codex 会话监控 1.1.1** 是查看本机 Codex 会话的 Windows 桌面程序，可观察文件增长、进行有界诊断，并生成供人工或 Agent 审阅的任务包。它不改写会话日志、不执行修复、不自动调用模型，也不重启 Codex。

本仓库还包含原有的 **cancer-session-rescue（`csr`）v0.2.0** 命令行工具，用于 WorkBuddy、Codex 和通用 JSONL 日志。它的独立命令在明确使用 `--apply` 时可以改写文件；这不是监控器 1.1.1 的能力。

## 下载 Windows 版监控器

前往 [Monitor 1.1.1 Release](https://github.com/JingWang-Star996/cancer-session-rescue/releases/tag/monitor-v1.1.1) 下载：

| 文件 | 用途 |
|---|---|
| `CodexSessionMonitor-1.1.1-windows-x64.zip` | 免安装便携包，含桌面程序与 `codex-monitor-agent.exe`；解压即可运行，无需安装 Python。 |
| `CodexSessionMonitor-1.1.1.exe` | 独立的桌面图形程序；需要 Agent CLI 时请下载 ZIP。 |
| `SHA256SUMS.txt` | 用于核对下载文件的 SHA-256。 |

支持 Windows x64。将 ZIP 解压到可写文件夹，运行其中的图形程序；独立 EXE 可直接运行。程序优先读取 `CODEX_HOME`，否则使用当前用户的 `.codex` 目录。另见[中文版](docs/monitor-1.1.1-release.zh-CN.md)与[英文版](docs/monitor-1.1.1-release.md)发布说明。

## 1.1.1 新增功能

- **简化模式汇总范围：**保留单会话，并新增近 60 秒有输出、最近 24 小时、全部未归档、仅主会话、仅子会话、当前列表筛选、全目录（含归档）七种汇总。前五种汇总只含未归档会话；当前列表筛选按最新快照统计所有匹配页。
- **自选指标与置顶：**例如选「全部未归档」＋「60 秒输出」＋置顶，就能用小窗观察整体输出。范围和单会话选择会保留，隐私开关同时作用于两种界面。Ctrl+M 切换简化模式，Ctrl+T 切换置顶。
- **可选输入、缓存与累计记录：**在「设置 → Token 与简化模式」开启记录，默认关闭。开启后显示 60 秒输入、缓存输入、缓存命中率，以及已记录输入／输出／缓存累计。关闭后停止记录，已有记录保留。
- **小窗口布局优化：**列表优先占用空间，详情按需展开，小窗自动收起概览卡片；保留采样间隔、巡检间隔和每页条数设置。

汇总复用现有采样快照，不增加日志扫描；显示已知覆盖数量，部分数据标注「小计」，全未知显示 `—`。缓存命中率按有效配对的缓存输入合计除以输入合计计算，不平均各会话百分比。缓存是输入的一部分，不重复相加。

累计首先采用尾部最新可用账务累计，再加入确认的增量。统计保存在 `%LOCALAPPDATA%/CodexSessionMonitor/usage.sqlite3`，只含会话 ID、计数与核对状态，不含正文、标题或图片。有界尾读无法补齐旧的计数重置；冷会话可能延迟。跨会话合计不去重共享历史，不能视为账户账单。60 秒是统计窗口，与设置中的 2–60 秒采样间隔不同；日志批量记账会造成延迟。

Agent JSON 协议保持 1.0；CLI 未开放 GUI 私有累计库、持续订阅或简化模式汇总接口。完整边界见 [Agent 调用说明](docs/monitor-agent-api.md)。

## 快速上手

1. 打开桌面程序。默认显示最近 24 小时活动的未归档会话，每页 100 个；可切换全部会话并纳入已归档会话。
2. 选中会话，查看文件体积、近期增长和详情。“诊断与优化”会读取有界证据并生成可复制的 Agent 任务包；它不会自动修复。
3. 导出或分享前，可开启隐私模式以隐藏身份和路径。CSV 导出须选 Codex home 之外的新文件名，不覆盖已有文件。匿名任务包无法直接定位原会话；如需 Agent 处理，须另行提供目标。

采样默认每 5 秒一次，可设为 2–60 秒；目录巡检默认每 60 秒一次，可设为 30–600 秒。所有会话每轮共用 2 MiB JSONL 读取预算，检查最多 32 个近期会话、另加所选会话，并轮转最多 64 个旧会话；检测到变化的旧会话可临时提高优先级。最小化后采样间隔至少 30 秒。旧会话变化可能要等轮转或巡检后才出现。文件字节和图片估算值不等于模型上下文 Token、账户用量或账单。

## Agent 命令行

ZIP 内含独立的 UTF-8 JSON 命令行程序。在解压目录打开 PowerShell：

```powershell
.\codex-monitor-agent.exe capabilities
.\codex-monitor-agent.exe list --privacy --page 1 --limit 100
.\codex-monitor-agent.exe diagnose --session '<会话UUID>' --private --output .\diagnosis.json
.\codex-monitor-agent.exe plan --session '<会话UUID>' --privacy --output .\plan.json
```

请将带引号的占位符替换为真实会话 UUID。`--privacy` 输出匿名信息，`--private` 明确允许身份信息进入报告。CLI 不读取图形界面的隐私偏好。`--output` 必须指向 Codex home 之外的新文件，已有文件不会被覆盖。诊断最多读取目标日志开头 16 MiB 或运行 2 秒；覆盖不完整时，不能断言未扫描部分没有图片。字段、边界和源码运行方式见 [Agent API 说明](docs/monitor-agent-api.md)。

## 原有 `csr` v0.2.0 CLI

`csr` 是仓库原有的会话排查和瘦身工具。克隆仓库后可用 `pip install -e .` 安装（Python 3.8+），并用 `csr --help` 查看命令。

```bash
csr triage codex
csr triage workbuddy
csr triage generic --dir /path/to/jsonl
csr slim codex /path/to/rollout.jsonl --sanitize
```

原有 `csr` 工具的五项处理与保护机制如下：

1. **剥离图片：**将符合条件的内嵌图片数据替换为体积标记。
2. **截短工具流量：**缩短超长工具输出和调用参数，保留消息类记录。
3. **可选剥离推理文本（`--deep`）：**移除对话正文以外的指定推理和原始内容字段。
4. **保留换行风格：**重写 JSONL 时探测并保留原换行方式，包括 Windows CRLF。
5. **可选路径脱敏（`--sanitize`）：**替换用户名路径；不能保证清除所有秘密。

这些都属于 **`csr`**，不属于监控器。`csr slim` 默认 dry run；`csr slim ... --apply` 和 `csr slim-dir ... --apply` 是独立的写入操作。对重要会话使用前应检查源文件和备份行为。

[提交 `44e68b0` 时的原 README](https://github.com/JingWang-Star996/cancer-session-rescue/blob/44e68b0/README.zh-CN.md) 保留了旧版案例与示例。其中的 `csr --apply` 示例只针对旧工具，不是监控器使用说明。

监控器任务包不授权或执行 `csr --apply`。任何后续修复都须独立核查当前 Codex 运行状态、会话状态、源文件与索引备份，并通过原生接口读回历史。监控器 1.1.1 没有生产日志改写命令。

## 源码与许可

监控器源码在 [`monitor/`](monitor/)，原有 `csr` 源码在 [`src/cancer_session_rescue/`](src/cancer_session_rescue/)。另见 [Agent API](docs/monitor-agent-api.md) 和仓库 [LICENSE](LICENSE)。
