import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from monitor_core import MonitorEngine


SID = "12345678-1234-1234-1234-1234567890ab"


def token(total, last=None):
    return {"type": "event_msg", "timestamp": "2026-10-12T00:00:00Z",
            "payload": {"type": "token_count",
            "info": {"total_token_usage": {"output_tokens": total,
                                           "reasoning_output_tokens": 3},
                     "last_token_usage": {"output_tokens": total if last is None else last}}}}


def append(path, obj):
    with path.open("ab") as stream:
        stream.write(json.dumps(obj, ensure_ascii=False).encode("utf-8") + b"\n")


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        folder = self.home / "sessions" / "2026" / "10" / "12"
        folder.mkdir(parents=True)
        self.file = folder / f"rollout-2026-10-12T00-00-00-{SID}.jsonl"
        append(self.file, token(100))
        self.now = 1000.0
        self.engine = MonitorEngine(self.home, clock=lambda: self.now,
                                    max_poll_seconds=5)
        self.engine.catalog_interval = 0

    def session(self):
        return self.engine.poll()["sessions"][0]

    def test_baseline_duplicates_reset_and_reasoning_not_double_counted(self):
        row = self.session()
        self.assertEqual(row["observed_output_tokens"], 0)
        append(self.file, token(110, 10))
        append(self.file, token(110, 10))
        self.assertEqual(self.session()["observed_output_tokens"], 10)
        append(self.file, token(5, 5))
        append(self.file, token(8, 3))
        row = self.session()
        self.assertEqual(row["observed_output_tokens"], 18)
        self.assertAlmostEqual(row["output_tps"], 18 / 60)
        self.assertEqual(row["last_output_tokens"], 3)
        self.assertEqual(row["last_token_at"], 1791763200.0)

    def test_old_tail_without_counter_counts_only_latest_response(self):
        with self.file.open("ab") as stream:
            stream.write(json.dumps({"type": "response_item", "payload": {"image": "x" * 70000}}).encode() + b"\n")
        row = self.session()
        self.assertIsNone(row["output_tps"])
        self.assertIsNone(row["history"][-1][1])
        append(self.file, token(10060, 60))
        row = self.session()
        self.assertEqual(row["observed_output_tokens"], 60)
        self.assertEqual(row["output_tps"], 1)
        append(self.file, token(10060, 60))
        self.assertEqual(self.session()["observed_output_tokens"], 60)

    def test_oversize_index_line_recovers_to_next_title(self):
        self.engine.max_line = 512
        index = self.home / "session_index.jsonl"
        index.write_bytes(b"x" * 2000 + b"\n" + json.dumps({"id": SID, "thread_name": "Index title"}).encode() + b"\n")
        result = self.engine.poll()
        self.assertEqual(result["sessions"][0]["title"], "Index title")
        self.assertTrue(any("超长" in text for text in result["errors"]))

    def test_partial_utf8_line_and_sliding_window(self):
        self.session()
        raw = json.dumps(token(120) | {"note": "中文"}, ensure_ascii=False).encode() + b"\n"
        split = raw.index("中".encode()) + 1
        with self.file.open("ab") as stream:
            stream.write(raw[:split])
        self.assertEqual(self.session()["observed_output_tokens"], 0)
        with self.file.open("ab") as stream:
            stream.write(raw[split:])
        self.assertEqual(self.session()["observed_output_tokens"], 20)
        self.now += 61
        row = self.session()
        self.assertEqual(row["observed_output_tokens"], 20)
        self.assertEqual(row["output_tps"], 0)

    def test_preexisting_partial_line_is_not_new_usage(self):
        raw = json.dumps(token(130)).encode() + b"\n"
        with self.file.open("ab") as stream:
            stream.write(raw[:-1])
        self.session()
        with self.file.open("ab") as stream:
            stream.write(b"\n")
        self.assertEqual(self.session()["observed_output_tokens"], 0)
        append(self.file, token(140))
        self.assertEqual(self.session()["observed_output_tokens"], 10)

    def test_oversize_line_skips_and_recovers(self):
        self.engine.max_line = 256
        self.session()
        with self.file.open("ab") as stream:
            stream.write(b"{" + b"x" * 1000)
        self.session()
        with self.file.open("ab") as stream:
            stream.write(b"}\n")
        append(self.file, token(130))
        row = self.session()
        self.assertIn("超大", row["warning"])
        self.assertEqual(row["observed_output_tokens"], 30)

    def test_truncation_rebases_without_negative_tokens(self):
        self.session()
        append(self.file, token(120))
        self.assertEqual(self.session()["observed_output_tokens"], 20)
        self.file.write_bytes(b"")
        row = self.session()
        self.assertIn("重新建立", row["warning"])
        append(self.file, token(7))
        self.assertEqual(self.session()["observed_output_tokens"], 27)
        append(self.file, token(11))
        row = self.session()
        self.assertEqual(row["observed_output_tokens"], 31)
        self.assertAlmostEqual(row["output_tps"], 11 / 60)

    def test_new_file_and_archive_move_discovery(self):
        self.session()
        other = "12345678-1234-1234-1234-1234567890ac"
        new = self.file.parent / f"rollout-2026-10-12T00-00-01-{other}.jsonl"
        append(new, token(200))
        rows = self.engine.poll()["sessions"]
        self.assertEqual(len(rows), 2)
        new_row = next(row for row in rows if row["id"] == other)
        self.assertEqual(new_row["observed_output_tokens"], 200)
        archive = self.home / "archived_sessions"
        archive.mkdir()
        moved = archive / new.name
        new.rename(moved)
        rows = self.engine.poll()["sessions"]
        new_row = next(row for row in rows if row["id"] == other)
        self.assertTrue(new_row["archived"])
        self.assertEqual(Path(new_row["path"]), moved)

    def test_poll_budget_maintains_pending_coverage(self):
        self.session()
        self.engine.max_bytes = 20
        append(self.file, token(120))
        first = self.session()
        self.assertEqual(first["observed_output_tokens"], 0)
        self.assertIn("追赶", first["coverage"])
        for _ in range(20):
            row = self.session()
            if row["observed_output_tokens"] == 20:
                break
        self.assertEqual(row["observed_output_tokens"], 20)

    def test_file_growth_uses_sample_interval_independent_of_read_budget(self):
        self.session()
        self.engine.max_bytes = 0
        self.now += 2
        append(self.file, token(120))
        size_delta = len(json.dumps(token(120), ensure_ascii=False).encode()) + 1
        row = self.session()
        self.assertEqual(row["growth_bps"], size_delta / 2)
        self.assertEqual(row["size_bytes"], self.file.stat().st_size)
        self.assertEqual(row["observed_output_tokens"], 0)
        self.now += 2
        self.assertEqual(self.session()["growth_bps"], 0)

    def test_history_is_poll_samples_and_keeps_five_minutes(self):
        self.session()
        self.now += 2
        append(self.file, token(120, 20))
        row = self.session()
        self.assertEqual(row["history"][-1], (self.now, 20 / 60))
        self.now += 301
        row = self.session()
        self.assertEqual(len(row["history"]), 1)
        self.assertEqual(row["history"][0][1], 0)

    def test_new_file_first_token_and_malformed_json_records(self):
        self.session()
        other = "12345678-1234-1234-1234-1234567890ac"
        new = self.file.parent / f"rollout-2026-10-12T00-00-01-{other}.jsonl"
        append(new, token(6, 6))
        rows = self.engine.poll()["sessions"]
        row = next(row for row in rows if row["id"] == other)
        self.assertEqual(row["observed_output_tokens"], 6)
        with new.open("ab") as stream:
            stream.write(b'[]\n"text"\n{"type":"event_msg","payload":[]}\n')
        append(new, token(8, 2))
        row = next(row for row in self.engine.poll()["sessions"] if row["id"] == other)
        self.assertEqual(row["observed_output_tokens"], 8)

    def test_file_created_before_first_poll_starts_at_zero(self):
        time.sleep(0.02)
        other = "12345678-1234-1234-1234-1234567890ac"
        new = self.file.parent / f"rollout-2026-10-12T00-00-01-{other}.jsonl"
        append(new, token(9, 9))
        row = next(row for row in self.engine.poll()["sessions"] if row["id"] == other)
        self.assertEqual(row["observed_output_tokens"], 9)

    def test_large_backlog_uses_global_budget(self):
        self.session()
        self.now += 2
        with self.file.open("ab") as stream:
            stream.write((b'{"type":"event_msg","payload":{"type":"item_completed"}}\n') * 12000)
        row = self.session()
        self.assertGreater(self.engine.sessions[SID].offset, 64 * 1024)
        self.assertEqual(row["growth_bps"], (self.file.stat().st_size - len(json.dumps(token(100), ensure_ascii=False).encode()) - 1) / 2)

    def test_large_catalog_bounds_cold_work_and_discovers_changes(self):
        folder = self.file.parent
        for n in range(5000):
            sid = f"00000000-0000-0000-0000-{n:012x}"
            path = folder / f"rollout-2026-10-12T00-00-00-{sid}.jsonl"
            append(path, token(100))
        target_id = "00000000-0000-0000-0000-000000000000"
        target = folder / f"rollout-2026-10-12T00-00-00-{target_id}.jsonl"
        os.utime(target, (1, 1))
        engine = MonitorEngine(self.home, clock=lambda: self.now,
                               max_cold_stats_per_poll=64, max_poll_seconds=5)
        first = engine.poll()
        self.assertEqual(first["total_files"], 5001)
        self.assertLessEqual(first["diagnostics"]["jsonl_read_bytes"], 32 * 64 * 1024)
        self.assertIsNone(next(r for r in first["sessions"] if r["id"] == target_id)["output_tps"])
        self.assertEqual(first["total_bytes"], sum(r["size_bytes"] for r in first["sessions"]))
        self.now += 5
        second = engine.poll()
        self.assertFalse(second["diagnostics"]["catalog_scanned"])
        self.assertLessEqual(second["diagnostics"]["stat_count"], 32 + 64)
        self.assertEqual(second["diagnostics"]["jsonl_read_bytes"], 0)
        append(target, token(120, 20))
        found = False
        for _ in range(80):
            self.now += 5
            row = next(r for r in engine.poll()["sessions"] if r["id"] == target_id)
            if row["size_bytes"] == target.stat().st_size:
                found = True
                break
        self.assertTrue(found)
        self.assertGreater(row["growth_bps"], 0)
        engine.focus_session(target_id)
        row = next(r for r in engine.poll()["sessions"] if r["id"] == target_id)
        self.assertIsNotNone(row["output_tps"])
        engine.focus_session(target_id)
        self.assertEqual(engine.poll()["diagnostics"]["jsonl_read_bytes"], 0)

    def test_refresh_removes_missing_and_deduplicates_hardlink(self):
        other = "12345678-1234-1234-1234-1234567890ac"
        alias = self.file.parent / f"rollout-2026-10-12T00-00-01-{other}.jsonl"
        os.link(self.file, alias)
        engine = MonitorEngine(self.home, clock=lambda: self.now)
        self.assertEqual(engine.poll()["total_files"], 1)
        alias.unlink()
        self.file.unlink()
        engine.request_refresh()
        self.assertEqual(engine.poll()["total_files"], 0)

    def test_new_cold_session_does_not_charge_unseen_backlog(self):
        self.engine.poll()
        time.sleep(0.02)
        paths = []
        for n in range(40):
            sid = f"00000000-0000-0000-0000-{n:012x}"
            path = self.file.parent / f"rollout-2026-10-12T00-00-00-{sid}.jsonl"
            append(path, token(100))
            paths.append((sid, path))
        self.engine.request_refresh()
        rows = self.engine.poll()["sessions"]
        cold_id, cold_path = next((sid, path) for sid, path in paths
                                  if next(r for r in rows if r["id"] == sid)["output_tps"] is None)
        append(cold_path, token(120, 20))
        self.engine.focus_session(cold_id)
        row = next(r for r in self.engine.poll()["sessions"] if r["id"] == cold_id)
        self.assertEqual(row["observed_output_tokens"], 0)
        append(cold_path, token(125, 5))
        row = next(r for r in self.engine.poll()["sessions"] if r["id"] == cold_id)
        self.assertEqual(row["observed_output_tokens"], 5)


if __name__ == "__main__":
    unittest.main()
