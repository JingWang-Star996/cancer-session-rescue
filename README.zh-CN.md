# cancer-session-rescue

[English](README.md) | [简体中文](README.zh-CN.md)

**诊断并抢救"癌症会话"——膨胀到数百 MB、一打开就卡死应用的 agent 对话记录。**

适用于任何以 JSONL 存储会话的 agent 框架。内置 **WorkBuddy** 和 **OpenAI Codex CLI/Desktop** 适配器，另有启发式 `generic` 适配器兜底其余框架。

```
修复前:  rollout-xxx.jsonl   355.6 MB   ← 打开即卡死
修复后:    同一文件           34.0 MB   ← 正常打开，对话完整
```

## 会话为什么会癌变

长会话会不断堆积机器产物，每次重新打开会话时全部灌进模型上下文：

| 毒素 | 典型占比 | 后果 |
|---|---|---|
| base64 截图 | 视觉类工作流中占 60–90% | 多模态上下文爆炸 |
| 冗长工具输出（验证循环、长命令回显） | 10–90% | 持续挤占上下文窗口 |
| 反复"打补丁→修复→验证"循环 | — | 以上两种叠加恶化 |

而真正的人机对话通常**不到文件体积的 5%**。

## 安装

```bash
pip install git+https://github.com/<you>/cancer-session-rescue.git
# 或本地克隆后：
pip install -e .
```

Python 3.8+，零第三方依赖。

## 快速上手

```bash
# 1. 体检：找出癌症会话
csr triage codex            # 扫描 ~/.codex/sessions 与 ~/.codex/archived_sessions
csr triage workbuddy        # 扫描 ~/.workbuddy/projects
csr triage generic --dir /任意/jsonl/目录

# 2. 压缩单个会话（默认 dry-run，不写任何东西）
csr slim codex ~/.codex/sessions/2026/07/15/rollout-xxx.jsonl --sanitize

# 3. 确认无误后正式执行（原文件自动改名保留）
csr slim codex rollout-xxx.jsonl --apply --sanitize

# 4. 或一键批量压缩目录下所有 ≥2MB 的会话
csr slim-dir codex --apply --sanitize
```

**安全设计**：压缩结果先逐行 JSON 校验，通过后才替换原文件；校验失败则什么都不改。原文件一律保留为 `*.replaced-<时间戳>`（加 `--backup` 另存 `*.bak-*MB`）。

## 工作原理

两层压缩，作用于每条记录：

1. **剥图**：所有 `data:image` / base64 大块 → `[image stripped, was N KB]`
2. **清洗工具流量**：超过 400 字符的工具输出截断为前 200 字符 + 尺寸备注；超过 600 字符的调用参数截断为前 300。**消息类记录绝不截断**——对话永远完整可读。

可选**脱敏**（`--sanitize`）：把 `C:\Users\<用户名>\...`、`/Users/<用户名>/...` 重写为 `~`，让会话文件可以直接当 bug 报告分享而不泄露用户名。实测 355MB 文件中改写 910 处，改写后残留 0 处。

## 安全模型

| 保证 | 实现方式 |
|---|---|
| 原文件绝不销毁 | 先改名后替换，原文件保留为 `.replaced-*` |
| 无静默损坏 | 替换前逐行解析校验，任何坏行立即中止 |
| 不碰活跃会话 | 批量模式自动跳过 2 分钟内有写入的文件 |
| 对话保真 | 工具截断层永不处理消息类记录 |
| 先看后动 | `slim` 不带 `--apply` 不写出任何文件 |

## 框架适配器

| 框架 | 状态 | 会话位置 | 说明 |
|---|---|---|---|
| **WorkBuddy** | 实战验证 | `~/.workbuddy/projects/**/*.jsonl` | 附带 `stop` 子命令，可强制停止运行中的会话 |
| **Codex CLI/Desktop** | 实战验证 | `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl` | 处理 `response_item`/`payload.type` 嵌套、`custom_tool_call_output`、`compacted` 记录；`ordinal` 顺序保留，`codex resume` 不受影响 |
| **generic** | 启发式 | 任意 `.jsonl` | 按类型名关键词分类，截断最长字符串字段 |

写新适配器约 60 行：实现 `classify()`、`truncate_tool_record()`、`privacy_rules()` 即可，参考 `src/cancer_session_rescue/adapters.py`。

## 实战战绩（2026-10，一台 Windows 机器）

| 框架 | 处理文件数 | 修复前 | 修复后 | 释放 |
|---|---|---|---|---|
| WorkBuddy | 37 个（含 11 个 ≥10MB 癌症级） | 374 MB | 111 MB | 263 MB |
| Codex | 1 个（单文件之王） | 355.6 MB | 34.0 MB | −91% |

那个 90.9MB 的 WorkBuddy 会话含 390 张 base64 截图和约 90MB Playwright 验证输出；那个 355MB 的 Codex 会话含 667 张图和 890 条超长工具调用。

## 野外手册：预防癌症会话的习惯

1. **别让 agent 反复截图**——base64 是头号毒源。让它跑脚本输出文字结论，图你自己开浏览器看。
2. **验证脚本先核对再跑循环**——有个会话就死在一个查不存在 DOM 元素的死循环里（"验证永不通过"式假卡死）。
3. **会话变卡变笨及时换新**——带走的应该是结论摘要，不是整个历史。
4. **先只读体检**——永远别为了"看一眼"而加载巨型会话；用 `csr triage`，它流式统计、不载入正文。

## FAQ

**压缩后 Codex 还能 resume 吗？**
能——记录顺序（`ordinal`）和全部消息/推理记录原样保留，我们只瘦身工具流量的 payload，不动账本结构。已在 16,761 条记录的 rollout 上验证。

**应用开着时能压缩吗？**
批量模式自动跳过最近 2 分钟有修改的文件。单文件压缩请先关闭该会话——WorkBuddy 的 `stop` 子命令通过归档运行时注册表记录来释放文件句柄。

**脱敏能处理密钥吗？**
默认只重写用户名路径。要分享文件前，可在适配器的 `privacy_rules()` 里扩展 token/邮箱规则。

## 许可

MIT
