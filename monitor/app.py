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
APP_VERSION = "1.1.1"
BG, PANEL, TEXT, MUTED = "#101722", "#192332", "#edf3fc", "#98aac1"
ACCENT, AMBER, RED = "#6ee7c5", "#ffc779", "#ff929f"
CONFIG_DIR = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "CodexSessionMonitor"
DEFAULTS = {"large_mib": 100.0, "critical_mib": 500.0, "growth_mib_min": 20.0,
            "high_tps": 150.0, "codex_home": os.environ.get("CODEX_HOME", str(Path.home() / ".codex")),
            "privacy_mode": False, "sampling_interval": 5.0, "catalog_interval": 60.0,
            "page_size": 100, "github_url": "https://github.com/JingWang-Star996/cancer-session-rescue",
            "usage_tracking_enabled": False, "compact_mode": False, "always_on_top": False,
            "compact_metrics": ["output_tokens_60s"], "show_details": False,
            "selected_session_id": "", "compact_scope": "单会话"}

COMPACT_SCOPES = ("单会话", "近 60 秒有输出 · 未归档", "最近 24 小时 · 未归档", "全部未归档",
                  "仅主会话 · 未归档", "仅子会话 · 未归档", "当前列表筛选", "全目录 · 含归档")

METRICS = {
    "output_tokens_60s": ("60 秒输出", "tokens / 60s", False),
    "output_tps": ("60 秒速率", "tok/s", False),
    "input_tokens_60s": ("60 秒输入", "tokens / 60s", True),
    "cached_input_tokens_60s": ("60 秒缓存输入", "tokens / 60s", True),
    "cache_hit_ratio_60s": ("60 秒缓存命中", "%", True),
    "total_input_tokens": ("累计输入 · 已记录", "tokens", True),
    "total_output_tokens": ("累计输出 · 已记录", "tokens", True),
    "total_cached_input_tokens": ("累计缓存输入 · 已记录", "tokens", True),
    "size_bytes": ("文件体积", "", False),
}


def metric_text(key, value):
    if value is None:
        return "—"
    if key == "size_bytes":
        return size_text(value)
    if key == "cache_hit_ratio_60s":
        return f"{value * 100:.1f}%"
    if key == "output_tps":
        return f"{value:,.1f}"
    return f"{int(value):,}"


def metric_value(row, key):
    value = row.get(key)
    if key == "output_tokens_60s" and value is None:
        rate = metric_value(row, "output_tps")
        value = round(rate * 60) if rate is not None else None
    if type(value) not in (int, float) or value < 0:
        return None
    return value if type(value) is int or math.isfinite(value) else None


