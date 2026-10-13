"""Bounded, read-only JSON interface for Codex session metadata and diagnosis."""

from __future__ import annotations

import argparse
import base64
import binascii
from collections import Counter
import json
import os
from pathlib import Path
import re
import sys
import time

from monitor_core import MonitorEngine


SCHEMA_VERSION = "1.0"
DEFAULT_BUDGET = 16 * 1024 * 1024
MAX_LINE = 2 * 1024 * 1024
TIME_BUDGET = 2.0
_SID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z", re.I)
_IMAGE = re.compile(rb'data:image/[a-zA-Z0-9.+-]+;base64,')
_IMAGE_PREFIX_OVERLAP = 80
_KNOWN_EVENTS = {"session_meta", "turn_context", "event_msg", "response_item", "compacted"}
_INLINE_IMAGE = re.compile(r"\Adata:image/(png|jpeg);base64,([A-Za-z0-9+/]+={0,2})\Z", re.I)


class DuplicateKeyError(ValueError):
    pass


def _home_path(home: str | Path) -> Path:
    path = Path(home).expanduser().resolve(strict=True)
    if not path.is_dir():
        raise ValueError("Codex home is not a directory")
    return path


def _catalog(home: Path) -> tuple[list, list[str]]:
    engine = MonitorEngine(home, max_bytes_per_poll=0, max_cold_stats_per_poll=0)
    engine._catalog()  # Metadata only; no rollout content is opened.
    return list(engine.sessions.values()), list(engine._errors)


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _source(home: Path, sid: str):
    if not _SID.fullmatch(sid):
        raise ValueError("Invalid session ID")
    rows, _ = _catalog(home)
    match = next((row for row in rows if row.id.lower() == sid.lower()), None)
    if match is None:
        raise ValueError("Session not found")
    source = match.path.resolve(strict=True)
    _source_from_row(home, match)
    return match, source


def _session_info(item, source: Path, privacy: bool) -> dict:
    base = {"archived": bool(item.meta.get("archived")), "size_bytes": source.stat().st_size}
    if privacy:
        return {"alias": "session-1", **base}
    return {"id": item.id, "title": item.meta.get("title") or item.id,
            "path": str(source), **base}


def list_sessions(home: str | Path, *, page: int = 1, limit: int = 100,
                  privacy: bool = False) -> dict:
    if page < 1 or not 1 <= limit <= 100:
        raise ValueError("page must be positive and limit must be 1..100")
    root = _home_path(home)
    rows, errors = _catalog(root)
    valid = []
    for item in rows:
        try:
            _, source = _source_from_row(root, item)
            valid.append((item, source))
        except (OSError, ValueError):
            continue
    valid.sort(key=lambda pair: (-pair[0].mtime, pair[0].id))
    start = (page - 1) * limit
    selected = valid[start:start + limit]
    sessions = []
    for offset, (item, source) in enumerate(selected, start=start + 1):
        info = _session_info(item, source, privacy)
        if privacy:
            info["alias"] = f"session-{offset}"
        info["mtime"] = item.mtime
        info["model"] = item.meta.get("model", "")
        info["is_subagent"] = bool(item.meta.get("is_subagent"))
        sessions.append(info)
    return {"schema_version": SCHEMA_VERSION, "kind": "session_list", "page": page,
            "limit": limit, "total": len(valid), "sessions": sessions,
            "warnings": ["Catalog metadata may be incomplete"] if errors else []}


def _source_from_row(home: Path, item):
    source = item.path.resolve(strict=True)
    roots = [(home / name).resolve() for name in ("sessions", "archived_sessions")]
    if source.suffix.lower() != ".jsonl" or not any(
            _inside(root, home) and _inside(source, root) for root in roots):
        raise ValueError("outside home")
    if not _SID.fullmatch(item.id) or item.id.lower() not in source.name.lower():
        raise ValueError("invalid source ID")
    return item, source


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError("duplicate JSON key")
        result[key] = value
    return result


