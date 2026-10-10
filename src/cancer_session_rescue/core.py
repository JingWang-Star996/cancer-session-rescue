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


def slim_stream(src_path, dst_path, adapter, sanitizer_rules=None, keep_chars=TOOL_OUT_KEEP):
    """
    流式压缩一个会话 jsonl。
    adapter 决定哪些记录是"工具类"（可截断），哪些是"消息类"（必须原样保留文字）。
    返回 stats dict。
    """
    compiled = compile_sanitizer(sanitizer_rules) if sanitizer_rules else []
    stats = {"lines": 0, "bad_lines": 0, "img": 0, "cut_outputs": 0, "cut_calls": 0,
             "sanitized": 0, "in_bytes": 0, "out_bytes": 0}
    tool_types = adapter.tool_types()
    msg_types = adapter.message_types()

    with open(src_path, encoding="utf-8", errors="replace") as fi, \
         open(dst_path, "w", encoding="utf-8") as fo:
        for line in fi:
            stats["lines"] += 1
            raw = len(line.encode("utf-8", errors="replace"))
            stats["in_bytes"] += raw
            try:
                o = json.loads(line)
            except Exception:
                stats["bad_lines"] += 1
                fo.write(line)
                stats["out_bytes"] += raw
                continue

            changed = False
            # Layer 1: 剥图（全类型）
            if "base64" in line or "data:image" in line:
                strip_b64(o, stats)
                changed = True

            kind = adapter.classify(o)  # 'tool' | 'message' | 'other'
            if kind == "tool":
                n = adapter.truncate_tool_record(o, keep_chars)
                if n:
                    stats["cut_outputs" if n == 1 else "cut_calls"] += 1
                    changed = True
            # message 类型只剥图不截断，保证对话可读

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
                newline = json.dumps(o, ensure_ascii=False) + "\n"
                out = len(newline.encode("utf-8"))
                stats["out_bytes"] += out
                fo.write(newline)
            else:
                stats["out_bytes"] += raw
                fo.write(line)
    return stats


def human(n_bytes):
    return f"{n_bytes/1048576:.1f}MB"
