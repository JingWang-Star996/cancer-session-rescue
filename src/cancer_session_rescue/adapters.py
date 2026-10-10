# -*- coding: utf-8 -*-
"""
Harness 适配器：把不同 agent 框架的会话记录格式映射到统一模型。

统一模型：
  classify(record) -> 'tool' | 'message' | 'other'
  tool_types() / message_types() -> 类型名集合（文档用途）
  truncate_tool_record(record, keep_chars) -> 0|1|2  (0=未动 1=截了输出 2=截了输入)
  privacy_rules() -> [(pattern, replacement), ...] 隐私脱敏规则
  default_roots() -> 常见会话根目录（供 scan 用）
"""
import os


class WorkBuddyAdapter:
    """WorkBuddy: ~/.workbuddy/projects/**/*.jsonl
    记录: {type: function_call|function_call_result|message|user_message|..., input/output/content}
    """

    def tool_types(self):
        return {"function_call", "function_call_result"}

    def message_types(self):
        return {"message", "user_message", "assistant_message"}

    def classify(self, o):
        t = o.get("type")
        if t in self.tool_types():
            return "tool"
        if t in self.message_types():
            return "message"
        return "other"

    def truncate_tool_record(self, o, keep_chars):
        n = 0
        if o.get("type") == "function_call_result":
            out = o.get("output")
            if isinstance(out, dict) and len(out.get("text", "")) > 400:
                txt = out["text"]
                out["text"] = txt[:keep_chars] + f" ...[truncated, was {len(txt)} chars]"
                n = 1
            elif isinstance(out, str) and len(out) > 400:
                o["output"] = out[:keep_chars] + f" ...[truncated, was {len(out)} chars]"
                n = 1
        else:  # function_call
            inp = o.get("input")
            if inp is not None:
                s = json.dumps(inp, ensure_ascii=False) if (json := __import__("json")) else ""
                if len(s) > 600:
                    o["input"] = {"_truncated": s[:300] + f"...[was {len(s)} chars]"}
                    n = 2
        return n

    def privacy_rules(self, username=None):
        rules = []
        if username:
            rules.append((f"C:\\Users\\{username}", "~"))
            rules.append((f"/c/Users/{username}", "~"))
            rules.append((f"/Users/{username}", "~"))
        return rules

    def default_roots(self):
        home = os.path.expanduser("~")
        return [os.path.join(home, ".workbuddy", "projects")]


class CodexAdapter:
    """OpenAI Codex CLI/Desktop: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl
    记录: {type: response_item|event_msg|session_meta|...,
           payload: {type: message|custom_tool_call|custom_tool_call_output|reasoning|...}}
    隐私点: session_meta 含 cwd(绝对路径)、creator_account_id(部分版本)。
    """

    def tool_types(self):
        return {"response_item:custom_tool_call", "response_item:custom_tool_call_output",
                "response_item:function_call", "response_item:function_call_output"}

    def message_types(self):
        return {"response_item:message", "response_item:agent_message", "response_item:reasoning"}

    def classify(self, o):
        t = o.get("type")
        pay = o.get("payload") or {}
        pt = pay.get("type") if isinstance(pay, dict) else None
        key = f"{t}:{pt}"
        if key in self.tool_types() or pt in ("custom_tool_call", "custom_tool_call_output",
                                              "function_call", "function_call_output",
                                              "local_shell_call", "local_shell_call_output"):
            return "tool"
        if key in self.message_types() or pt in ("message", "agent_message", "reasoning"):
            return "message"
        return "other"

    def truncate_tool_record(self, o, keep_chars):
        pay = o.get("payload")
        if not isinstance(pay, dict):
            return 0
        pt = pay.get("type", "")
        if "output" in pt:
            out = pay.get("output")
            # Codex 输出结构: {type, output} 或直接 str
            text = None
            holder = None
            if isinstance(out, dict) and isinstance(out.get("output"), str):
                holder, text = out, out["output"]
            elif isinstance(out, str):
                holder, text = pay, out
            if holder is not None and len(text) > 400:
                holder["output"] = text[:keep_chars] + f" ...[truncated, was {len(text)} chars]"
                return 1
        else:  # call 侧：截 arguments / command
            for field in ("arguments", "input"):
                v = pay.get(field)
                if isinstance(v, str) and len(v) > 600:
                    pay[field] = v[:300] + f"...[was {len(v)} chars]"
                    return 2
                if isinstance(v, (list, dict)):
                    import json as _json
                    s = _json.dumps(v, ensure_ascii=False)
                    if len(s) > 600:
                        pay[field] = {"_truncated": s[:300] + f"...[was {len(s)} chars]"}
                        return 2
        return 0

    def privacy_rules(self, username=None):
        rules = []
        if username:
            for prefix in (f"C:\\Users\\{username}", f"/c/Users/{username}", f"/Users/{username}"):
                rules.append((prefix, "~"))
        rules.append(("creator_user_id", "creator_user_id"))  # no-op 占位：字段名保留
        return rules

    def default_roots(self):
        home = os.path.expanduser("~")
        return [os.path.join(home, ".codex", "sessions"),
                os.path.join(home, ".codex", "archived_sessions")]


class GenericAdapter:
    """兜底：任何 JSONL。把超长行按字段启发式截断；消息类仅 message/content/text 字段保留。"""

    def tool_types(self):
        return {"*"}

    def message_types(self):
        return {"message"}

    def classify(self, o):
        t = str(o.get("type", "")).lower()
        if any(k in t for k in ("call", "tool", "result", "output", "observation")):
            return "tool"
        if any(k in t for k in ("message", "assistant", "user")):
            return "message"
        return "other"

    def truncate_tool_record(self, o, keep_chars):
        # 启发式：找最长的字符串值截断
        best_key, best_val = None, ""
        for k, v in o.items():
            if isinstance(v, str) and len(v) > len(best_val):
                best_key, best_val = k, v
            if isinstance(v, dict):
                for k2, v2 in v.items():
                    if isinstance(v2, str) and len(v2) > len(best_val):
                        best_key, best_val = f"{k}.{k2}", v2
        if best_key and len(best_val) > 800:
            cur = o
            keys = best_key.split(".")
            for k in keys[:-1]:
                cur = cur[k]
            cur[keys[-1]] = best_val[:keep_chars] + f" ...[truncated, was {len(best_val)} chars]"
            return 1
        return 0

    def privacy_rules(self, username=None):
        return []


ADAPTERS = {
    "workbuddy": WorkBuddyAdapter,
    "codex": CodexAdapter,
    "generic": GenericAdapter,
}


def get_adapter(name):
    cls = ADAPTERS.get(name)
    if not cls:
        raise SystemExit(f"未知 harness: {name}。可选: {', '.join(ADAPTERS)}")
    return cls()
