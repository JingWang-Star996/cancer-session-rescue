# Codex 会话监控 1.0.0

[中文 README](https://github.com/JingWang-Star996/cancer-session-rescue/blob/monitor-v1.0.0/README.zh-CN.md) · [English notes](https://github.com/JingWang-Star996/cancer-session-rescue/blob/monitor-v1.0.0/docs/monitor-1.0-release.md) · [Agent API](https://github.com/JingWang-Star996/cancer-session-rescue/blob/monitor-v1.0.0/docs/monitor-agent-api.md)

本次发布为仓库加入免安装的 Windows x64 Codex 会话监控桌面程序。默认按每页 100 个显示最近 24 小时活动的未归档会话；可搜索、排序、纳入已归档会话，并导出 CSV 以查看旧会话。CSV 导出须选择 Codex home 之外的新文件名，已有文件不会被覆盖。

采样默认每 5 秒一次（可设 2–60 秒），目录巡检默认每 60 秒一次（可设 30–600 秒）。每轮由最多 32 个近期会话、另加所选会话和最多 64 个轮转旧会话共用 2 MiB JSONL 读取预算；检测到变化的旧会话可临时提高优先级。最小化后采样间隔至少 30 秒，旧会话变化可能延迟到轮转或巡检后显示。隐私模式会匿名化界面与导出。

桌面程序提供有界只读诊断和 Agent 任务包。配套 `codex-monitor-agent.exe` 通过 UTF-8 JSON 提供 `capabilities`、`list`、`diagnose`、`plan` 命令。诊断最多读取目标日志开头 16 MiB 或运行 2 秒，并报告覆盖情况。任务包不执行修复；监控器不会改写 Codex 日志或自动调用模型。磁盘字节和图片估算值不代表模型上下文、账户用量或账单。

**下载文件：**`CodexSessionMonitor-1.0.0-windows-x64.zip`（便携图形程序和 Agent CLI）、`CodexSessionMonitor-1.0.0.exe`（独立图形程序）、`SHA256SUMS.txt`。解压 ZIP 后运行其中的图形程序，或直接运行独立 EXE。原有 `csr` v0.2.0 CLI 是另一套工具，只有明确使用其 `--apply` 命令才进入写入流程。
