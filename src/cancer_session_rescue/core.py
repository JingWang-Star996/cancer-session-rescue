# -*- coding: utf-8 -*-
"""
核心引擎：JSONL 会话记录的两层压缩 + 可选脱敏。
与具体 harness（WorkBuddy / Codex / ...）解耦——记录类型由 adapter 决定。
"""
import json
import re

# ---------- 默认参数 ----------
B64_MIN_LEN = 5000
TOOL_OUT_KEEP = 200
TOOL_OUT_THRESHOLD = 400
CALL_IN_KEEP = 300
CALL_IN_THRESHOLD = 600

REDACT = "[REDACTED]"


def looks_like_b64(s: str) -> bool:
    return s.startswith("data:image") or (len(s) > B64_MIN_LEN and "base64" in s[:200])


def strip_b64(obj, stats):
    """递归剥离 base64 图片字符串 -> 占位符。所有记录类型通用。"""
    if isinstance(obj, dict):
        for k in list(obj.keys()):
            v = obj[k]
            if isinstance(v, str) and looks_like_b64(v):
                obj[k] = f"[image stripped, was {len(v)//1024}KB]"
                stats["img"] += 1
            else:
                strip_b64(v, stats)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str) and looks_like_b64(v):
                obj[i] = f"[image stripped, was {len(v)//1024}KB]"
                stats["img"] += 1
            else:
                strip_b64(v, stats)


def compile_sanitizer(rules):
    """rules: list of (pattern:str, replacement:str)。返回按长度降序的已编译正则列表。"""
    compiled = []
    for pat, rep in sorted(rules, key=lambda x: -len(x[0])):
        try:
            compiled.append((re.compile(re.escape(pat)), rep))
        except re.error:
            continue
    return compiled


def sanitize_string(s: str, compiled) -> str:
    for rx, rep in compiled:
        s = rx.sub(rep, s)
    return s


def sanitize_obj(obj, compiled, stats):
    """递归脱敏所有字符串值与 key。"""
    if isinstance(obj, dict):
        for k in list(obj.keys()):
            v = obj[k]
            if isinstance(v, str):
                if compiled:
                    obj[k] = sanitize_string(v, compiled)
                    if obj[k] != v:
                        stats["sanitized"] += 1
            else:
                sanitize_obj(v, compiled, stats)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str):
                if compiled:
                    obj[i] = sanitize_string(v, compiled)
                    if obj[i] != v:
                        stats["sanitized"] += 1
            else:
                sanitize_obj(v, compiled, stats)


# ---------- Layer 3: 推理全文剥离（v2 实战发现的藏毒点） ----------
# agent 会话里推理全文（模型 thinking）会藏在三处，全部不是对话内容：
#   ① message.content[] 中 type=="reasoning" 的块
#   ② 任意层级的 providerData.reasoning（WorkBuddy 实测 1.41MB/会话）
#   ③ rawContent 字段（WorkBuddy 实测 1.19MB/会话）

def _truncate_reason_value(val, stats, keep=300):
    full = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
    if len(full) <= 400:
        return False
    if isinstance(val, str):
        obj_new = val[:keep] + f" ...[reasoning truncated, was {len(val)} chars]"
    else:
        obj_new = f"[reasoning stripped, was {len(full)//1024}KB]"
    return obj_new


def strip_reasoning(obj, stats, keep=300):
    """递归剥离三处 reasoning 藏毒点。消息正文不受影响。"""
    if isinstance(obj, dict):
        for k in list(obj.keys()):
            v = obj[k]
            if k == "rawContent" and isinstance(v, (list, dict)):
                full = json.dumps(v, ensure_ascii=False)
                if len(full) > 400:
                    obj[k] = f"[rawContent stripped, was {len(full)//1024}KB]"
                    stats["reasoning"] += 1
                continue
            if k == "providerData" and isinstance(v, dict):
                for pk in list(v.keys()):
                    if "reason" in pk.lower():
                        nv = _truncate_reason_value(v[pk], stats, keep)
                        if nv is not False and nv is not None:
                            v[pk] = nv
                            stats["reasoning"] += 1
                continue
            if k == "content" and isinstance(v, list):
                for item in v:
                    if isinstance(item, dict) and item.get("type") == "reasoning":
                        for rk in ("reasoning", "summary", "text", "thinking"):
                            if rk in item:
                                nv = _truncate_reason_value(item[rk], stats, keep)
                                if nv is not False and nv is not None:
                                    item[rk] = nv
                                    stats["reasoning"] += 1
            strip_reasoning(v, stats, keep)
    elif isinstance(obj, list):
        for v in obj:
            strip_reasoning(v, stats, keep)


