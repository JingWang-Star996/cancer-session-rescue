"""Codex session monitor: a local, read-only native Windows dashboard."""
from __future__ import annotations

import argparse
import csv
import ctypes
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from urllib.parse import urlparse
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from monitor_core import MonitorEngine

APP_NAME = "Codex 会话监控"
APP_VERSION = "1.0.0"
BG, PANEL, TEXT, MUTED = "#101722", "#192332", "#edf3fc", "#98aac1"
ACCENT, AMBER, RED = "#6ee7c5", "#ffc779", "#ff929f"
CONFIG_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CodexSessionMonitor"
DEFAULTS = {"large_mib": 100.0, "critical_mib": 500.0, "growth_mib_min": 20.0,
            "high_tps": 150.0, "codex_home": os.environ.get("CODEX_HOME", str(Path.home() / ".codex")),
            "privacy_mode": False, "sampling_interval": 5.0, "catalog_interval": 60.0,
            "page_size": 100, "github_url": "https://github.com/JingWang-Star996/cancer-session-rescue"}


def size_text(value):
    if value is None:
        return "—"
    value = max(0, value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:,.1f} {unit}" if unit != "B" else f"{value:,d} B"
        value /= 1024


def age_text(epoch):
    if not epoch:
        return "—"
    seconds = max(0, time.time() - epoch)
    if seconds < 3:
        return "刚刚"
    if seconds < 60:
        return f"{seconds:.0f} 秒前"
    if seconds < 3600:
        return f"{seconds / 60:.0f} 分钟前"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} 小时前"
    return time.strftime("%m-%d %H:%M", time.localtime(epoch))