def _eligible_images(record: dict) -> tuple[int, int]:
    if record.get("type") != "response_item":
        return 0, 0
    payload = record.get("payload")
    output = payload.get("output") if isinstance(payload, dict) else None
    if not isinstance(output, list):
        return 0, 0
    count = total = 0
    for entry in output:
        if not isinstance(entry, dict) or entry.get("type") != "input_image":
            continue
        url = entry.get("image_url")
        match = _INLINE_IMAGE.fullmatch(url) if isinstance(url, str) else None
        if match is None:
            continue
        try:
            decoded = base64.b64decode(match.group(2), validate=True)
        except (binascii.Error, ValueError):
            continue
        signature = (decoded.startswith(b"\x89PNG\r\n\x1a\n") if match.group(1).lower() == "png"
                     else decoded.startswith(b"\xff\xd8\xff"))
        if signature:
            count += 1
            total += len(decoded)
    return count, total


def capabilities() -> dict:
    return {"schema_version": SCHEMA_VERSION, "product_version": "1.1.1", "kind": "capabilities",
            "commands": ["capabilities", "list", "diagnose", "plan"],
            "diagnosis_budget_bytes": DEFAULT_BUDGET,
            "diagnosis_budget_seconds": TIME_BUDGET, "max_line_bytes": MAX_LINE,
            "source_writes": False, "apply_available": False,
            "privacy": "--privacy anonymizes session identity and source paths; private output requires --private"}


def _image_scan(data: bytes, state: dict) -> None:
    """Count inline image characters across chunks without retaining payloads."""
    data = state["tail"] + data
    pos = 0
    while pos < len(data):
        if state["inside"]:
            end = data.find(b'"', pos)
            if end < 0:
                state["characters"] += len(data) - pos
                state["tail"] = b""
                return
            state["characters"] += end - pos
            state["inside"] = False
            pos = end + 1
            continue
        found = _IMAGE.search(data, pos)
        if found is None:
            state["tail"] = data[max(pos, len(data) - _IMAGE_PREFIX_OVERLAP):]
            return
        state["count"] += 1
        state["inside"] = True
        pos = found.end()
    state["tail"] = b""