def aggregate_metrics(rows, keys):
    """Sum only known values. Ratios use the same valid input/cache pairs."""
    values, coverage = {}, {}
    for key in keys:
        if key == "cache_hit_ratio_60s":
            pairs = [(metric_value(row, "input_tokens_60s"), metric_value(row, "cached_input_tokens_60s")) for row in rows]
            pairs = [(inp, cached) for inp, cached in pairs if inp is not None and cached is not None and cached <= inp]
            denominator = sum(inp for inp, _ in pairs)
            values[key] = sum(cached for _, cached in pairs) / denominator if denominator else None
            coverage[key] = len(pairs)
        else:
            known = [value for row in rows if (value := metric_value(row, key)) is not None]
            values[key] = sum(known) if known or not rows else None
            coverage[key] = len(known)
    return values, coverage


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
                if key in ("codex_home", "github_url", "selected_session_id") and isinstance(value, str) and value:
                    config[key] = value
                elif key == "compact_scope" and value in COMPACT_SCOPES:
                    config[key] = value
                elif isinstance(DEFAULTS[key], bool) and isinstance(value, bool):
                    config[key] = value
                elif key == "compact_metrics" and isinstance(value, list):
                    config[key] = list(dict.fromkeys(k for k in value if isinstance(k, str) and k in METRICS)) or ["output_tokens_60s"]
                elif isinstance(DEFAULTS[key], (int, float)) and not isinstance(DEFAULTS[key], bool) and isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
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
        self.selected_id = self.config["selected_session_id"] or None
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
        self.compact = tk.BooleanVar(value=self.config["compact_mode"])
        self.on_top = tk.BooleanVar(value=self.config["always_on_top"])
        self.show_details = tk.BooleanVar(value=self.config["show_details"])
        self.normal_geometry = None
        self.compact_geometry = "380x230"
        self.layout_job = None
        self.compact_widgets = {}
        self.compact_metric_hints = {}
        self.compact_scope = tk.StringVar(value=self.config["compact_scope"])
        self._compact_choices = []
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
        width, height = min(1280, screen_w - 60), min(800, screen_h - 110)
        r.geometry(f"{width}x{height}+{max(0, (screen_w-width)//2)}+35")
        r.minsize(820, 500)
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
        style.configure("TButton", background="#26354a", foreground=TEXT, borderwidth=0, padding=(9, 5))
        style.map("TButton", background=[("active", "#344b64")])
        style.configure("TCheckbutton", background=BG, foreground=MUTED)
        style.map("TCheckbutton", background=[("active", BG)], foreground=[("active", TEXT)])
        style.configure("TEntry", fieldbackground=PANEL, foreground=TEXT, insertcolor=TEXT, padding=8)
        style.configure("TCombobox", fieldbackground=PANEL, background=PANEL, foreground=TEXT, padding=6)
        style.map("TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", TEXT)])
        r.option_add("*TCombobox*Listbox.background", PANEL)
        r.option_add("*TCombobox*Listbox.foreground", TEXT)
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        rowheight=30, borderwidth=0, font=("Microsoft YaHei UI", 10))
        style.configure("Treeview.Heading", background="#233145", foreground=MUTED,
                        font=("Microsoft YaHei UI", 9), padding=(6, 6), relief="flat")
        style.map("Treeview", background=[("selected", "#2e5063")], foreground=[("selected", "#ffffff")])
        style.map("Treeview.Heading", background=[("active", "#30445c")])
        style.configure("Vertical.TScrollbar", background="#304158", troughcolor=BG, borderwidth=0)
        outer = self.normal_frame = ttk.Frame(r, padding=(14, 10, 14, 8))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(3, weight=1)
        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew")
        title = ttk.Frame(header)
        title.pack(fill="x", pady=(3, 6))
        ttk.Label(title, text="会话监控  1.1", font=("Microsoft YaHei UI", 18, "bold")).pack(side="left")
        ttk.Button(title, text="简化模式", command=self.toggle_compact).pack(side="left", padx=12)
        ttk.Checkbutton(title, text="窗口置顶", variable=self.on_top, command=self.toggle_topmost).pack(side="left")
        self.live = ttk.Label(title, text="●  正在建立监控基线", foreground=ACCENT)
        self.live.pack(side="right")
        self.subtitle = ttk.Label(header, text="近期优先 · 历史低频巡检 · 本机只读监控", foreground=MUTED)
        self.subtitle.pack(anchor="w")
        cards = self.cards = ttk.Frame(outer)
        cards.grid(row=1, column=0, sticky="ew", pady=(8, 8))
        self.card_vars = []
        self.card_hints = []
        for i, (label, hint) in enumerate((("已发现会话文件", "全目录统计，与列表筛选分开"), ("全部日志体积", "未归档 + 已归档 · 不等于上下文"),
                                          ("近 60 秒输出", "已记账 output tokens / 秒"), ("达到提醒阈值", "阈值可调整 · 不等同于故障"))):
            cards.columnconfigure(i, weight=1, uniform="cards")
            frame = tk.Frame(cards, bg=PANEL, padx=10, pady=6)
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
        operations = ttk.Frame(outer)
        operations.grid(row=4, column=0, sticky="ew", pady=(5, 0))
        ttk.Checkbutton(operations, text="展开详情", variable=self.show_details, command=self.toggle_details).pack(side="left")
        for label, action in (("设置", self.settings), ("导出", self.export), ("刷新", self.refresh)):
            ttk.Button(operations, text=label, command=action).pack(side="right", padx=(6, 0))
        self.pause_button = ttk.Button(operations, text="暂停", command=self.toggle_pause)
        self.pause_button.pack(side="right", padx=(8, 0))
        columns = ("title", "size_bytes", "output_tps", "growth_bps", "mtime", "status", "model", *[k for k in METRICS if k not in ("size_bytes", "output_tps")])
        table_frame = self.table_frame = ttk.Frame(outer)
        table_frame.grid(row=3, column=0, sticky="nsew")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse", height=10)
        for key, name, width, anchor in (("title", "会话  /  点击表头排序", 350, "w"), ("size_bytes", "文件体积", 108, "e"),
                                       ("output_tps", "输出 tok/s · 60s", 136, "e"), ("growth_bps", "增长 KiB/s", 104, "e"),
                                       ("mtime", "最近活动", 120, "w"), ("status", "观察状态", 180, "w"), ("model", "模型", 142, "w")):
            self.tree.heading(key, text=name, command=lambda k=key: self.sort(k))
            self.tree.column(key, width=width, minwidth=70, anchor=anchor, stretch=key in ("title", "status"))
        for key in METRICS:
            if key not in ("size_bytes", "output_tps"):
                self.tree.heading(key, text=METRICS[key][0], command=lambda k=key: self.sort(k))
                self.tree.column(key, width=150, minwidth=110, anchor="e", stretch=False)
        self.tree.column("title", width=260, minwidth=160)
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
        detail = self.detail_panel = tk.Frame(outer, bg=PANEL, padx=12, pady=8)
        detail.grid(row=5, column=0, sticky="ew", pady=(6, 6))
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
        bottom.grid(row=6, column=0, sticky="ew")
        self.status_label = ttk.Label(bottom, text="准备中…", foreground=MUTED, font=("Microsoft YaHei UI", 9))
        self.status_label.pack(side="left")
        ttk.Button(bottom, text="读数说明", command=self.help).pack(side="right")
        ttk.Button(bottom, text="读取提示", command=self.show_errors).pack(side="right", padx=8)
        r.bind("<Control-f>", lambda _: self.search.focus_set())
        r.bind("<F5>", lambda _: self.refresh())
        r.bind("<Control-m>", lambda _: self.toggle_compact())
        r.bind("<Control-t>", lambda _: self.toggle_topmost(invert=True))
        r.bind("<Configure>", self.schedule_layout, add=True)
        self._build_compact()
        self.apply_columns()
        r.attributes("-topmost", self.on_top.get())
        if self.compact.get():
            self.set_compact(True, persist=False)
        self.apply_layout()
        self.update_privacy_actions()

    def _build_compact(self):
        self.compact_frame = ttk.Frame(self.root, padding=10)
        controls = ttk.Frame(self.compact_frame)
        controls.pack(fill="x")
        ttk.Button(controls, text="完整界面", command=self.toggle_compact).pack(side="left")
        ttk.Checkbutton(controls, text="置顶", variable=self.on_top, command=self.toggle_topmost).pack(side="left", padx=5)
        ttk.Button(controls, text="设置", command=self.settings).pack(side="right")
        scope_bar = ttk.Frame(self.compact_frame)
        scope_bar.pack(fill="x", pady=(8, 0))
        ttk.Label(scope_bar, text="范围", foreground=MUTED).pack(side="left", padx=(0, 8))
        self.compact_scope_selector = ttk.Combobox(scope_bar, textvariable=self.compact_scope,
                                                   values=COMPACT_SCOPES, state="readonly", width=22)
        self.compact_scope_selector.pack(side="left", fill="x", expand=True)
        self.compact_scope_selector.bind("<<ComboboxSelected>>", self.change_compact_scope)
        self.compact_session = tk.StringVar()
        self.compact_selector = ttk.Combobox(self.compact_frame, textvariable=self.compact_session, state="readonly")
        self.compact_selector.pack(fill="x", pady=(8, 6))
        self.compact_selector.bind("<<ComboboxSelected>>", self.select_compact)
        self.compact_values = ttk.Frame(self.compact_frame)
        self.compact_values.pack(fill="both", expand=True)
        footer = ttk.Frame(self.compact_frame)
        footer.pack(fill="x", pady=(6, 0))
        self.compact_status = ttk.Label(footer, text="等待采样", foreground=MUTED, font=("Microsoft YaHei UI", 9))
        self.compact_status.pack(side="left")
        ttk.Checkbutton(footer, text="隐私", variable=self.privacy, command=self.toggle_privacy).pack(side="right")
        if self.compact_scope.get() != "单会话":
            self.compact_selector.pack_forget()
        self.rebuild_compact_metrics()

    def rebuild_compact_metrics(self):
        for widget in self.compact_values.winfo_children():
            widget.destroy()
        self.compact_widgets = {}
        self.compact_metric_hints = {}
        keys = [k for k in self.config["compact_metrics"] if not METRICS[k][2] or self.config["usage_tracking_enabled"]]
        keys = keys or ["output_tokens_60s"]
        cols = 1 if len(keys) == 1 else 2
        for n in range(2):
            self.compact_values.columnconfigure(n, weight=1 if n < cols else 0, uniform="metric")
        for n, key in enumerate(keys):
            cell = tk.Frame(self.compact_values, bg=PANEL, padx=10, pady=6)
            cell.grid(row=n // cols, column=n % cols, sticky="nsew", padx=2, pady=2)
            tk.Label(cell, text=METRICS[key][0], bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(anchor="w")
            var = tk.StringVar(value="—")
            tk.Label(cell, textvariable=var, bg=PANEL, fg=ACCENT,
                     font=("Segoe UI", 29 if len(keys) == 1 else 19, "bold")).pack(anchor="w")
            hint = tk.Label(cell, text=METRICS[key][1], bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 8), height=2, anchor="nw", justify="left")
            hint.pack(anchor="w")
            self.compact_metric_hints[key] = hint
            self.compact_widgets[key] = var
        self.root.update_idletasks()
        self.compact_min_height = max(200, self.compact_frame.winfo_reqheight() + 16,
                                      125 + math.ceil(len(keys) / cols) * 100)
        self.compact_geometry = f"380x{max(230, self.compact_min_height)}"
        if self.compact.get() and self.compact_frame.winfo_ismapped():
            self.root.minsize(320, self.compact_min_height)

    def set_compact(self, enabled, *, persist=True):
        if enabled:
            self.normal_geometry = self.root.geometry() if self.normal_frame.winfo_ismapped() else self.normal_geometry
            self.normal_frame.pack_forget()
            self.root.minsize(320, self.compact_min_height)
            self.root.geometry(self.compact_geometry)
            self.compact_frame.pack(fill="both", expand=True)
        else:
            self.compact_geometry = self.root.geometry()
            self.compact_frame.pack_forget()
            self.root.minsize(820, 500)
            self.root.geometry(self.normal_geometry or "1160x760")
            self.normal_frame.pack(fill="both", expand=True)
        self.compact.set(enabled)
        if persist:
            self.persist_preferences({"compact_mode": enabled})
        self.apply_layout()
        self.render()

    def toggle_compact(self):
        self.set_compact(not self.compact.get())

    def toggle_topmost(self, *, invert=False):
        if invert:
            self.on_top.set(not self.on_top.get())
        self.root.attributes("-topmost", self.on_top.get())
        self.persist_preferences({"always_on_top": self.on_top.get()})

    def persist_preferences(self, changes):
        try:
            self.save_config(changes)
        except OSError:
            self.live.configure(text="●  偏好未保存", foreground=AMBER)

    def toggle_details(self):
        if self.show_details.get() and self.root.winfo_height() < 680:
            self.show_details.set(False)
            self.text_dialog("所选会话详情", self.detail_title.cget("text") + "\n\n" + self.detail_text.cget("text"))
        self.persist_preferences({"show_details": self.show_details.get()})
        self.apply_layout()

    def schedule_layout(self, event):
        if event.widget is not self.root or self.closed.is_set():
            return
        if self.layout_job:
            self.root.after_cancel(self.layout_job)
        self.layout_job = self.root.after(80, self.apply_layout)

    def apply_layout(self):
        if self.layout_job:
            self.root.after_cancel(self.layout_job)
        self.layout_job = None
        if self.compact.get():
            return
        height = self.root.winfo_height()
        if height < 720:
            self.cards.grid_remove()
        else:
            self.cards.grid()
        if self.show_details.get() and height >= 680:
            self.detail_panel.grid()
        else:
            self.detail_panel.grid_remove()
        # The session list owns the flexible area; summary panels yield first.
        self.normal_frame.rowconfigure(3, weight=1, minsize=200)

    def apply_columns(self):
        if self.config["usage_tracking_enabled"]:
            visible = ("title", "size_bytes", "output_tokens_60s", "input_tokens_60s", "cache_hit_ratio_60s",
                       "total_input_tokens", "total_output_tokens", "total_cached_input_tokens",
                       "cached_input_tokens_60s", "output_tps", "growth_bps", "mtime", "status", "model")
        else:
            visible = ("title", "size_bytes", "output_tps", "growth_bps", "mtime", "status", "model")
        self.tree.configure(displaycolumns=visible)

    def select_compact(self, _=None):
        index = self.compact_selector.current()
        if 0 <= index < len(self._compact_choices):
            self.selected_id = self._compact_choices[index]
            if self.config["selected_session_id"] != self.selected_id:
                self.persist_preferences({"selected_session_id": self.selected_id})
            self.focus_id = self.selected_id
            self.wake.set()
            self.render_compact()

    def change_compact_scope(self, _=None):
        scope = self.compact_scope.get()
        if scope not in COMPACT_SCOPES:
            return
        self.persist_preferences({"compact_scope": scope})
        if scope == "单会话":
            self.compact_selector.pack(before=self.compact_values, fill="x", pady=(8, 6))
        else:
            self.compact_selector.pack_forget()
        width, height = self.root.winfo_width(), self.root.winfo_height()
        self.rebuild_compact_metrics()
        if self.compact.get():
            self.root.geometry(f"{max(320, width)}x{max(height, self.compact_min_height)}")
        self.render_compact()

    def filtered_sessions(self):
        query, mode, now = self.query.get().strip().casefold(), self.filter_var.get(), time.time()
        data = []
        for s in self.rows.values():
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
        return data

    def compact_scope_rows(self):
        scope = self.compact_scope.get()
        if scope == "当前列表筛选":
            # Recompute from this snapshot; hidden table pagination is irrelevant.
            return self.filtered_sessions()
        if scope == "全目录 · 含归档":
            return list(self.rows.values())
        rows = [row for row in self.rows.values() if not row.get("archived")]
        if scope == "最近 24 小时 · 未归档":
            now = time.time()
            rows = [row for row in rows if now - row.get("activity_at", row.get("mtime", 0)) <= 86400]
        elif scope == "近 60 秒有输出 · 未归档":
            rows = [row for row in rows if (metric_value(row, "output_tokens_60s") or 0) > 0]
        elif scope == "仅主会话 · 未归档":
            rows = [row for row in rows if not row.get("is_subagent")]
        elif scope == "仅子会话 · 未归档":
            rows = [row for row in rows if row.get("is_subagent")]
        return rows

    def render_compact_total(self):
        rows = self.compact_scope_rows()
        values, coverage = aggregate_metrics(rows, self.compact_widgets)
        for key, variable in self.compact_widgets.items():
            variable.set(metric_text(key, values[key]))
            note = f"已知 {coverage[key]}/{len(rows)}" + (" · 小计" if coverage[key] < len(rows) else "")
            self.compact_metric_hints[key].configure(text=METRICS[key][1] + "\n" + note)
        stamp = time.strftime("%H:%M:%S", time.localtime(self.last_snapshot_at or time.time()))
        state = "已暂停" if self.paused.is_set() else "合计" if rows else "无匹配"
        self.compact_status.configure(text=f"{state} {len(rows):,} 个 · {stamp}")

    def render_compact(self):
        if self.compact_scope.get() != "单会话":
            self.render_compact_total()
            return
        recent = sorted(self.rows.values(), key=lambda s: s.get("mtime", 0), reverse=True)[:100]
        if self.selected_id is None and recent:
            self.selected_id = str(recent[0]["id"])
        if self.selected_id and self.selected_id not in [str(s["id"]) for s in recent]:
            recent.insert(0, self.rows.get(self.selected_id, {"id": self.selected_id, "title": "所选会话暂不可读"}))
        self._compact_choices = [str(s["id"]) for s in recent]
        names = [self.anonymous_title(s["id"]) if self.privacy.get() else str(s.get("title") or s["id"]).replace("\n", " ")[:60] for s in recent]
        if tuple(self.compact_selector.cget("values")) != tuple(names):
            self.compact_selector.configure(values=names)
        if self.selected_id in self._compact_choices:
            self.compact_selector.current(self._compact_choices.index(self.selected_id))
        row = self.rows.get(self.selected_id, {})
        if self.focus_id != self.selected_id:
            self.focus_id = self.selected_id
            self.wake.set()
        for key, var in self.compact_widgets.items():
            value = row.get(key)
            if key == "output_tokens_60s" and value is None and row.get("output_tps") is not None:
                value = round(row["output_tps"] * 60)
            var.set(metric_text(key, value))
            self.compact_metric_hints[key].configure(text=METRICS[key][1])
        stamp = time.strftime("%H:%M:%S", time.localtime(self.last_snapshot_at or time.time()))
        state = "已暂停" if self.paused.is_set() else "所选会话暂不可读" if not row else "读取提示" if row.get("usage_warning") or row.get("warning") else "监控中"
        self.compact_status.configure(text=f"{state} · {stamp}")

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
        values = {**self.config, **values}
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
                                           catalog_interval=self.config["catalog_interval"],
                                           usage_tracking_enabled=self.config["usage_tracking_enabled"],
                                           usage_store_path=CONFIG_DIR / "usage.sqlite3")
                    self.engine = engine
                engine.sampling_interval = self.config["sampling_interval"]
                engine.catalog_interval = self.config["catalog_interval"]
                engine.set_usage_tracking(self.config["usage_tracking_enabled"])
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
        if engine is not None:
            engine.close()

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
        if self.compact.get():
            self.render_compact()
            return
        if self.search_job:
            self.root.after_cancel(self.search_job)
        self.search_job = None
        all_rows = list(self.rows.values())
        data = self.filtered_sessions()
        key = self.sort_key
        numeric = key in METRICS or key in ("growth_bps", "mtime")
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
            values += tuple(metric_text(k, s.get(k)) for k in METRICS if k not in ("size_bytes", "output_tps"))
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
        count_errors = len(self.snapshot.get("errors", [])) + sum(bool(s.get("usage_warning")) for s in all_rows)
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
        if self.selected_id and self.config["selected_session_id"] != self.selected_id:
            self.persist_preferences({"selected_session_id": self.selected_id})
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
        if self.config["usage_tracking_enabled"]:
            detail += ["60 秒输入 " + metric_text("input_tokens_60s", s.get("input_tokens_60s")) +
                       " · 缓存命中 " + metric_text("cache_hit_ratio_60s", s.get("cache_hit_ratio_60s")),
                       "累计已记录：输入 " + metric_text("total_input_tokens", s.get("total_input_tokens")) +
                       " / 输出 " + metric_text("total_output_tokens", s.get("total_output_tokens")),
                       str(s.get("usage_coverage") or "等待账务基线")]
            if s.get("usage_warning"):
                detail.append("累计记录有覆盖或存储提示" if self.privacy.get() else str(s["usage_warning"]))
        if self.privacy.get():
            detail.append("覆盖说明已隐藏；原始会话标识和路径仅保留在本机数据中。")
        else:
            detail = [f"ID  {s['id']}"] + detail + [f"项目：{s.get('cwd') or '未知'}", f"文件：{s.get('path') or '未知'}",
                     str(s.get("coverage") or "近 60 秒已记账输出 / 60；包含推理 Token，日志更新可能滞后。")]
        self.detail_text.configure(text="\n".join(line if len(line) < 110 else line[:107] + "…" for line in detail),
                                   height=min(8, len(detail)))
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
        if self.config["usage_tracking_enabled"]:
            fields += tuple(k for k in METRICS if k not in ("size_bytes", "output_tps")) + ("usage_coverage",)
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
        win.geometry("720x560")
        win.minsize(660, 520)
        win.transient(self.root)
        win.grab_set()
        self.info_dialogs.append(win)
        shell = ttk.Frame(win, padding=14)
        shell.pack(fill="both", expand=True)
        tabs = ttk.Notebook(shell)
        tabs.pack(fill="both", expand=True)
        pages = []
        for title in ("采样与目录", "Token 与简化模式", "提醒阈值"):
            frame = ttk.Frame(tabs, padding=16)
            frame.columnconfigure(1, weight=1)
            tabs.add(frame, text=title)
            pages.append(frame)
        variables = {}
        row_numbers = [0, 0, 0]
        for i, (key, label) in enumerate((("sampling_interval", "活跃采样间隔（2–60 秒）"),
                                          ("catalog_interval", "全目录巡检（30–600 秒）"),
                                          ("page_size", "每页会话数（20–200）"),
                                          ("large_mib", "文件偏大（MiB）"), ("critical_mib", "文件很大（MiB）"),
                                          ("growth_mib_min", "文件增长提醒（MiB / 分钟）"), ("high_tps", "输出较高提醒（tok / 秒）"),
                                          ("codex_home", "Codex 数据目录"), ("github_url", "优化经验项目 GitHub")), 1):
            page = 2 if key in ("large_mib", "critical_mib", "growth_mib_min", "high_tps") else 0
            frame = pages[page]
            i = row_numbers[page]
            row_numbers[page] += 1
            ttk.Label(frame, text=label, foreground=MUTED).grid(row=i, column=0, sticky="w", pady=7, padx=(0, 12))
            hidden_home = key == "codex_home" and self.privacy.get()
            variables[key] = tk.StringVar(value="隐私模式下隐藏" if hidden_home else str(self.config[key]))
            ttk.Entry(frame, textvariable=variables[key], width=32,
                      state="disabled" if hidden_home else "normal").grid(row=i, column=1, sticky="ew", pady=7)
        ttk.Label(pages[0], text="推荐：5 秒采样 / 60 秒巡检。省电可用 15 / 180 秒。\n最小化后至少 30 秒采样，统计窗口仍为 60 秒。\n数据目录下次启动生效，旧会话低频轮转。", foreground=MUTED).grid(row=6, column=0, columnspan=2, sticky="w", pady=14)
        usage = tk.BooleanVar(value=self.config["usage_tracking_enabled"])
        ttk.Checkbutton(pages[1], text="开启输入、缓存与累计 Token 记录（默认关闭）", variable=usage).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(pages[1], text="缓存输入属于全部输入；命中率 = 缓存输入 ÷ 输入。\n累计记录保存在本机，重启可续记；关闭后保留已有记录、停止写入。\n首次从最新日志累计起步，不能补齐以前的重置；缺失字段显示 —。", foreground=MUTED).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 14))
        ttk.Label(pages[1], text="简化模式保留哪些指标", font=("Microsoft YaHei UI", 11, "bold")).grid(row=2, column=0, columnspan=2, sticky="w", pady=(0, 7))
        metric_vars = {}
        metric_checks = {}
        for index, (key, spec) in enumerate(METRICS.items()):
            var = tk.BooleanVar(value=key in self.config["compact_metrics"])
            metric_vars[key] = var
            check = ttk.Checkbutton(pages[1], text=spec[0], variable=var)
            check.grid(row=3 + index // 2, column=index % 2, sticky="w", pady=4)
            metric_checks[key] = check
        def update_metric_choices(*_):
            for key, check in metric_checks.items():
                check.configure(state="disabled" if METRICS[key][2] and not usage.get() else "normal")
        usage.trace_add("write", update_metric_choices)
        update_metric_choices()
        ttk.Label(pages[1], text="只勾选「60 秒输出」即可成为单指标小窗。Ctrl+M 切换，Ctrl+T 置顶。", foreground=MUTED).grid(row=8, column=0, columnspan=2, sticky="w", pady=10)
        self.settings_controls = {"window": win, "usage": usage, "metrics": metric_vars, "variables": variables, "tabs": tabs}

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
                values["usage_tracking_enabled"] = usage.get()
                values["compact_metrics"] = [key for key, var in metric_vars.items() if var.get() and (usage.get() or not METRICS[key][2])]
                if not values["compact_metrics"]:
                    raise ValueError("简化模式至少保留一个可用指标。")
                self.save_config(values)
            except (ValueError, OSError) as error:
                messagebox.showerror("无法保存", "设置保存失败。" if self.privacy.get() else str(error), parent=win)
                return
            win.destroy()
            self.apply_columns()
            self.rebuild_compact_metrics()
            if self.compact.get():
                rows = math.ceil(len(self.compact_widgets) / (1 if len(self.compact_widgets) == 1 else 2))
                self.root.geometry(f"{max(360, self.root.winfo_width())}x{max(230, 125 + rows * 98)}")
            self.wake.set()
            self.reset_page()

        save_button = ttk.Button(shell, text="保存并应用", command=save)
        save_button.pack(anchor="e", pady=(12, 0))
        self.settings_controls["save"] = save_button

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
        errors = list(self.snapshot.get("errors", []))
        errors.extend(dict.fromkeys(str(row["usage_warning"]) for row in self.rows.values() if row.get("usage_warning")))
        if self.privacy.get():
            self.text_dialog("读取提示", f"全局读取提示：{len(errors)} 条。\n最近一次采样：{'读取失败' if self.last_error else '已完成'}。\n隐私模式已隐藏数据目录及错误原文。")
            return
        message = f"数据源：{self.home}\n\n" + ("\n\n".join(map(str, errors)) if errors else "最近一次采样没有全局读取错误。单会话的覆盖限制见详情及观察状态。")
        if self.last_error:
            message += "\n\n最近错误：" + self.last_error
        self.text_dialog("读取提示", message)

    def help(self):
        self.text_dialog("如何理解这些读数", "60 秒计数与速率\n60 秒输出是最近 60 秒观察到的 output_tokens 增量；tok/s 是该值除以 60。推理 Token 已包含在输出中，不再重复加算。日志常在响应结束时批量记账，不代表逐字流速。0 也不能证明卡死。\n\n输入与缓存（设置中开启）\n缓存输入是输入的子集。60 秒缓存命中率 = 缓存输入 / 全部输入；没有输入或字段缺失时显示 —。采样间隔与 60 秒统计窗口分别设置，窗口固定 60 秒。\n\n累计已记录\n首次以可读到的最新日志累计值初始化，之后保存确认到的增量，重启可续记。历史重置无法由尾部采样补齐；截断、替换或计数下降后重新建立基线并提示缺口。这是有覆盖范围的本地账务记录，不是账单或上下文长度。关闭开关后停止记录，已有记录保留。\n\n简化与置顶\n小窗「范围」支持单会话与七种汇总。当前列表筛选按完整界面的本次搜索和筛选统计所有匹配项，不受分页限制。缓存率按有效输入/缓存配对加权；部分未知显示小计与覆盖数。跨会话累计未去重共享历史，不代表账户账单。\n在完整界面选中会话，再点简化模式；设置可只保留 60 秒输出，也可选择输入、缓存、累计或体积。小窗保持所选会话，不随新活动跳转。Ctrl+M 切换模式，Ctrl+T 切换置顶。隐私开关对两个模式都有效。\n\n小窗与文件\n小窗口优先显示会话列表，概览卡片会自动收起；详情可按需展开。文件体积统计 sessions 与 archived_sessions 下 JSONL 的字节数，不等于模型上下文。监控不改写 Codex 日志或索引，统计库只存放在监控器配置目录。")

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
        for job in (self.search_job, self.drain_job, self.layout_job):
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