def load_settings():
    config = dict(DEFAULTS)
    try:
        saved = json.loads((CONFIG_DIR / "settings.json").read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            for key in DEFAULTS:
                value = saved.get(key)
                if key in ("codex_home", "github_url") and isinstance(value, str) and value:
                    config[key] = value
                elif key == "privacy_mode" and isinstance(value, bool):
                    config[key] = value
                elif key not in ("codex_home", "github_url", "privacy_mode") and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
                    config[key] = value
        if config["critical_mib"] < config["large_mib"]:
            config["critical_mib"] = max(500.0, config["large_mib"])
        config["sampling_interval"] = min(60, max(2, config["sampling_interval"]))
        config["catalog_interval"] = min(600, max(30, config["catalog_interval"]))
        config["page_size"] = int(min(200, max(20, config["page_size"])))
    except (OSError, ValueError, TypeError):
        pass
    return config


class MonitorApp:
    def __init__(self, root, codex_home=None, *, start_worker=True, privacy=False):
        self.root = root
        self.config = load_settings()
        if codex_home:
            self.config["codex_home"] = str(codex_home)
        self.home = self.config["codex_home"]
        self.snapshot = {"sessions": [], "errors": []}
        self.rows = {}
        self.filtered_rows = []
        self.selected_id = None
        self.sort_key, self.sort_reverse = "mtime", True
        self.closed = threading.Event()
        self.wake = threading.Event()
        self.results = queue.Queue(maxsize=1)
        self.last_snapshot_at = None
        self.last_error = None
        self.search_job = None
        self.drain_job = None
        self.page = 0
        self.row_cache = {}
        self.paused = threading.Event()
        self.poll_generation = 0
        self.minimized = False
        self.force_catalog = False
        self.focus_id = None
        self.engine = None
        self.agent_results = queue.Queue()
        self.agent_job_running = False
        self.privacy = tk.BooleanVar(value=privacy or self.config["privacy_mode"])
        self.anonymous_ids = {}
        self.info_dialogs = []
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.report_callback_exception = self.callback_error
        if start_worker:
            self.worker = threading.Thread(target=self._poll_loop, name="CodexMonitorReader", daemon=True)
            self.worker.start()
            self.drain_job = self.root.after(150, self._drain)

    def _build(self):
        r = self.root
        r.title(f"{APP_NAME}  {APP_VERSION}")
        r.configure(bg=BG)
        screen_w, screen_h = r.winfo_screenwidth(), r.winfo_screenheight()
        width, height = min(1420, screen_w - 60), min(880, screen_h - 110)
        r.geometry(f"{width}x{height}+{max(0, (screen_w-width)//2)}+35")
        r.minsize(min(1100, width), min(700, height))
        try:
            icon = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "monitor.ico"
            if icon.exists():
                r.iconbitmap(str(icon))
        except tk.TclError:
            pass
        r.option_add("*Font", ("Microsoft YaHei UI", 10))
        style = ttk.Style(r)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("TButton", background="#26354a", foreground=TEXT, borderwidth=0, padding=(12, 8))
        style.map("TButton", background=[("active", "#344b64")])
        style.configure("TCheckbutton", background=BG, foreground=MUTED)
        style.map("TCheckbutton", background=[("active", BG)], foreground=[("active", TEXT)])
        style.configure("TEntry", fieldbackground=PANEL, foreground=TEXT, insertcolor=TEXT, padding=8)
        style.configure("TCombobox", fieldbackground=PANEL, background=PANEL, foreground=TEXT, padding=6)
        style.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", TEXT)])
        r.option_add("*TCombobox*Listbox.background", PANEL)
        r.option_add("*TCombobox*Listbox.foreground", TEXT)
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        rowheight=36, borderwidth=0, font=("Microsoft YaHei UI", 10))
        style.configure("Treeview.Heading", background="#233145", foreground=MUTED,
                        font=("Microsoft YaHei UI", 9), padding=(8, 10), relief="flat")
        style.map("Treeview", background=[("selected", "#2e5063")], foreground=[("selected", "#ffffff")])
        style.map("Treeview.Heading", background=[("active", "#30445c")])
        style.configure("Vertical.TScrollbar", background="#304158", troughcolor=BG, borderwidth=0)
        outer = ttk.Frame(r, padding=(24, 18, 24, 12))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)
        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="CODEX  /  LOCAL OBSERVATORY", foreground=ACCENT,
                  font=("Segoe UI", 9, "bold")).pack(anchor="w")
        title = ttk.Frame(header)
        title.pack(fill="x", pady=(3, 6))
        ttk.Label(title, text="会话监控  1.0", font=("Microsoft YaHei UI", 25, "bold")).pack(side="left")
        self.live = ttk.Label(title, text="●  正在建立监控基线", foreground=ACCENT)
        self.live.pack(side="right")
        self.subtitle = ttk.Label(header, text="近期优先 · 历史低频巡检 · 本机只读监控", foreground=MUTED)
        self.subtitle.pack(anchor="w")
        cards = ttk.Frame(outer)
        cards.grid(row=1, column=0, sticky="ew", pady=(18, 16))
        self.card_vars = []
        self.card_hints = []
        for i, (label, hint) in enumerate((("已发现会话文件", "全目录统计，与列表筛选分开"), ("全部日志体积", "未归档 + 已归档 · 不等于上下文"),
                                          ("近 60 秒输出", "已记账 output tokens / 秒"), ("达到提醒阈值", "阈值可调整 · 不等同于故障"))):
            cards.columnconfigure(i, weight=1, uniform="cards")
            frame = tk.Frame(cards, bg=PANEL, padx=16, pady=12)
            frame.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 6, 0 if i == 3 else 6))
            tk.Label(frame, text=label, bg=PANEL, fg=MUTED, anchor="w").pack(fill="x")
            var = tk.StringVar(value="—")
            tk.Label(frame, textvariable=var, bg=PANEL, fg=AMBER if i == 3 else TEXT,
                     font=("Segoe UI", 23, "bold"), anchor="w").pack(fill="x", pady=2)
            hint_label = tk.Label(frame, text=hint, bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 9), anchor="w")
            hint_label.pack(fill="x")
            self.card_hints.append(hint_label)
            self.card_vars.append(var)
        toolbar = ttk.Frame(outer)
        toolbar.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(toolbar, text="搜索", foreground=MUTED).pack(side="left", padx=(0, 8))
        self.query = tk.StringVar()
        self.search = ttk.Entry(toolbar, textvariable=self.query, width=20)
        self.search.pack(side="left", padx=(0, 10))
        self.query.trace_add("write", self._search_changed)
        self.filter_var = tk.StringVar(value="最近 24 小时")
        combo = ttk.Combobox(toolbar, textvariable=self.filter_var, state="readonly", width=13,
                             values=("全部会话", "最近 24 小时", "正在增长", "触发提醒"))
        combo.pack(side="left", padx=(0, 8))
        combo.bind("<<ComboboxSelected>>", lambda _: self.reset_page())
        self.show_children, self.show_archived = tk.BooleanVar(value=True), tk.BooleanVar(value=False)
        ttk.Checkbutton(toolbar, text="子会话", variable=self.show_children, command=self.reset_page).pack(side="left", padx=4)
        ttk.Checkbutton(toolbar, text="已归档", variable=self.show_archived, command=self.reset_page).pack(side="left", padx=4)
        ttk.Checkbutton(toolbar, text="隐私模式", variable=self.privacy, command=self.toggle_privacy).pack(side="left", padx=4)
        for label, action in (("设置", self.settings), ("导出", self.export), ("刷新", self.refresh)):
            ttk.Button(toolbar, text=label, command=action).pack(side="right", padx=(8, 0))
        self.pause_button = ttk.Button(toolbar, text="暂停", command=self.toggle_pause)
        self.pause_button.pack(side="right", padx=(8, 0))
        columns = ("title", "size_bytes", "output_tps", "growth_bps", "mtime", "status", "model")
        table_frame = ttk.Frame(outer)
        table_frame.grid(row=3, column=0, sticky="nsew")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse", height=10)
        for key, name, width, anchor in (("title", "会话  /  点击表头排序", 350, "w"), ("size_bytes", "文件体积", 108, "e"),
                                       ("output_tps", "输出 tok/s · 60s", 136, "e"), ("growth_bps", "增长 KiB/s", 104, "e"),
                                       ("mtime", "最近活动", 120, "w"), ("status", "观察状态", 180, "w"), ("model", "模型", 142, "w")):
            self.tree.heading(key, text=name, command=lambda k=key: self.sort(k))
            self.tree.column(key, width=width, minwidth=70, anchor=anchor, stretch=key in ("title", "status"))
        self.tree.tag_configure("warning", foreground=AMBER)
        self.tree.tag_configure("critical", foreground=RED)
        self.tree.tag_configure("active", foreground=ACCENT)
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        pager = ttk.Frame(table_frame)
        pager.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(7, 0))
        self.page_label = ttk.Label(pager, text="正在建立目录…", foreground=MUTED)
        self.page_label.pack(side="left")
        self.next_button = ttk.Button(pager, text="下一页 →", command=lambda: self.change_page(1))
        self.next_button.pack(side="right")
        self.prev_button = ttk.Button(pager, text="← 上一页", command=lambda: self.change_page(-1))
        self.prev_button.pack(side="right", padx=6)
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self.select)
        detail = tk.Frame(outer, bg=PANEL, padx=16, pady=12)
        detail.grid(row=4, column=0, sticky="ew", pady=(12, 10))
        info = tk.Frame(detail, bg=PANEL)
        info.pack(side="left", fill="both", expand=True)
        self.detail_title = tk.Label(info, text="选择会话，查看详情与速率曲线", bg=PANEL, fg=TEXT,
                                     anchor="w", font=("Microsoft YaHei UI", 11, "bold"))
        self.detail_title.pack(fill="x")
        self.detail_text = tk.Label(info, text="正在读取会话索引…", bg=PANEL, fg=MUTED, justify="left", anchor="nw",
                                    font=("Microsoft YaHei UI", 9), height=6)
        self.detail_text.pack(fill="x", pady=(6, 4))
        actions = tk.Frame(info, bg=PANEL)
        actions.pack(fill="x")
        self.copy_button = ttk.Button(actions, text="复制文件路径", command=self.copy_path)
        self.copy_button.pack(side="left")
        self.folder_button = ttk.Button(actions, text="打开所在文件夹", command=self.open_folder)
        self.folder_button.pack(side="left", padx=8)
        ttk.Button(actions, text="诊断与优化", command=self.optimize_selected).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="Agent 接入", command=self.agent_hub).pack(side="left")
        self.chart = tk.Canvas(detail, width=370, height=168, bg=PANEL, highlightthickness=0)
        self.chart.pack(side="right", padx=(14, 0))
        self.chart.bind("<Configure>", lambda _: self.draw_chart())
        bottom = ttk.Frame(outer)
        bottom.grid(row=5, column=0, sticky="ew")
        self.status_label = ttk.Label(bottom, text="准备中…", foreground=MUTED, font=("Microsoft YaHei UI", 9))
        self.status_label.pack(side="left")
        ttk.Button(bottom, text="读数说明", command=self.help).pack(side="right")
        ttk.Button(bottom, text="读取提示", command=self.show_errors).pack(side="right", padx=8)
        r.bind("<Control-f>", lambda _: self.search.focus_set())
        r.bind("<F5>", lambda _: self.refresh())
        self.update_privacy_actions()

    def reset_page(self):
        self.page = 0
        self.render()

    def change_page(self, delta):
        self.page = max(0, self.page + delta)
        self.render()

    def toggle_pause(self):
        self.poll_generation += 1
        if self.paused.is_set():
            self.paused.clear()
            self.pause_button.configure(text="暂停")
            self.refresh()
        else:
            self.paused.set()
            self.pause_button.configure(text="继续")
            self.live.configure(text="●  已暂停 · 保留当前快照", foreground=AMBER)
        self.wake.set()

    def anonymous_title(self, session_id):
        session_id = str(session_id)
        if session_id not in self.anonymous_ids:
            self.anonymous_ids[session_id] = f"会话 {len(self.anonymous_ids) + 1:04d}  ▰▰▰▰▰▰▰▰"
        return self.anonymous_ids[session_id]

    def update_privacy_actions(self):
        state = "disabled" if self.privacy.get() else "normal"
        self.copy_button.configure(state=state)
        self.folder_button.configure(state=state)

    def save_config(self, values):
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        temporary = CONFIG_DIR / "settings.tmp"
        temporary.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(CONFIG_DIR / "settings.json")
        self.config.update(values)

    def toggle_privacy(self):
        # A previously opened readout may contain an unredacted path.
        for dialog in self.info_dialogs:
            if dialog.winfo_exists():
                dialog.destroy()
        self.info_dialogs.clear()
        if self.privacy.get():
            self.query.set("")
            if self.last_error:
                self.status_label.configure(text="读取失败 · 路径与错误详情已隐藏", foreground=RED)
        self.update_privacy_actions()
        self.render()
        try:
            self.save_config({**self.config, "privacy_mode": self.privacy.get()})
        except OSError:
            self.live.configure(text="●  隐私偏好未保存", foreground=AMBER)

    def _poll_loop(self):
        engine = None
        while not self.closed.is_set():
            generation = self.poll_generation
            if self.paused.is_set():
                self.wake.wait(1)
                self.wake.clear()
                continue
            start = time.monotonic()
            try:
                if engine is None:
                    engine = MonitorEngine(self.home, window_seconds=60,
                                           sampling_interval=self.config["sampling_interval"],
                                           catalog_interval=self.config["catalog_interval"])
                    self.engine = engine
                engine.sampling_interval = self.config["sampling_interval"]
                engine.catalog_interval = self.config["catalog_interval"]
                engine.focus_session(self.focus_id)
                if self.force_catalog:
                    self.force_catalog = False
                    engine.request_refresh()
                result = engine.poll()
                result["received_at"] = time.time()
            except Exception as error:
                result = {"fatal_error": f"{type(error).__name__}: {error}", "received_at": time.time()}
            result["generation"] = generation
            try:
                self.results.put_nowait(result)
            except queue.Full:
                try:
                    self.results.get_nowait()
                except queue.Empty:
                    pass
                self.results.put_nowait(result)
            interval = max(30, self.config["sampling_interval"]) if self.minimized else self.config["sampling_interval"]
            self.wake.wait(max(0.2, interval - (time.monotonic() - start)))
            self.wake.clear()

    def _drain(self):
        if self.closed.is_set():
            return
        was_minimized = self.minimized
        self.minimized = self.root.state() == "iconic"
        if was_minimized and not self.minimized:
            self.render()
            self.wake.set()
        self._drain_agent()
        try:
            result = self.results.get_nowait()
            if self.paused.is_set() or result.get("generation", self.poll_generation) != self.poll_generation:
                pass
            elif "fatal_error" in result:
                self.last_error = result["fatal_error"]
                self.live.configure(text="●  读取失败 · 保留上次数据", foreground=RED)
                self.status_label.configure(text="读取失败 · 路径与错误详情已隐藏" if self.privacy.get() else self.last_error[:130], foreground=RED)
            else:
                self.last_error = None
                self.last_snapshot_at = result["received_at"]
                self.snapshot = result
                self.rows = {str(s["id"]): s for s in result["sessions"]}
                if not self.minimized:
                    self.render()
        except queue.Empty:
            if not self.paused.is_set() and not self.minimized and self.last_snapshot_at and time.time() - self.last_snapshot_at > max(15, self.config["sampling_interval"] * 3):
                self.live.configure(text="●  采样延迟 · 数据尚未刷新", foreground=AMBER)
        self.drain_job = self.root.after(400, self._drain)

    def _search_changed(self, *_):
        self.page = 0
        if self.search_job:
            self.root.after_cancel(self.search_job)
        self.search_job = self.root.after(180, self.render)

    def alert(self, s):
        reasons, severity = [], ""
        size = s.get("size_bytes", 0) / 1048576
        if size >= self.config["critical_mib"]:
            reasons.append("文件很大")
            severity = "critical"
        elif size >= self.config["large_mib"]:
            reasons.append("文件偏大")
            severity = "warning"
        if (s.get("growth_bps") or 0) * 60 / 1048576 >= self.config["growth_mib_min"]:
            reasons.append("增长较快")
            severity = severity or "warning"
        if (s.get("output_tps") or 0) >= self.config["high_tps"]:
            reasons.append("输出较高")
            severity = severity or "warning"
        if s.get("warning"):
            reasons.append(str(s["warning"]))
            severity = severity or "warning"
        return " · ".join(reasons), severity

    def render(self):
        if self.search_job:
            self.root.after_cancel(self.search_job)
        self.search_job = None
        all_rows = list(self.rows.values())
        query, mode, now = self.query.get().strip().casefold(), self.filter_var.get(), time.time()
        data = []
        for s in all_rows:
            if not self.show_children.get() and s.get("is_subagent"):
                continue
            if not self.show_archived.get() and s.get("archived"):
                continue
            if query and query not in " ".join(str(s.get(k, "")) for k in ("title", "id", "cwd", "model", "path")).casefold():
                continue
            if mode == "最近 24 小时" and now - s.get("activity_at", s.get("mtime", 0)) > 86400:
                continue
            if mode == "正在增长" and (s.get("growth_bps") or 0) <= 0:
                continue
            if mode == "触发提醒" and not self.alert(s)[0]:
                continue
            data.append(s)
        key = self.sort_key
        numeric = key in ("size_bytes", "output_tps", "growth_bps", "mtime")
        data.sort(key=lambda s: (s.get("activity_at", s.get("mtime", 0)) if key == "mtime" else
                                (s.get(key) if s.get(key) is not None else -1) if numeric
                                else str(s.get(key) or "").casefold()), reverse=self.sort_reverse)
        self.filtered_rows = data
        page_size = int(self.config["page_size"])
        pages = max(1, math.ceil(len(data) / page_size))
        self.page = min(self.page, pages - 1)
        visible = data[self.page * page_size:(self.page + 1) * page_size]
        self.page_label.configure(text=f"第 {self.page + 1} / {pages} 页 · 筛选命中 {len(data):,} 个 · 每页 {page_size} 个")
        self.prev_button.configure(state="normal" if self.page else "disabled")
        self.next_button.configure(state="normal" if self.page < pages - 1 else "disabled")
        keep = {str(s["id"]) for s in visible}
        stale = [iid for iid in self.tree.get_children() if iid not in keep]
        if stale:
            self.tree.delete(*stale)
            for iid in stale:
                self.row_cache.pop(iid, None)
        order = self.tree.get_children()
        for pos, s in enumerate(visible):
            iid = str(s["id"])
            warning, severity = self.alert(s)
            title = (self.anonymous_title(iid) if self.privacy.get() else str(s.get("title") or iid).replace("\n", " "))
            if s.get("is_subagent"):
                title = "↳ " + title
            if s.get("archived"):
                title = "[归档] " + title
            tps = s.get("output_tps")
            values = (title, size_text(s.get("size_bytes")), "—" if tps is None else f"{tps:,.1f}",
                      f"{(s.get('growth_bps') or 0) / 1024:,.1f}", age_text(s.get("activity_at", s.get("mtime"))),
                      ("读取受限" if s.get("warning") else (warning or s.get("status", ""))) if self.privacy.get() else (warning or s.get("status", "")),
                      s.get("model") or "—")
            tag = severity or ("active" if (s.get("growth_bps") or 0) > 0 else "")
            if self.tree.exists(iid):
                if self.row_cache.get(iid) != (values, tag):
                    self.tree.item(iid, values=values, tags=(tag,))
                if pos >= len(order) or order[pos] != iid:
                    self.tree.move(iid, "", pos)
            else:
                self.tree.insert("", pos, iid=iid, values=values, tags=(tag,))
            self.row_cache[iid] = (values, tag)
        self.card_vars[0].set(f"{len(all_rows):,}")
        self.card_vars[1].set(size_text(sum(s.get("size_bytes", 0) for s in all_rows)))
        active_bytes = sum(s.get("size_bytes", 0) for s in all_rows if not s.get("archived"))
        archive_bytes = sum(s.get("size_bytes", 0) for s in all_rows if s.get("archived"))
        self.card_hints[1].configure(text=f"未归档 {size_text(active_bytes)} / 已归档 {size_text(archive_bytes)}")
        known = [s["output_tps"] for s in all_rows if s.get("output_tps") is not None]
        self.card_vars[2].set(f"{sum(known):,.1f} tok/s" if known else "—")
        self.card_vars[3].set(str(sum(bool(self.alert(s)[0]) for s in all_rows)))
        count_errors = len(self.snapshot.get("errors", []))
        self.live.configure(text="●  已暂停 · 保留当前快照" if self.paused.is_set() else "●  本机监控中" if not count_errors else f"●  监控中 · {count_errors} 条读取提示",
                            foreground=ACCENT if not count_errors else AMBER)
        self.subtitle.configure(text=f"近期优先 · 每 {self.config['sampling_interval']:g} 秒采样 · 目录每 {self.config['catalog_interval']:g} 秒巡检 · 最小化后降频")
        stamp = time.strftime("%H:%M:%S", time.localtime(self.last_snapshot_at or time.time()))
        diag = self.snapshot.get("diagnostics", {})
        self.status_label.configure(text=f"本页 {len(visible)} / 全目录 {len(all_rows):,} · Token 基线 {len(known):,} · {stamp} · 采样 {self.snapshot.get('scan_seconds', 0):.2f}s · 读取 {size_text(diag.get('jsonl_read_bytes', 0))}",
                                    foreground=MUTED)
        if self.selected_id not in keep:
            self.selected_id = None
            if visible:
                self.tree.selection_set(str(visible[0]["id"]))
        self.update_detail()

    def sort(self, key):
        self.sort_reverse = not self.sort_reverse if key == self.sort_key else key != "title"
        self.sort_key = key
        self.reset_page()

    def select(self, _=None):
        selection = self.tree.selection()
        self.selected_id = selection[0] if selection else None
        if self.focus_id != self.selected_id:
            self.focus_id = self.selected_id
            self.wake.set()
        self.update_detail()

    def update_detail(self):
        s = self.rows.get(self.selected_id)
        if not s:
            self.detail_title.configure(text="选择会话，查看详情与速率曲线")
            self.detail_text.configure(text="可按名称、ID、项目路径或模型搜索。\n黄色 / 红色为达到阈值；静默本身不能证明卡死。")
            self.draw_chart()
            return
        title = self.anonymous_title(s["id"]) if self.privacy.get() else str(s.get("title") or s["id"]).replace("\n", " ")
        self.detail_title.configure(text=title[:75])
        last = s.get("last_output_tokens")
        detail = [f"体积 {size_text(s.get('size_bytes'))}  ·  本次监控新增输出 {s.get('observed_output_tokens', 0):,} tokens",
                  f"最后 Token 记录：{age_text(s.get('last_token_at'))}  ·  最近记录输出：{'—' if last is None else f'{last:,}'}"]
        if self.privacy.get():
            detail.append("覆盖说明已隐藏；原始会话标识和路径仅保留在本机数据中。")
        else:
            detail = [f"ID  {s['id']}"] + detail + [f"项目：{s.get('cwd') or '未知'}", f"文件：{s.get('path') or '未知'}",
                     str(s.get("coverage") or "近 60 秒已记账输出 / 60；包含推理 Token，日志更新可能滞后。")]
        self.detail_text.configure(text="\n".join(line if len(line) < 110 else line[:107] + "…" for line in detail))
        self.draw_chart()

    def draw_chart(self):
        canvas = self.chart
        canvas.delete("all")
        w, h = max(280, canvas.winfo_width()), max(150, canvas.winfo_height())
        canvas.create_text(8, 10, text="输出速率  /  近 5 分钟", anchor="nw", fill=MUTED, font=("Microsoft YaHei UI", 9))
        s = self.rows.get(self.selected_id, {})
        now = self.last_snapshot_at or time.time()
        points = [(float(t), float(v)) for t, v in s.get("history", []) if v is not None and now - 300 <= float(t) <= now + 1]
        peak = max(1, max((v for _, v in points), default=0))
        left, right, top, bottom = 42, w - 12, 38, h - 24
        for fraction in (0, 0.5, 1):
            y = bottom - fraction * (bottom - top)
            canvas.create_line(left, y, right, y, fill="#2b3b50")
            canvas.create_text(left - 7, y, text=f"{peak * fraction:.0f}", anchor="e", fill=MUTED, font=("Segoe UI", 8))
        canvas.create_text(left, h - 10, text="-5 分钟", anchor="w", fill=MUTED, font=("Microsoft YaHei UI", 8))
        canvas.create_text(right, h - 10, text="现在", anchor="e", fill=MUTED, font=("Microsoft YaHei UI", 8))
        if len(points) > 1:
            coords = []
            for timestamp, value in points:
                coords.extend((left + max(0, min(1, (timestamp - now + 300) / 300)) * (right - left),
                               bottom - max(0, value) / peak * (bottom - top)))
            canvas.create_line(*coords, fill=ACCENT, width=2)
        else:
            canvas.create_text((left + right) / 2, (top + bottom) / 2, text="等待采样数据", fill=MUTED)

    def refresh(self):
        self.force_catalog = True
        self.wake.set()
        self.live.configure(text="●  正在刷新", foreground=ACCENT)

    def copy_path(self):
        if self.privacy.get():
            return
        s = self.rows.get(self.selected_id)
        if s:
            self.root.clipboard_clear()
            self.root.clipboard_append(s["path"])
            self.status_label.configure(text="已复制完整文件路径")

    def open_folder(self):
        if self.privacy.get():
            return
        s = self.rows.get(self.selected_id)
        if s and Path(s["path"]).is_file():
            subprocess.Popen(["explorer.exe", "/select,", str(Path(s["path"]))])

    def agent_command(self, command="capabilities", session_id=None):
        if getattr(sys, "frozen", False):
            base = Path(sys.executable).parent
            executable = base / "codex-monitor-agent.exe"
            if not executable.exists():
                executable = base / "Codex 会话监控 1.0" / "codex-monitor-agent.exe"
            args = [str(executable), command]
        else:
            args = [sys.executable, str(Path(__file__).with_name("agent_api.py")), command]
        args += ["--codex-home", self.home]
        if session_id:
            args += ["--session", session_id]
        if self.privacy.get():
            return 'codex-monitor-agent.exe ' + command + (' --session <会话ID>' if session_id else '') + ' --privacy'
        if command != "capabilities":
            args += ["--private"]
        # List invocation is printed as PowerShell syntax, without executing a shell.
        return "& " + " ".join("'" + arg.replace("'", "''") + "'" for arg in args)

    def agent_hub(self):
        win = tk.Toplevel(self.root)
        self.info_dialogs.append(win)
        win.title("Agent 接入 · 1.0")
        win.geometry("860x650")
        win.configure(bg=BG)
        frame = ttk.Frame(win, padding=24)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="把诊断和经验交给 Agent", font=("Microsoft YaHei UI", 18, "bold")).pack(anchor="w")
        ttk.Label(frame, text="本地 JSON 接口 · 无需 API Key · 不启动网络服务", foreground=ACCENT).pack(anchor="w", pady=10)
        text = ("1. capabilities：读取接口能力与限制。\n"
                "2. list：分页列出会话元信息。\n"
                "3. diagnose：有界检查指定会话，输出图片与日志构成线索。\n"
                "4. plan：生成含验证步骤的优化任务包，交给你选择的 Agent。\n\n"
                "PowerShell 调用示例：\n" + self.agent_command() + "\n\n" +
                self.agent_command("diagnose", self.selected_id or "<会话ID>") + "\n\n"
                "添加 --output report.json 可保存结果；添加 --privacy 可匿名输出。\n"
                "界面隐私开关只影响界面与界面导出；独立 CLI 请显式使用 --privacy。\n\n"
                "经验项目：" + self.config["github_url"] + "\n"
                "集成了近期会话瘦身的诊断与维护步骤。仓库历史文档可能早于本地修复；"
                "本版本不提供 Codex 日志直接覆盖命令。\n\n"
                "执行优化前，Agent 仍需核对目标、备份、停写、分页索引、回滚与原生历史读回。"
                "生成任务包不会自动调用模型、发送消息或改写原会话。")
        body = tk.Text(frame, bg=PANEL, fg=TEXT, wrap="word", relief="flat", padx=14, pady=12, height=18)
        body.pack(fill="both", expand=True)
        body.insert("1.0", text)
        body.configure(state="disabled")
        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="复制接入说明", command=lambda: self.copy_text(text)).pack(side="left")
        ttk.Button(actions, text="打开经验项目 GitHub", command=self.open_github).pack(side="left", padx=8)
        ttk.Button(actions, text="阅读优化流程", command=self.show_optimization_guide).pack(side="left")

    def open_github(self):
        url = self.config["github_url"]
        parsed = urlparse(url)
        if parsed.scheme == "https" and parsed.hostname == "github.com" and not parsed.username and not parsed.password:
            webbrowser.open(url)

    def show_optimization_guide(self):
        path = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "optimization_guide.md"
        try:
            self.text_dialog("会话优化流程", path.read_text(encoding="utf-8"))
        except OSError:
            self.text_dialog("会话优化流程", "请查看交付文件夹中的 optimization_guide.md。")

    def copy_text(self, text):
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status_label.configure(text="已复制到剪贴板")

    def optimize_selected(self):
        row = self.rows.get(self.selected_id)
        if not row:
            messagebox.showinfo(APP_NAME, "请先选择一个会话。", parent=self.root)
            return
        win = tk.Toplevel(self.root)
        self.info_dialogs.append(win)
        win.title("诊断与优化")
        win.geometry("850x650")
        win.configure(bg=BG)
        frame = ttk.Frame(win, padding=22)
        frame.pack(fill="both", expand=True)
        title = self.anonymous_title(row["id"]) if self.privacy.get() else row.get("title", row["id"])
        ttk.Label(frame, text=title[:65], font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w")
        ttk.Label(frame, text="先定位膨胀来源，再生成供 Agent 执行的维护方案。", foreground=MUTED).pack(anchor="w", pady=10)
        body = tk.Text(frame, bg=PANEL, fg=TEXT, wrap="word", relief="flat", padx=16, pady=14)
        body.pack(fill="both", expand=True)
        body.insert("1.0", "点击“开始诊断”检查这个会话。\n\n诊断有读取预算，只输出统计，不复制对话正文。\n生成任务包不会改写日志；维护前需验证停写、备份与分页索引。")
        body.configure(state="disabled")
        holder = {"data": None}
        actions = ttk.Frame(frame)
        actions.pack(fill="x", pady=(12, 0))
        buttons = []
        for label, kind in (("开始诊断", "diagnosis"), ("生成 Agent 任务包", "plan")):
            button = ttk.Button(actions, text=label, command=lambda k=kind: self.start_agent_job(win, body, holder, row["id"], k, buttons))
            button.pack(side="left", padx=(0, 8))
            buttons.append(button)
        ttk.Button(actions, text="复制结果", command=lambda: self.copy_agent_result(holder)).pack(side="left", padx=(0, 8))
        ttk.Button(actions, text="保存 JSON", command=lambda: self.save_agent_result(holder)).pack(side="left")

    def start_agent_job(self, win, body, holder, sid, kind, buttons):
        if self.agent_job_running:
            return
        from agent_api import diagnose_session
        from optimization import build_plan
        self.agent_job_running = True
        privacy = self.privacy.get()
        for button in buttons:
            button.configure(state="disabled")
        body.configure(state="normal")
        body.delete("1.0", "end")
        body.insert("1.0", "正在只读诊断…")
        body.configure(state="disabled")

        def run():
            try:
                function = build_plan if kind == "plan" else diagnose_session
                data, error = function(self.home, sid, privacy=privacy), None
            except Exception as exc:
                data, error = None, f"{type(exc).__name__}: {exc}"
            self.agent_results.put((win, body, holder, buttons, data, error, privacy))
        threading.Thread(target=run, name="CodexOptimizationReader", daemon=True).start()

    def _drain_agent(self):
        try:
            win, body, holder, buttons, data, error, privacy = self.agent_results.get_nowait()
        except queue.Empty:
            return
        self.agent_job_running = False
        if not win.winfo_exists() or privacy != self.privacy.get():
            return
        for button in buttons:
            button.configure(state="normal")
        holder["data"] = data
        text = ("诊断未完成，请检查文件是否仍存在。" if privacy else error) if error else self.format_agent_result(data)
        body.configure(state="normal")
        body.delete("1.0", "end")
        body.insert("1.0", text)
        body.configure(state="disabled")

    @staticmethod
    def format_agent_result(data):
        if data.get("kind") == "plan":
            return data["prompt"]
        cover = data.get("coverage", {})
        images = data.get("image_estimate", {})
        events = data.get("events", {})
        return (f"日志体积：{size_text(data.get('session', {}).get('size_bytes', 0))}\n"
                f"已检查：{size_text(cover.get('read_bytes', 0))} · {'完整覆盖' if cover.get('complete') else '部分覆盖'}\n"
                f"图片线索：{images.get('count', 0)} 处 · Base64 约 {size_text(images.get('base64_bytes', 0))}\n"
                f"符合限定字段与签名条件：{images.get('eligible_image_count', 0)} 张 · {size_text(images.get('eligible_image_bytes', 0))}\n"
                f"完整 JSON 记录：{events.get('valid_json_lines', 0)} · 超长记录：{events.get('oversize_lines', 0)}\n\n"
                "图片统计是已读取范围内的线索，不是确认可释放的空间。未覆盖部分不能据此判断。\n\n" +
                "\n".join(str(x) for x in cover.get("notes", []) + data.get("warnings", [])) +
                "\n\n下一步：生成 Agent 任务包，交给你选择的 Agent 核对完整历史与维护条件。")

    def copy_agent_result(self, holder):
        if holder.get("data"):
            data = holder["data"]
            self.copy_text(data.get("prompt") or json.dumps(data, ensure_ascii=False, indent=2))

    def save_agent_result(self, holder):
        if not holder.get("data"):
            return
        target = filedialog.asksaveasfilename(parent=self.root, title="保存诊断或任务包", defaultextension=".json", initialfile="codex-optimization.json", filetypes=[("JSON", "*.json")])
        if target:
            try:
                resolved = Path(target).resolve()
                home = Path(self.home).resolve()
                if resolved == home or home in resolved.parents:
                    raise OSError("Reports must be outside Codex home")
                with resolved.open("x", encoding="utf-8") as stream:
                    stream.write(json.dumps(holder["data"], ensure_ascii=False, indent=2))
                self.status_label.configure(text="已保存优化报告")
            except OSError:
                messagebox.showerror(APP_NAME, "请使用 Codex 数据目录之外的新文件名，避免覆盖已有文件。", parent=self.root)

    def export(self):
        if not self.filtered_rows:
            messagebox.showinfo(APP_NAME, "当前筛选下没有会话。", parent=self.root)
            return
        path = filedialog.asksaveasfilename(parent=self.root, title="导出当前筛选结果", defaultextension=".csv",
                                          initialfile=time.strftime("Codex会话快照-%Y%m%d-%H%M%S.csv"), filetypes=[("CSV 表格", "*.csv")])
        if not path:
            return
        fields = (("title", "size_bytes", "output_tps", "growth_bps", "observed_output_tokens", "model", "mtime", "warning")
                  if self.privacy.get() else
                  ("id", "title", "size_bytes", "output_tps", "growth_bps", "observed_output_tokens", "model", "mtime", "path", "cwd", "warning"))
        try:
            target = Path(path).resolve()
            home = Path(self.home).resolve()
            if target == home or home in target.parents:
                raise OSError("CSV exports must be outside Codex home")
            with target.open("x", encoding="utf-8-sig", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(("采样时间",) + fields)
                for row in self.filtered_rows:
                    values = [(self.anonymous_title(row["id"]) if k == "title" else "读取受限" if k == "warning" and row.get("warning") else "")
                              if self.privacy.get() and k in ("title", "warning") else row.get(k, "") for k in fields]
                    # Titles are untrusted data; prevent CSV spreadsheet formula execution.
                    values = ["'" + v if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")) else v for v in values]
                    writer.writerow([self.snapshot.get("generated_at", "")] + values)
            self.status_label.configure(text=f"已导出 {len(self.filtered_rows)} 个会话")
        except OSError as error:
            messagebox.showerror(APP_NAME, "请选择 Codex 数据目录之外的新文件名，已有文件不会被覆盖。", parent=self.root)

    def settings(self):
        win = tk.Toplevel(self.root)
        win.title("监控设置")
        win.configure(bg=BG)
        win.geometry("760x660")
        win.minsize(650, 540)
        win.transient(self.root)
        win.grab_set()
        self.info_dialogs.append(win)
        frame = ttk.Frame(win, padding=24)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="性能与偏好", font=("Microsoft YaHei UI", 17, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 14))
        frame.columnconfigure(1, weight=1)
        variables = {}
        for i, (key, label) in enumerate((("sampling_interval", "活跃采样间隔（2–60 秒）"),
                                          ("catalog_interval", "全目录巡检（30–600 秒）"),
                                          ("page_size", "每页会话数（20–200）"),
                                          ("large_mib", "文件偏大（MiB）"), ("critical_mib", "文件很大（MiB）"),
                                          ("growth_mib_min", "文件增长提醒（MiB / 分钟）"), ("high_tps", "输出较高提醒（tok / 秒）"),
                                          ("codex_home", "Codex 数据目录"), ("github_url", "优化经验项目 GitHub")), 1):
            ttk.Label(frame, text=label, foreground=MUTED).grid(row=i, column=0, sticky="w", pady=7, padx=(0, 18))
            hidden_home = key == "codex_home" and self.privacy.get()
            variables[key] = tk.StringVar(value="隐私模式下隐藏" if hidden_home else str(self.config[key]))
            ttk.Entry(frame, textvariable=variables[key], width=32,
                      state="disabled" if hidden_home else "normal").grid(row=i, column=1, sticky="ew", pady=7)
        ttk.Label(frame, text="推荐：采样 5 秒 / 巡检 60 秒 / 每页 100 个。省电可设为 15 / 180 秒。\n间隔保存后立即生效；最小化后至少 30 秒采样。数据目录下次启动生效。\n历史会话按预算轮转检查，尚未建立 Token 基线时显示 —。", foreground=MUTED).grid(row=10, column=0, columnspan=2, sticky="w", pady=(14, 8))

        def save():
            try:
                values = {k: (self.config[k] if k == "codex_home" and self.privacy.get() else v.get().strip()
                              if k in ("codex_home", "github_url") else float(v.get())) for k, v in variables.items()}
                if any(not math.isfinite(v) or v <= 0 for k, v in values.items() if k not in ("codex_home", "github_url")):
                    raise ValueError("阈值须为大于零的有限数字。")
                self.validate_performance_settings(values)
                if values["critical_mib"] < values["large_mib"]:
                    raise ValueError("红色阈值应大于或等于黄色阈值。")
                if not Path(values["codex_home"]).is_dir():
                    raise ValueError("Codex 数据目录不存在。")
                values["privacy_mode"] = self.privacy.get()
                self.save_config(values)
            except (ValueError, OSError) as error:
                messagebox.showerror("无法保存", "设置保存失败。" if self.privacy.get() else str(error), parent=win)
                return
            win.destroy()
            self.wake.set()
            self.reset_page()

        ttk.Button(frame, text="保存并应用", command=save).grid(row=11, column=1, sticky="e", pady=8)

    @staticmethod
    def validate_performance_settings(values):
        for key, minimum, maximum in (("sampling_interval", 2, 60), ("catalog_interval", 30, 600), ("page_size", 20, 200)):
            number = float(values[key])
            if not math.isfinite(number) or not minimum <= number <= maximum:
                raise ValueError(f"{key} 必须在 {minimum}–{maximum} 之间。")
        if float(values["page_size"]) != int(values["page_size"]):
            raise ValueError("每页会话数必须为整数。")
        values["page_size"] = int(values["page_size"])
        url = urlparse(values.get("github_url", DEFAULTS["github_url"]))
        if url.scheme != "https" or url.hostname != "github.com" or url.username or url.password or len(url.path.strip("/").split("/")) < 2:
            raise ValueError("GitHub 链接须为 https://github.com/所有者/仓库。")

    def text_dialog(self, title, text):
        win = tk.Toplevel(self.root)
        self.info_dialogs.append(win)
        win.title(title)
        win.geometry("900x720")
        win.configure(bg=BG)
        scrollbar = ttk.Scrollbar(win, orient="vertical")
        scrollbar.pack(side="right", fill="y", pady=12, padx=(0, 12))
        body = tk.Text(win, bg=PANEL, fg=TEXT, padx=22, pady=18, wrap="word", relief="flat", spacing3=8)
        body.pack(side="left", fill="both", expand=True, padx=(12, 0), pady=12)
        body.configure(yscrollcommand=scrollbar.set)
        scrollbar.configure(command=body.yview)
        body.insert("1.0", text)
        body.configure(state="disabled")

    def show_errors(self):
        errors = self.snapshot.get("errors", [])
        if self.privacy.get():
            self.text_dialog("读取提示", f"全局读取提示：{len(errors)} 条。\n最近一次采样：{'读取失败' if self.last_error else '已完成'}。\n隐私模式已隐藏数据目录及错误原文。")
            return
        message = f"数据源：{self.home}\n\n" + ("\n\n".join(map(str, errors)) if errors else "最近一次采样没有全局读取错误。单会话的覆盖限制见详情及观察状态。")
        if self.last_error:
            message += "\n\n最近错误：" + self.last_error
        self.text_dialog("读取提示", message)

    def help(self):
        self.text_dialog("如何理解这些读数", "文件体积\n统计 Codex sessions 与 archived_sessions 下的 JSONL 文件。图片、工具结果、事件等都可能占据空间，文件大小不等于模型上下文大小。\n\n输出 tok/s · 60s\n最近 60 秒内监测到的 output_tokens 增量除以 60。包含推理 Token；不包含输入或缓存输入。只累计监控启动后观察到的新增输出。初始没有基线时显示 —。\n\n日志不是逐 Token 流\nCodex 常在响应阶段结束后批量记录 token_count，因此读数有延迟，也可能呈阶梯变化。它是日志记账速率，不能当成屏幕逐字流速。工具等待、思考或闲置都可能暂时没有更新；0 本身不能证明卡死。\n\n文件增长\n按文件字节数与采样间隔计算，截断或替换时重新建立基线。大图片会让文件猛增，但不一定增加相同比例的 Token。\n\n提醒与范围\n默认 100 MiB 黄色、500 MiB 红色、增长 20 MiB/分钟或输出 150 tok/s 提醒。可在设置中调整；这些是观察阈值，并非官方异常标准。\n\n本地与只读\n不修改、清理或重启 Codex，不发网络请求。标题来自本机索引。归档与子会话可筛选；不可读、截断和有界读取的覆盖限制会显示出来。图表仅保留本次打开期间的短时样本。")

    def callback_error(self, kind, value, tb):
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            with (CONFIG_DIR / "monitor-errors.log").open("a", encoding="utf-8") as stream:
                stream.write("".join(traceback.format_exception(kind, value, tb)))
        except OSError:
            pass
        self.last_error = f"{kind.__name__}: {value}"
        self.live.configure(text="●  界面错误 · 请查看读取提示", foreground=RED)

    def close(self):
        self.closed.set()
        self.wake.set()
        for job in (self.search_job, self.drain_job):
            if job:
                self.root.after_cancel(job)
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--codex-home", help="指定 Codex 数据目录")
    parser.add_argument("--privacy", action="store_true", help="启动时隐藏会话标题、标识及路径")
    args = parser.parse_args()
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Local.CodexSessionMonitor.1")
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    MonitorApp(root, args.codex_home, privacy=args.privacy)
    root.mainloop()


if __name__ == "__main__":
    main()