def diagnose_session(home: str | Path, sid: str, privacy: bool = False) -> dict:
    root = _home_path(home)
    item, source = _source(root, sid)
    size = source.stat().st_size
    started = time.monotonic()
    read_bytes = valid = invalid = oversize = duplicate = eligible_count = eligible_bytes = 0
    types = Counter()
    line = bytearray()
    line_size = 0
    image = {"tail": b"", "inside": False, "count": 0, "characters": 0}
    complete = True
    with source.open("rb") as stream:
        before = os.fstat(stream.fileno())
        while read_bytes < min(size, DEFAULT_BUDGET):
            if time.monotonic() - started >= TIME_BUDGET:
                complete = False
                break
            chunk = stream.read(min(64 * 1024, DEFAULT_BUDGET - read_bytes, size - read_bytes))
            if not chunk:
                break
            read_bytes += len(chunk)
            _image_scan(chunk, image)
            for part in chunk.splitlines(keepends=True):
                line_size += len(part)
                if line_size <= MAX_LINE:
                    line.extend(part)
                if part.endswith(b"\n"):
                    if line_size > MAX_LINE:
                        oversize += 1
                    else:
                        try:
                            obj = json.loads(line.decode("utf-8"), object_pairs_hook=_unique_object)
                            if not isinstance(obj, dict):
                                raise ValueError("JSON event is not an object")
                            valid += 1
                            event_type = obj.get("type")
                            types[event_type if isinstance(event_type, str) and event_type in _KNOWN_EVENTS
                                  else "unknown"] += 1
                            found, decoded_bytes = _eligible_images(obj)
                            eligible_count += found
                            eligible_bytes += decoded_bytes
                        except DuplicateKeyError:
                            invalid += 1
                            duplicate += 1
                        except (UnicodeError, ValueError):
                            invalid += 1
                    line.clear()
                    line_size = 0
        if read_bytes < size:
            complete = False
        after = os.fstat(stream.fileno())
    current = source.stat()
    changed = ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) !=
               (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or
               (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) !=
               (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns))
    if changed:
        complete = False
    partial = not complete or bool(line_size)
    warnings = []
    if partial:
        warnings.append("部分覆盖：未读区域或不完整记录可能仍含图片和事件。")
    if oversize:
        warnings.append("超长记录只扫描了图片标记，未解析事件类型或校验图片资格。")
    if invalid:
        warnings.append("发现无效 UTF-8、JSON 或重复键记录。")
    if changed:
        warnings.append("扫描期间源文件发生变化，结果仅为部分覆盖。")
    return {"schema_version": SCHEMA_VERSION, "kind": "diagnosis",
            "session": _session_info(item, source, privacy),
            "coverage": {"status": "complete" if not partial else "partial",
                         "read_bytes": read_bytes, "total_bytes": size,
                         "budget_bytes": DEFAULT_BUDGET, "time_budget_seconds": TIME_BUDGET,
                         "complete": not partial,
                         "notes": ["部分扫描不能证明未读区域没有图片。",
                                   "文件字节数和图片估算不等于上下文 Token、用量或账单。"]},
            "image_estimate": {"count": image["count"],
                               "base64_bytes": image["characters"],
                               "decoded_bytes_approx": image["characters"] * 3 // 4,
                               "method": "词法扫描 data:image 标记；可能包含非图片字段，仅作粗估。",
                               "eligible_image_count": eligible_count,
                               "eligible_image_bytes": eligible_bytes,
                               "eligible_method": "仅统计完整 JSON 中有效 PNG/JPEG 签名的 output input_image；不代表实际可释放量。"},
            "events": {"types": dict(types), "valid_json_lines": valid,
                       "invalid_json_lines": invalid, "duplicate_key_lines": duplicate,
                       "oversize_lines": oversize,
                       "incomplete_tail": bool(line_size)},
            "warnings": warnings}


def run_cli(argv: list[str] | None = None) -> int:
    # Machine consumers must receive the same UTF-8 JSON under every Windows locale.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="strict")
    parser = argparse.ArgumentParser(description="Read-only Codex session Agent API")
    parser.add_argument("command", choices=("capabilities", "list", "diagnose", "plan"))
    parser.add_argument("--codex-home", default=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    parser.add_argument("--session")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--privacy", action="store_true")
    parser.add_argument("--private", action="store_true", help="Allow identifying metadata in JSON")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    try:
        if not args.privacy and not args.private and args.command != "capabilities":
            raise ValueError("Identifying output requires --private or use --privacy")
        if args.command == "capabilities":
            result = capabilities()
        elif args.command == "list":
            result = list_sessions(args.codex_home, page=args.page, limit=args.limit,
                                   privacy=args.privacy)
        else:
            if not args.session:
                raise ValueError("--session is required")
            if args.command == "diagnose":
                result = diagnose_session(args.codex_home, args.session, privacy=args.privacy)
            else:
                from optimization import build_plan
                result = build_plan(args.codex_home, args.session, privacy=args.privacy)
        rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            report_parent = args.output.parent.resolve(strict=True)
            if _inside(report_parent, Path(args.codex_home).expanduser().resolve()):
                raise ValueError("报告路径不能位于 Codex home 内")
            with args.output.open("x", encoding="utf-8") as report:
                report.write(rendered)
        else:
            sys.stdout.write(rendered)
        return 0
    except (OSError, ValueError) as exc:
        sys.stderr.write(json.dumps({"error": str(exc) if not args.privacy else "Request failed",
                                     "kind": "error"}, ensure_ascii=False) + "\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(run_cli())
