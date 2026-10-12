"""Read-only, bounded observation of local Codex rollout logs.

Rates are logged output tokens observed in the last window divided by the
window length. They are accounting averages, not a live streaming speed.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from typing import Callable


_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)


class _Session:
    def __init__(self, sid: str, path: Path, stat: os.stat_result, meta: dict,
                 now: float, *, new_file: bool = False):
        self.id = sid
        self.path = path
        self.size = stat.st_size
        self.mtime = stat.st_mtime
        self.activity_at = stat.st_mtime
        self.identity = (stat.st_dev, stat.st_ino)
        self.meta = meta
        self.offset = 0 if new_file else stat.st_size
        self.pending = b""
        self.skip_line = False
        self.baseline_partial = False
        self.baseline_end = stat.st_size
        self.baseline_done = new_file
        self.total_output: int | None = 0 if new_file else None
        self.observed = 0
        self.last_token_at: float | None = None
        self.last_output_tokens: int | None = None
        self.growth_bps = 0.0
        self.sample_at = now
        self.tokens = deque()
        self.history = deque()
        self.warning = ""
        self.coverage = "新文件从起始追踪" if new_file else "初始基线待读取"
        self.last_event = ""
        self.checked_at = 0.0


class MonitorEngine:
    """Poll local session files without altering Codex state.

    ``clock`` and IO limits are optional so tests can exercise the window.
    An incomplete/oversize JSONL record is never counted. Files first found
    during monitoring start at their current length, excluding old usage.
    """

    def __init__(self, codex_home: str | Path, window_seconds: float = 60,
                 *, clock: Callable[[], float] = time.time,
                 max_bytes_per_poll: int = 2 * 1024 * 1024,
                 max_poll_seconds: float = 0.75,
                 max_line_bytes: int = 1024 * 1024,
                 sampling_interval: float = 5.0,
                 catalog_interval: float = 60.0,
                 max_cold_stats_per_poll: int = 64,
                 max_hot_sessions: int = 32):
        self.home = Path(codex_home)
        self._started_wall_ns = time.time_ns()
        self.window = float(window_seconds)
        if self.window <= 0:
            raise ValueError("window_seconds must be positive")
        self.clock = clock
        self.max_bytes = max_bytes_per_poll
        self.max_seconds = max_poll_seconds
        self.max_line = max_line_bytes
        if sampling_interval <= 0 or catalog_interval < 0 or max_cold_stats_per_poll < 0 or max_hot_sessions < 1:
            raise ValueError("invalid sampling limits")
        self.sampling_interval = float(sampling_interval)
        self.catalog_interval = float(catalog_interval)
        self.max_cold_stats_per_poll = max_cold_stats_per_poll
        self.max_hot_sessions = max_hot_sessions
        self.sessions: dict[str, _Session] = {}
        self._last_catalog: float | None = None
        self._last_sample: float | None = None
        self._refresh_requested = False
        self._focus_id: str | None = None
        self._hot_ids: set[str] = set()
        self._cold_cursor = 0
        self._last_diagnostics: dict = {}
        self._round = 0
        self._increment_cursor = 0
        self._errors: list[str] = []
        self._index: dict[str, str] = {}

    def _read_index(self):
        path = self.home / "session_index.jsonl"
        if not path.is_file():
            return
        try:
            # Index is small metadata; cap work if a corrupt installation grows it.
            with path.open("rb") as stream:
                read_bytes, skip_line = 0, False
                while read_bytes < 8 * 1024 * 1024:
                    line = stream.readline(min(self.max_line + 1, 8 * 1024 * 1024 - read_bytes))
                    if not line:
                        break
                    read_bytes += len(line)
                    if skip_line:
                        skip_line = not line.endswith(b"\n")
                        continue
                    if len(line) > self.max_line:
                        skip_line = not line.endswith(b"\n")
                        self._errors.append("标题索引有超长行，已跳过")
                        continue
                    try:
                        item = json.loads(line)
                        if isinstance(item, dict) and isinstance(item.get("id"), str):
                            self._index[item["id"]] = str(item.get("thread_name") or "")
                    except (ValueError, TypeError):
                        continue
                if read_bytes >= 8 * 1024 * 1024:
                    self._errors.append("标题索引达到 8 MiB 读取上限，后续标题未读取")
        except OSError as exc:
            self._errors.append(f"读取标题索引失败: {exc}")

    def _catalog(self):
        rows: dict[str, tuple[Path, dict]] = {}
        db = self.home / "state_5.sqlite"
        if db.is_file():
            try:
                # mode=ro avoids an accidental DB/WAL creation or migration.
                with sqlite3.connect(db.as_uri() + "?mode=ro", uri=True, timeout=0.2) as conn:
                    query = ("SELECT id,rollout_path,title,cwd,model,agent_role,"
                             "agent_path,archived FROM threads")
                    for sid, raw, title, cwd, model, role, agent_path, archived in conn.execute(query):
                        if not sid or not raw:
                            continue
                        rows[sid] = (Path(raw), {"title": title or "", "cwd": cwd or "",
                                                 "model": model or "", "is_subagent": bool(role or agent_path),
                                                 "archived": bool(archived)})
            except (sqlite3.Error, OSError) as exc:
                self._errors.append(f"只读数据库不可用，改用目录: {exc}")
        self._read_index()
        # Directory enumeration finds rows pending DB commit and is the fallback.
        for folder, archived in ((self.home / "sessions", False),
                                 (self.home / "archived_sessions", True)):
            if not folder.exists():
                continue
            try:
                for root, _, names in os.walk(folder):
                    for name in names:
                        if not name.endswith(".jsonl"):
                            continue
                        ids = _ID.findall(name)
                        if not ids:
                            continue
                        sid = ids[-1].lower()
                        actual = Path(root) / name
                        prior = rows.get(sid)
                        meta = dict(prior[1]) if prior else {"title": "", "cwd": "", "model": "",
                                                            "is_subagent": len(ids) > 1, "archived": archived}
                        meta["archived"] = archived
                        rows[sid] = (actual, meta)
            except OSError as exc:
                self._errors.append(f"扫描会话目录失败 {folder}: {exc}")
        seen: set[str] = set()
        identities: set[tuple[int, int]] = set()
        stat_count = 0
        for sid, (path, meta) in rows.items():
            meta["title"] = self._index.get(sid) or meta.get("title") or sid
            try:
                stat = path.stat()
                stat_count += 1
            except OSError as exc:
                if sid in self.sessions:
                    self.sessions[sid].warning = f"日志文件暂不可读: {exc}"
                else:
                    self._errors.append(f"会话 {sid} 的日志文件不可读: {exc}")
                continue
            identity = (stat.st_dev, stat.st_ino)
            if identity in identities:
                continue
            identities.add(identity)
            seen.add(sid)
            item = self.sessions.get(sid)
            if item is None:
                self.sessions[sid] = _Session(
                    sid, path, stat, meta, self.clock(),
                    new_file=stat.st_ctime_ns > self._started_wall_ns)
            else:
                meta["model"] = meta.get("model") or item.meta.get("model", "")
                meta["cwd"] = meta.get("cwd") or item.meta.get("cwd", "")
                item.meta = meta
                # An archive move may change the path without replacing bytes.
                item.path = path
                if item.identity != (stat.st_dev, stat.st_ino) or stat.st_size < item.offset:
                    self._reset_file(item, stat)
                # Catalog stat is also a sample, including changes to cold files.
                changed = self._apply_stat(item, stat, self.clock())
                if changed and sid not in self._hot_ids and not item.baseline_done:
                    item.offset = item.size
                    item.baseline_end = item.size
        for sid in set(self.sessions) - seen:
            del self.sessions[sid]
        self._hot_ids = set(sid for sid, item in sorted(
            self.sessions.items(), key=lambda row: (-row[1].mtime, row[0]))[:self.max_hot_sessions])
        for sid, item in self.sessions.items():
            if sid not in self._hot_ids and not item.baseline_done and item.offset == 0:
                item.offset = item.size
                item.baseline_end = item.size
                item.coverage = "冷会话未读取日志；点选或检测到变化后建立基线"
            elif sid not in self._hot_ids and item.baseline_done and item.offset == 0:
                item.offset = item.size
                item.baseline_end = item.size
                item.baseline_done = False
                item.total_output = None
                item.coverage = "冷会话未读取日志；点选或检测到变化后建立基线"
        return stat_count

    def _reset_file(self, item: _Session, stat: os.stat_result):
        item.identity = (stat.st_dev, stat.st_ino)
        item.size = stat.st_size
        item.mtime = stat.st_mtime
        item.offset = stat.st_size
        item.baseline_end = stat.st_size
        item.baseline_done = False
        item.pending = b""
        item.skip_line = False
        item.baseline_partial = False
        item.total_output = None
        item.last_output_tokens = None
        item.growth_bps = 0.0
        item.tokens.clear()
        item.last_token_at = None
        item.last_event = ""
        item.history.clear()
        item.warning = "日志被截断或替换；已重新建立计数基线"
        item.coverage = "文件重新建立基线"

    def _event(self, item: _Session, raw: bytes, now: float, *, baseline: bool):
        try:
            record = json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError):
            item.warning = "遇到无效 UTF-8 或 JSON 日志行，已跳过"
            return
        if not isinstance(record, dict):
            item.warning = "遇到非对象 JSON 日志行，已跳过"
            return
        kind = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            return
        if kind == "turn_context":
            item.meta["model"] = payload.get("model") or item.meta.get("model", "")
            item.meta["cwd"] = payload.get("cwd") or item.meta.get("cwd", "")
        elif kind == "event_msg":
            event = payload.get("type")
            if event in ("task_started", "task_complete", "turn_aborted"):
                item.last_event = event
            if event == "token_count":
                info = payload.get("info")
                if not isinstance(info, dict):
                    return
                usage = info.get("total_token_usage")
                if not isinstance(usage, dict):
                    return
                total = usage.get("output_tokens")
                if type(total) is not int or total < 0:
                    return
                last_usage = info.get("last_token_usage")
                current_last = None
                if isinstance(last_usage, dict):
                    last = last_usage.get("output_tokens")
                    if type(last) is int and last >= 0:
                        item.last_output_tokens = last
                        current_last = last
                stamp = record.get("timestamp")
                if isinstance(stamp, str):
                    try:
                        item.last_token_at = datetime.fromisoformat(
                            stamp.replace("Z", "+00:00")).timestamp()
                    except ValueError:
                        pass
                old = item.total_output
                item.total_output = total
                if baseline or total == old:
                    return
                if old is None:
                    # No historical counter was found in the bounded tail.
                    # Only the latest response is known; never charge lifetime total.
                    if current_last is None:
                        item.warning = "初始尾部缺少计数，首笔输出无法确认；后续按增量计数"
                        return
                    delta = min(total, current_last)
                elif total < old:
                    # A reset/compaction starts a fresh cumulative sequence.
                    # The last response usage captures the first new output.
                    if current_last is None:
                        item.warning = "累计 token 重置且缺少末次用量；首个增量无法确认"
                        item.coverage = "重置后首个输出可能漏计"
                        return
                    delta = min(total, current_last)
                else:
                    delta = total - old
                item.observed += delta
                item.tokens.append((now, delta))

    def _feed(self, item: _Session, data: bytes, now: float, *, baseline: bool):
        for piece in data.split(b"\n")[:-1]:
            line = item.pending + piece
            item.pending = b""
            if item.skip_line:
                item.skip_line = False
                item.baseline_partial = False
                continue
            if len(line) > self.max_line:
                item.warning = "超大日志行已跳过，计数可能不完整"
                item.baseline_partial = False
                continue
            if line.strip():
                self._event(item, line, now, baseline=baseline or item.baseline_partial)
            item.baseline_partial = False
        rest = data.rsplit(b"\n", 1)[-1]
        if item.skip_line:
            return
        item.pending += rest
        if len(item.pending) > self.max_line:
            item.pending = b""
            item.skip_line = True
            item.warning = "超大日志行已跳过，计数可能不完整"

    def _baseline(self, item: _Session, now: float, deadline: float, budget: int) -> int:
        # Seek near end; never load historical log content into current rates.
        if item.baseline_done or budget <= 0:
            return 0
        cap = min(item.baseline_end, 64 * 1024, budget)
        if cap <= 0:
            item.baseline_done = True
            item.coverage = "日志从监控启动后计数"
            return 0
        if time.perf_counter() >= deadline:
            return 0
        try:
            with item.path.open("rb") as stream:
                start = item.baseline_end - cap
                stream.seek(start)
                data = stream.read(cap)
            read_bytes = len(data)
            if start:
                first = data.find(b"\n")
                data = data[first + 1:] if first >= 0 else b""
            # Parse only complete lines. A line already in progress at startup
            # belongs to the baseline even if its newline arrives later.
            last_break = data.rfind(b"\n")
            complete = data[:last_break + 1] if last_break >= 0 else b""
            self._feed(item, complete, now, baseline=True)
            item.pending = data[last_break + 1:] if last_break >= 0 else data
            item.baseline_partial = bool(item.pending)
            if len(item.pending) > self.max_line:
                item.pending = b""
                item.skip_line = True
                item.baseline_partial = False
            item.baseline_done = True
            item.coverage = "初始尾部最多 64 KiB；此前历史未计入速率"
            return read_bytes
        except OSError as exc:
            item.warning = f"读取日志基线失败: {exc}"
            return 0

    def _increment(self, item: _Session, now: float, deadline: float, budget: int) -> int:
        if not item.baseline_done or budget <= 0:
            return 0
        try:
            available = item.size - item.offset
            if available <= 0 or time.perf_counter() >= deadline:
                return 0
            amount = min(available, budget, 64 * 1024)
            with item.path.open("rb") as stream:
                stat = os.fstat(stream.fileno())
                if item.identity != (stat.st_dev, stat.st_ino) or stat.st_size < item.offset:
                    self._reset_file(item, stat)
                    return 0
                stream.seek(item.offset)
                data = stream.read(amount)
            item.offset += len(data)
            self._feed(item, data, now, baseline=False)
            if item.offset < item.size:
                item.coverage = f"追赶日志中，尚有 {item.size - item.offset} 字节"
            elif item.warning:
                item.coverage = "部分日志行不可计数"
            else:
                item.coverage = "已追到当前文件末尾"
            return len(data)
        except OSError as exc:
            item.warning = f"读取新增日志失败: {exc}"
            return 0

    def _apply_stat(self, item: _Session, stat: os.stat_result, now: float) -> bool:
        replaced = item.identity != (stat.st_dev, stat.st_ino)
        truncated = stat.st_size < item.size or stat.st_size < item.offset
        if replaced or truncated:
            self._reset_file(item, stat)
            item.sample_at = now
            return True
        interval = now - item.sample_at
        changed = stat.st_size != item.size
        if changed:
            item.activity_at = now
        else:
            item.activity_at = max(item.activity_at, stat.st_mtime)
        item.growth_bps = ((stat.st_size - item.size) / interval
                           if interval > 0 else 0.0)
        item.size, item.mtime = stat.st_size, stat.st_mtime
        if interval > 0:
            item.sample_at = now
        return changed

    def _sample_stat(self, item: _Session, now: float) -> bool:
        try:
            stat = item.path.stat()
        except OSError as exc:
            item.warning = f"读取文件状态失败: {exc}"
            item.growth_bps = 0.0
            return False
        return self._apply_stat(item, stat, now)

    def request_refresh(self):
        """Refresh the directory catalog on the next poll."""
        self._refresh_requested = True

    def focus_session(self, sid: str | None):
        """Prioritize one historical session without rereading it on repeat calls."""
        self._focus_id = sid

    def poll(self) -> dict:
        started = time.perf_counter()
        deadline = started + self.max_seconds
        now = self.clock()
        self._errors = []
        catalog_due = (self._refresh_requested or self._last_catalog is None or
                       now - self._last_catalog >= self.catalog_interval)
        stat_count = 0
        catalog_seconds = 0.0
        if catalog_due:
            catalog_started = time.perf_counter()
            stat_count = self._catalog()
            catalog_seconds = time.perf_counter() - catalog_started
            self._last_catalog = now
            self._refresh_requested = False
        sample_due = (self._last_sample is None or
                      now - self._last_sample >= self.sampling_interval or catalog_due)
        focused = self.sessions.get(self._focus_id) if self._focus_id else None
        hot_ids = set(self._hot_ids)
        if focused is not None:
            hot_ids.add(focused.id)
        cold_stats = 0
        if sample_due:
            if not catalog_due:
                for sid in hot_ids:
                    item = self.sessions.get(sid)
                    if item is not None:
                        changed = self._sample_stat(item, now)
                        stat_count += 1
                        if changed:
                            self._hot_ids.add(sid)
                cold = [item for sid, item in self.sessions.items() if sid not in hot_ids]
                count = min(len(cold), self.max_cold_stats_per_poll)
                for n in range(count):
                    item = cold[(self._cold_cursor + n) % len(cold)]
                    if self._sample_stat(item, now):
                        if not item.baseline_done:
                            # A cold file may have changed at any time since
                            # its last stat; its backlog is not live rate.
                            item.offset = item.size
                            item.baseline_end = item.size
                        self._hot_ids.add(item.id)
                        hot_ids.add(item.id)
                    stat_count += 1
                self._cold_cursor += count
                cold_stats = count
            self._last_sample = now
        if focused is not None and not sample_due:
            self._sample_stat(focused, now)
            stat_count += 1
        if focused is not None:
            hot_ids.add(focused.id)
        remaining = self.max_bytes
        hot = [self.sessions[sid] for sid in hot_ids if sid in self.sessions]
        hot.sort(key=lambda item: (item.id != self._focus_id, -item.mtime, item.id))
        if sample_due or focused is not None:
            for item in hot:
                if remaining <= 0 or time.perf_counter() >= deadline:
                    break
                if not item.baseline_done:
                    remaining -= self._baseline(item, now, deadline, remaining)
        active = [item for item in hot if item.baseline_done and item.size > item.offset]
        while active and remaining > 0 and time.perf_counter() < deadline:
            next_active = []
            count = len(active)
            pivot = self._increment_cursor % count
            ordered = active[pivot:] + active[:pivot]
            self._increment_cursor += 1
            progress = 0
            for item in ordered:
                if remaining <= 0 or time.perf_counter() >= deadline:
                    next_active.append(item)
                    continue
                used = self._increment(item, now, deadline, remaining)
                remaining -= used
                progress += used
                if item.baseline_done and item.size > item.offset:
                    next_active.append(item)
            if progress == 0:
                break
            active = next_active
        read_bytes = self.max_bytes - remaining
        result = []
        for item in self.sessions.values():
            cutoff = now - self.window
            while item.tokens and item.tokens[0][0] <= cutoff:
                item.tokens.popleft()
            token_sum = sum(v for _, v in item.tokens)
            output_tps = token_sum / self.window if item.total_output is not None else None
            item.history.append((now, output_tps))
            while item.history and item.history[0][0] < now - 300:
                item.history.popleft()
            while len(item.history) > 180:
                item.history.popleft()
            if item.meta.get("archived"):
                status = "已归档"
            elif item.growth_bps > 0:
                status = "日志正在增长"
            elif token_sum > 0:
                status = "近 60 秒收到输出"
            elif item.last_event == "task_complete":
                status = "最近记录为任务完成"
            elif item.last_event == "task_started":
                status = "最近记录为任务开始"
            elif now - item.mtime < self.window:
                status = "近期有日志活动"
            else:
                status = "暂无近期日志活动"
            result.append({"id": item.id, "title": item.meta.get("title") or item.id,
                           "path": str(item.path), "cwd": item.meta.get("cwd", ""),
                           "model": item.meta.get("model", ""),
                           "is_subagent": bool(item.meta.get("is_subagent")),
                           "archived": bool(item.meta.get("archived")),
                           "size_bytes": item.size, "mtime": item.mtime, "activity_at": item.activity_at,
                           "growth_bps": item.growth_bps,
                           "output_tps": output_tps,
                           "observed_output_tokens": item.observed,
                           "last_token_at": item.last_token_at,
                           "last_output_tokens": item.last_output_tokens,
                           "status": status,
                           "history": list(item.history),
                           "warning": item.warning, "coverage": item.coverage})
        result.sort(key=lambda x: (-x["mtime"], x["id"]))
        diagnostics = {"read_bytes": read_bytes, "jsonl_read_bytes": read_bytes,
                       "stat_count": stat_count,
                       "hot_count": len(hot), "cold_stats": cold_stats,
                       "catalog_seconds": catalog_seconds, "catalog_scanned": catalog_due,
                       "sampled": sample_due}
        self._last_diagnostics = diagnostics
        return {"sessions": result,
                "generated_at": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "scan_seconds": time.perf_counter() - started,
                "errors": self._errors,
                "diagnostics": diagnostics,
                "total_files": len(result),
                "total_bytes": sum(x["size_bytes"] for x in result)}
