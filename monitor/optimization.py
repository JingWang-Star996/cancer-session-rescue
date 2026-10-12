"""Read-only recovery task plans; production mutation is intentionally absent."""

from __future__ import annotations

from pathlib import Path


def build_plan(home: str | Path, sid: str, privacy: bool = False) -> dict:
    from agent_api import SCHEMA_VERSION, diagnose_session

    diagnosis = diagnose_session(home, sid, privacy=privacy)
    session = diagnosis["session"]
    target = ("该匿名会话；执行前请通过私下授权渠道取得会话 ID 与 Codex home"
              if privacy else f"会话 {session['id']}（Codex home：{Path(home).expanduser().resolve()}）")
    steps = [
        {"id": "checkpoint", "action": "维护前保留原任务、当前状态和恢复 checkpoint。",
         "gate": "checkpoint 能恢复同一任务与会话。"},
        {"id": "history", "action": "识别所有 rollout 段，包括 history_base 续段及分页历史索引引用。",
         "gate": "冻结完整段与索引清单。"},
        {"id": "backup", "action": "为每个源文件和相关索引制作不可变备份及 SHA-256 清单。",
         "gate": "备份哈希与源快照一致，且可恢复。"},
        {"id": "unload", "action": "验证单会话归档或卸载、静止状态，以及所有段的独占无 writer 访问。",
         "gate": "仅有暂停或归档标签不足以通过。"},
        {"id": "transform", "action": "仅考虑替换 response_item.payload.output 中有效 PNG/JPEG input_image.image_url；拒绝重复 JSON 键和无效签名，并证明所有非图片数据精确不变。",
         "gate": "不做通用截断或未经审查的 apply；非图片内容完全保留。"},
        {"id": "index", "action": "经验证的段替换后，事务性重映射分页历史字节偏移与 projection cursor。",
         "gate": "索引与每个改写段一致，并可回滚。"},
        {"id": "rollback", "action": "生产改写前注入失败并演练回滚。",
         "gate": "原字节、哈希和索引状态均能精确恢复。"},
        {"id": "readback", "action": "恢复后通过 Codex 原生接口读回同一会话的完整历史及目标状态。",
         "gate": "所有轮次、条目与原目标齐全，然后才能继续任务。"},
    ]
    prompt_lines = [
        f"调查并准备安全恢复：{target}。",
        "在主协调者授权并验证具体生产流程之前，这只是一份只读计划。",
        f"有界诊断：覆盖状态 {diagnosis['coverage']['status']}，读取 {diagnosis['coverage']['read_bytes']} / {diagnosis['coverage']['total_bytes']} 字节；扫描区发现 {diagnosis['image_estimate']['count']} 个词法图片标记，粗估解码后 {diagnosis['image_estimate']['decoded_bytes_approx']} 字节；其中经字段与签名校验的候选图片 {diagnosis['image_estimate']['eligible_image_count']} 个、{diagnosis['image_estimate']['eligible_image_bytes']} 字节。",
        "部分扫描不能证明未读区域没有图片。文件字节和图片估算均不等于上下文 Token、用量或账单。",
        "按顺序满足以下门槛：",
    ]
    prompt_lines.extend(f"{index}. {step['action']} 验收：{step['gate']}" for index, step in enumerate(steps, 1))
    prompt_lines.append("以本地 1.0 安全门槛为准；不要直接运行网页示例中的通用 Codex --apply 命令。")
    prompt_lines.append("回传证据路径、源文件与备份哈希、精确改动、原生读回结果、未解决风险和恢复 checkpoint。")
    return {"schema_version": SCHEMA_VERSION, "kind": "plan", "session": session,
            "diagnosis": diagnosis, "steps": steps, "prompt": "\n".join(prompt_lines),
            "execution": {"apply_available": False, "source_writes": False},
            "warnings": ["本接口未验证生产改写，也不提供 apply。"]}
