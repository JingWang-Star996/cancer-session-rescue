"""Bounded synthetic benchmark; optional real directory metadata scan only."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
import time

from monitor_core import MonitorEngine


def measure(call):
    wall = time.perf_counter()
    cpu = time.process_time()
    value = call()
    return value, {"wall_seconds": round(time.perf_counter() - wall, 4),
                   "process_cpu_seconds": round(time.process_time() - cpu, 4)}


def synthetic():
    with tempfile.TemporaryDirectory() as temp:
        home = Path(temp)
        folder = home / "sessions" / "2026" / "10" / "12"
        folder.mkdir(parents=True)
        line = b'{"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"output_tokens":100}}}}\n'
        for n in range(5000):
            sid = f"00000000-0000-0000-0000-{n:012x}"
            (folder / f"rollout-2026-10-12T00-00-00-{sid}.jsonl").write_bytes(line)
        now = [1000.0]
        engine = MonitorEngine(home, clock=lambda: now[0], max_poll_seconds=5)
        first, first_time = measure(engine.poll)
        now[0] += 5
        second, second_time = measure(engine.poll)
        return {"files": first["total_files"], "startup": first_time,
                "startup_diagnostics": first["diagnostics"],
                "steady": second_time, "steady_diagnostics": second["diagnostics"]}


def real_stat(home: Path):
    def scan():
        count = 0
        size = 0
        for name in ("sessions", "archived_sessions"):
            folder = home / name
            if not folder.is_dir():
                continue
            for root, _, names in os.walk(folder):
                for filename in names:
                    if filename.endswith(".jsonl"):
                        try:
                            size += (Path(root) / filename).stat().st_size
                            count += 1
                        except OSError:
                            pass
        return {"files": count, "bytes": size}
    result, timing = measure(scan)
    return {**result, **timing, "method": "directory enumeration and stat only"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--real-stat-home", type=Path)
    args = parser.parse_args()
    report = {"synthetic_5000": synthetic()}
    if args.real_stat_home:
        report["real_stat_only"] = real_stat(args.real_stat_home)
    print(json.dumps(report, ensure_ascii=False, indent=2))