def detect_newline(src, sample_bytes=200000):
    """Layer 4: 探测行尾风格。Windows 部分会话文件是 CRLF（实测占比 25/39）。
    用 join('\\n') 重拼 CRLF 文件会产生混合行尾/坏行——写出时必须按原风格还原。"""
    with open(src, "rb") as f:
        data = f.read(sample_bytes)
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n")
    return "\r\n" if crlf > lf / 2 and lf > 0 else "\n"


def slim_stream(src_path, dst_path, adapter, sanitizer_rules=None, keep_chars=TOOL_OUT_KEEP,
                deep=False):
    """
    流式压缩一个会话 jsonl（五层）。
    adapter 决定哪些记录是"工具类"（可截断），哪些是"消息类"（必须原样保留文字）。
    deep=True 启用 Layer 3 推理/rawContent 剥离（v2 实战推荐）。
    返回 stats dict。
    """
    compiled = compile_sanitizer(sanitizer_rules) if sanitizer_rules else []
    stats = {"lines": 0, "bad_lines": 0, "img": 0, "cut_outputs": 0, "cut_calls": 0,
             "sanitized": 0, "reasoning": 0, "in_bytes": 0, "out_bytes": 0}
    newline_style = detect_newline(src_path)  # Layer 4: CRLF 安全

    with open(src_path, encoding="utf-8", errors="replace") as fi, \
         open(dst_path, "w", encoding="utf-8", newline="") as fo:
        for line in fi:
            stats["lines"] += 1
            raw = len(line.encode("utf-8", errors="replace"))
            stats["in_bytes"] += raw
            stripped = line.rstrip("\r\n")
            try:
                o = json.loads(stripped)
            except Exception:
                stats["bad_lines"] += 1
                fo.write(stripped + newline_style)
                stats["out_bytes"] += raw
                continue

            changed = False
            # Layer 1: 剥图（全类型）
            if "base64" in stripped or "data:image" in stripped:
                strip_b64(o, stats)
                changed = True

            # Layer 3: 剥推理/rawContent（deep 模式，全类型）
            if deep and ("reasoning" in stripped.lower() or "rawcontent" in stripped.lower()):
                strip_reasoning(o, stats)
                changed = True

            kind = adapter.classify(o)  # 'tool' | 'message' | 'other'
            if kind == "tool":
                n = adapter.truncate_tool_record(o, keep_chars)
                if n:
                    stats["cut_outputs" if n == 1 else "cut_calls"] += 1
                    changed = True
            # message 类型只剥图/剥推理不截断，保证对话可读

            # 脱敏（全类型，含消息——用户名出现在对话里也要抹）
            if compiled:
                before = json.dumps(o, sort_keys=True, default=str)
                sanitize_obj(o, compiled, stats)
                if json.dumps(o, sort_keys=True, default=str) != before:
                    changed = True

            if kind == "message":
                stats.setdefault("messages_kept", 0)
                stats["messages_kept"] += 1

            if changed:
                newline = json.dumps(o, ensure_ascii=False) + newline_style
                out = len(newline.encode("utf-8"))
                stats["out_bytes"] += out
                fo.write(newline)
            else:
                # 统一行尾为探测到的风格（Layer 4: 防混合行尾）
                body = stripped
                out = len((body + newline_style).encode("utf-8"))
                stats["out_bytes"] += out
                fo.write(body + newline_style)
    return stats


def human(n_bytes):
    return f"{n_bytes/1048576:.1f}MB"
