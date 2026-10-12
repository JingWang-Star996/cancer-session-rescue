# 会话体积优化任务包说明

Agent API 1.0 只做有界只读诊断与生成任务包。诊断结果的图片体积是扫描区域粗估；部分覆盖时不能外推全文件，更不能将磁盘体积当作 Token 或账单。

计划按以下顺序设置执行门槛：保留原目标和 checkpoint；识别所有 `history_base` 段与分页索引；冻结源文件和索引的 SHA-256 备份；验证单会话卸载、无 writer 和独占句柄；仅考虑有效 PNG/JPEG `response_item.payload.output[*]` 的 `input_image.image_url` 替换，并拒绝重复 JSON key 或无效签名，证明非图片内容精确不变；事务更新分页偏移和 projection cursor；注入失败做回滚演练；通过 Codex 原生接口读回完整历史与原目标后恢复。

本地 `cancer-session-rescue` 的严格无写 dry-run 和 image-only 约束属于有案例的经验，通用生产 apply 尚未在此监控器中验证，也未授权。本地 1.0 门槛优先；公开仓库 README 的通用 `Codex --apply` 示例不能直接运行。此仓库不复制第三方源码，后续如复用须先核实许可证与实现版本。
