#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cancer-session-rescue 统一 CLI

子命令：
  triage  <harness> [--top N]          体检：列出最大的会话文件+体积构成+风险评级
  slim    <harness> <file.jsonl> [--apply] [--backup] [--sanitize] [--keep-chars N]
                                       压缩单个会话（默认 dry-run）
  slim-dir <harness> <dir> [--apply] [--min-mb 2] [--sanitize]
                                       批量压缩目录下所有超重会话
  stop    <harness> <session-prefix>   强制停止运行中的会话（仅 workbuddy 实现）

示例：
  python -m cancer_session_rescue triage codex
  python -m cancer_session_rescue slim codex "C:/Users/me/.codex/sessions/.../rollout-xxx.jsonl" --apply --sanitize
"""
import argparse
import json
import os
import shutil
import sys
import time

from .core import slim_stream, human, TOOL_OUT_KEEP
from .adapters import get_adapter


def cmd_triage(args):
    adapter = get_adapter(args.harness)
    roots = [args.dir] if args.dir else adapter.default_roots()
    files = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _, names in os.walk(root):
            for n in names:
                if n.endswith(".jsonl"):
                    p = os.path.join(dirpath, n)
                    files.append((os.path.getsize(p), p))
    files.sort(reverse=True)
    total = sum(s for s, _ in files)
    print(f"共 {len(files)} 个 .jsonl, 总计 {total/1048576:.0f} MB")
    print(f"\n最大的 {min(args.top, len(files))} 个:")
    for s, p in files[:args.top]:
        mb = s / 1048576
        risk = "🔴" if mb >= 10 else ("🟠" if mb >= 5 else ("🟡" if mb >= 2 else "🟢"))
        print(f"  {mb:8.1f}MB {risk} {p}")


def _sanitize_rules(adapter, enabled):
    if not enabled:
        return None
    username = os.environ.get("CSR_USERNAME") or os.path.basename(os.path.expanduser("~"))
    return adapter.privacy_rules(username)


def cmd_slim(args):
    adapter = get_adapter(args.harness)
    src = args.file
    if not os.path.isfile(src):
        print(f"错误: 文件不存在 {src}")
        sys.exit(1)
    size = os.path.getsize(src)
    print(f"源文件: {src} ({human(size)})")
    if not args.apply:
        print("(dry-run 模式：加 --apply 才写出)\n")

    rules = _sanitize_rules(adapter, args.sanitize)
    if args.sanitize and rules:
        print(f"脱敏规则 {len(rules)} 条（用户名 -> ~ 等）")

    tmp = src + ".sliming"
    stats = slim_stream(src, tmp, adapter, sanitizer_rules=rules, keep_chars=args.keep_chars,
                        deep=args.deep)
    print(f"行数: {stats['lines']} (坏行 {stats['bad_lines']})")
    print(f"体积: {human(stats['in_bytes'])} -> {human(stats['out_bytes'])}  "
          f"压缩率 {100 - stats['out_bytes']*100//max(stats['in_bytes'],1)}%")
    print(f"剥图 {stats['img']} 处 | 截断工具输出 {stats['cut_outputs']} 条 | 截断调用 {stats['cut_calls']} 条"
          + (f" | 剥推理 {stats.get('reasoning', 0)} 处" if args.deep else "")
          + (f" | 脱敏 {stats['sanitized']} 处" if args.sanitize else ""))

    if not args.apply:
        os.remove(tmp)
        return

    if args.backup:
        bak = src + f".bak-{size//1048576}MB"
        shutil.copy2(src, bak)
        print(f"备份: {bak}")

    # 校验
    bad = 0
    with open(tmp, encoding="utf-8") as f:
        for ln in f:
            try:
                json.loads(ln)
            except Exception:
                bad += 1
    if bad:
        os.remove(tmp)
        print(f"❌ 压缩结果有 {bad} 行损坏，已放弃（原文件未动）")
        sys.exit(1)

    replaced = src + f".replaced-{time.strftime('%Y%m%d-%H%M%S')}"
    os.rename(src, replaced)
    shutil.move(tmp, src)
    print(f"✅ 完成。原文件保留为: {replaced}")


def cmd_slim_dir(args):
    adapter = get_adapter(args.harness)
    roots = [args.dir] if args.dir else adapter.default_roots()
    rules = _sanitize_rules(adapter, args.sanitize)
    username = os.environ.get("CSR_USERNAME") or os.path.basename(os.path.expanduser("~"))
    now = time.time()
    stamp = time.strftime("%Y%m%d-%H%M%S")

    targets = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for dirpath, _, names in os.walk(root):
            for n in names:
                if not n.endswith(".jsonl"):
                    continue
                p = os.path.join(dirpath, n)
                if any(m in n for m in (".slim", ".replaced", ".bak")):
                    continue
                if os.path.getsize(p) < args.min_mb * 1048576:
                    continue
                # 跳过 2 分钟内还在写的
                if now - os.path.getmtime(p) < 120:
                    print(f"跳过(写入中): {p}")
                    continue
                targets.append((os.path.getsize(p), p))
    targets.sort(reverse=True)
    print(f"目标 {len(targets)} 个 (≥{args.min_mb}MB)\n")

    total_in = total_out = 0
    done = 0
    for i, (s, src) in enumerate(targets, 1):
        try:
            tmp = src + ".sliming"
            st = slim_stream(src, tmp, adapter, sanitizer_rules=rules, keep_chars=args.keep_chars,
                             deep=args.deep)
        except Exception as e:
            print(f"{i:3}. ❌ {os.path.basename(src)[:50]}: {e}")
            continue
        bad = 0
        with open(tmp, encoding="utf-8") as f:
            for ln in f:
                try:
                    json.loads(ln)
                except Exception:
                    bad += 1
        if bad:
            os.remove(tmp)
            print(f"{i:3}. ❌ {os.path.basename(src)[:50]}: 压缩后损坏，已回滚")
            continue
        if args.backup:
            shutil.copy2(src, src + f".bak-{s//1048576}MB")
        os.rename(src, src + f".replaced-{stamp}")
        shutil.move(tmp, src)
        total_in += st["in_bytes"]
        total_out += st["out_bytes"]
        done += 1
        print(f"{i:3}. ✅ {s/1048576:7.1f}→{st['out_bytes']/1048576:6.1f}MB {os.path.basename(src)[:52]}")

    print(f"\n===== 总账: {done} 个文件 {total_in/1048576:.0f}MB -> {total_out/1048576:.0f}MB "
          f"(释放 {(total_in-total_out)/1048576:.0f}MB) =====")


def cmd_stop(args):
    if args.harness != "workbuddy":
        print("stop 目前仅支持 workbuddy（其会话注册表机制已知）。其他 harness 的会话通常直接杀进程即可。")
        sys.exit(1)
    # 动态复用 WorkBuddy 技能里的实现（若本机有），否则提示
    skill = os.path.expanduser("~/.workbuddy/skills/session-first-aid/scripts/session_stop.py")
    if os.path.isfile(skill):
        import subprocess
        cmd = [sys.executable, skill, "stop", "--session", args.session]
        if args.yes:
            cmd.append("--yes")
        sys.exit(subprocess.call(cmd))
    print("未找到 session_stop.py，请参考 docs/stop.md 手动操作")


def main():
    ap = argparse.ArgumentParser(prog="cancer-session-rescue",
                                 description="诊断并抢救膨胀到卡死的 agent 会话记录")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("triage", help="体检：找癌症会话")
    p.add_argument("harness", choices=["workbuddy", "codex", "generic"])
    p.add_argument("--dir", help="自定义目录（默认用 harness 已知根目录）")
    p.add_argument("--top", type=int, default=20)
    p.set_defaults(func=cmd_triage)

    p = sub.add_parser("slim", help="压缩单个会话")
    p.add_argument("harness", choices=["workbuddy", "codex", "generic"])
    p.add_argument("file")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--backup", action="store_true")
    p.add_argument("--sanitize", action="store_true", help="脱敏用户名路径")
    p.add_argument("--deep", action="store_true", help="剥离推理全文/rawContent（Layer 3，推荐）")
    p.add_argument("--keep-chars", type=int, default=TOOL_OUT_KEEP)
    p.set_defaults(func=cmd_slim)

    p = sub.add_parser("slim-dir", help="批量压缩目录")
    p.add_argument("harness", choices=["workbuddy", "codex", "generic"])
    p.add_argument("dir", nargs="?", help="目录（省略则用 harness 默认根）")
    p.add_argument("--apply", action="store_true")
    p.add_argument("--backup", action="store_true")
    p.add_argument("--sanitize", action="store_true")
    p.add_argument("--deep", action="store_true", help="剥离推理全文/rawContent（Layer 3）")
    p.add_argument("--min-mb", type=float, default=2.0)
    p.add_argument("--keep-chars", type=int, default=TOOL_OUT_KEEP)
    p.set_defaults(func=cmd_slim_dir)

    p = sub.add_parser("stop", help="强制停止会话（workbuddy）")
    p.add_argument("harness", choices=["workbuddy"])
    p.add_argument("session", help="sessionId 前缀")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_stop)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
