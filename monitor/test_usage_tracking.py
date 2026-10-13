import json
from pathlib import Path
import tempfile
import unittest

from monitor_core import MonitorEngine
from test_monitor_core import SID, append, token


def usage(inp=1000, out=100, cached=400):
    record = token(out)
    total = record["payload"]["info"]["total_token_usage"]
    if inp is not None:
        total["input_tokens"] = inp
    if cached is not None:
        total["cached_input_tokens"] = cached
    return record


class UsageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.home = root / "codex"
        folder = self.home / "sessions"
        folder.mkdir(parents=True)
        self.path = folder / f"rollout-{SID}.jsonl"
        append(self.path, usage())
        self.store = root / "monitor" / "usage.sqlite3"
        self.now = 1000.0
        self.engines = []
        self.addCleanup(lambda: [engine.close() for engine in self.engines])

    def engine(self, enabled=True, **kwargs):
        engine = MonitorEngine(self.home, clock=lambda: self.now,
                               usage_tracking_enabled=enabled, usage_store_path=self.store,
                               catalog_interval=0, **kwargs)
        self.engines.append(engine)
        return engine

    def row(self, engine):
        return engine.poll()["sessions"][0]

    def test_disabled_has_no_store_io_and_output_window_remains(self):
        engine = self.engine(False)
        row = self.row(engine)
        self.assertFalse(self.store.parent.exists())
        append(self.path, usage(1100, 110, 450))
        row = self.row(engine)
        self.assertEqual(row["output_tokens_60s"], 10)
        for field in ("input_tokens_60s", "cached_input_tokens_60s", "cache_hit_ratio_60s",
                      "total_input_tokens", "total_output_tokens", "total_cached_input_tokens"):
            self.assertIsNone(row[field])
        engine.close()
        self.assertFalse(self.store.parent.exists())

    def test_baseline_deltas_duplicate_window_expiry_and_cache_subset(self):
        engine = self.engine()
        row = self.row(engine)
        self.assertEqual(row["total_input_tokens"], 1000)
        self.assertEqual(row["total_output_tokens"], 100)
        self.assertEqual(row["input_tokens_60s"], 0)
        self.assertIsNone(row["cache_hit_ratio_60s"])
        append(self.path, usage(1200, 120, 500))
        append(self.path, usage(1200, 120, 500))
        row = self.row(engine)
        self.assertEqual(row["input_tokens_60s"], 200)
        self.assertEqual(row["output_tokens_60s"], 20)
        self.assertEqual(row["cached_input_tokens_60s"], 100)
        self.assertEqual(row["cache_hit_ratio_60s"], .5)
        self.assertEqual(row["total_input_tokens"], 1200)
        self.now += 61
        self.assertEqual(self.row(engine)["input_tokens_60s"], 0)

    def test_reopen_catches_monotonic_total_without_historical_window(self):
        engine = self.engine()
        self.row(engine)
        append(self.path, usage(1200, 120, 500))
        self.row(engine)
        engine.close()
        append(self.path, usage(1500, 140, 600))
        reopened = self.engine()
        row = self.row(reopened)
        self.assertEqual(row["total_input_tokens"], 1500)
        self.assertEqual(row["total_output_tokens"], 140)
        self.assertEqual(row["input_tokens_60s"], 0)
        self.assertEqual(row["output_tokens_60s"], 0)

    def test_toggle_catches_disabled_total_without_window(self):
        engine = self.engine()
        self.row(engine)
        engine.set_usage_tracking(False)
        before = self.store.stat().st_mtime_ns
        append(self.path, usage(1600, 150, 650))
        self.row(engine)
        self.assertEqual(self.store.stat().st_mtime_ns, before)
        engine.set_usage_tracking(True)
        row = self.row(engine)
        self.assertEqual(row["total_input_tokens"], 1600)
        self.assertEqual(row["input_tokens_60s"], 0)
        self.assertEqual(row["output_tokens_60s"], 50)

    def test_reset_first_record_rebases_then_adds_only_new_delta(self):
        engine = self.engine()
        self.row(engine)
        append(self.path, usage(20, 5, 10))
        row = self.row(engine)
        self.assertEqual(row["total_input_tokens"], 1000)
        self.assertIn("缺口", row["usage_coverage"])
        append(self.path, usage(30, 7, 15))
        row = self.row(engine)
        self.assertEqual(row["total_input_tokens"], 1010)
        self.assertEqual(row["total_output_tokens"], 102)

    def test_truncate_and_replace_do_not_double_count(self):
        engine = self.engine()
        self.row(engine)
        self.path.write_bytes(b"")
        append(self.path, usage(10, 2, 5))
        self.assertEqual(self.row(engine)["total_input_tokens"], 1000)
        append(self.path, usage(20, 3, 8))
        self.assertEqual(self.row(engine)["total_input_tokens"], 1010)
        replacement = self.path.with_suffix(".replacement")
        append(replacement, usage(2000, 200, 1000))
        replacement.replace(self.path)
        self.assertEqual(self.row(engine)["total_input_tokens"], 1010)
        engine.close()
        self.assertEqual(self.row(self.engine())["total_input_tokens"], 1010)

    def test_missing_fields_remain_unknown(self):
        self.path.write_bytes(b"")
        append(self.path, usage(None, 100, None))
        engine = self.engine()
        row = self.row(engine)
        self.assertIsNone(row["total_input_tokens"])
        self.assertIsNone(row["input_tokens_60s"])
        self.assertIsNone(row["total_cached_input_tokens"])
        append(self.path, usage(None, 120, None))
        row = self.row(engine)
        self.assertIsNone(row["cache_hit_ratio_60s"])
        self.assertEqual(row["total_output_tokens"], 120)

    def test_tail_missing_does_not_full_scan_or_charge_unknown_history(self):
        with self.path.open("ab") as stream:
            stream.write(json.dumps({"filler": "x" * 70000}).encode() + b"\n")
        engine = self.engine()
        row = self.row(engine)
        self.assertIsNone(row["total_input_tokens"])
        self.assertIn("未找到", row["usage_warning"])
        append(self.path, usage(10000, 1000, 4000))
        row = self.row(engine)
        self.assertEqual(row["total_input_tokens"], 10000)
        self.assertEqual(row["input_tokens_60s"], 0)
        self.assertEqual(row["usage_warning"], "")

    def test_budget_and_partial_records(self):
        engine = self.engine()
        self.row(engine)
        engine.max_bytes = 256
        result = engine.poll()
        self.assertLessEqual(result["diagnostics"]["jsonl_read_bytes"], 256)
        for _ in range(8):
            self.row(engine)
        raw = json.dumps(usage(1200, 120, 500)).encode()
        with self.path.open("ab") as stream:
            stream.write(raw)
        for _ in range(8):
            row = self.row(engine)
        self.assertEqual(row["total_input_tokens"], 1000)
        with self.path.open("ab") as stream:
            stream.write(b"\n")
        row = self.row(engine)
        self.assertEqual(row["total_input_tokens"], 1200)

    def test_old_record_replay_does_not_rebase_or_count_twice(self):
        engine = self.engine()
        self.row(engine)
        append(self.path, usage(1200, 120, 500))
        self.row(engine)
        append(self.path, usage())
        append(self.path, usage(1200, 120, 500))
        row = self.row(engine)
        self.assertEqual(row["total_input_tokens"], 1200)
        append(self.path, usage(1300, 130, 550))
        self.assertEqual(self.row(engine)["total_input_tokens"], 1300)

    def test_replayed_tail_restart_preserves_confirmed_high_baseline(self):
        engine = self.engine()
        self.row(engine)
        append(self.path, usage(1200, 120, 500))
        self.row(engine)
        append(self.path, usage())
        engine.close()
        reopened = self.engine()
        row = self.row(reopened)
        self.assertEqual(row["total_input_tokens"], 1200)
        self.assertIn("无法区分", row["usage_warning"])
        self.assertIn("缺口", row["usage_coverage"])
        append(self.path, usage(1200, 120, 500))
        append(self.path, usage(1300, 130, 550))
        row = self.row(reopened)
        self.assertEqual(row["total_input_tokens"], 1300)
        self.assertEqual(row["input_tokens_60s"], 100)

    def test_missing_then_restored_input_marks_persistent_gap(self):
        engine = self.engine()
        self.row(engine)
        append(self.path, usage(None, 120, 500))
        row = self.row(engine)
        self.assertIsNone(row["input_tokens_60s"])
        self.assertIn("缺失", row["usage_warning"])
        append(self.path, usage(1500, 150, 600))
        row = self.row(engine)
        self.assertEqual(row["total_input_tokens"], 1000)
        self.assertIsNone(row["input_tokens_60s"])
        self.assertIn("恢复", row["usage_warning"])
        self.assertIn("缺口", row["usage_coverage"])
        append(self.path, usage(1600, 160, 650))
        self.assertEqual(self.row(engine)["total_input_tokens"], 1100)
        engine.close()
        self.assertIn("缺口", self.row(self.engine())["usage_coverage"])

    def test_missing_or_excess_cache_has_no_invented_ratio(self):
        engine = self.engine()
        self.row(engine)
        append(self.path, usage(1200, 120, None))
        row = self.row(engine)
        self.assertIsNone(row["cached_input_tokens_60s"])
        self.assertIsNone(row["cache_hit_ratio_60s"])
        self.assertIn("缺失", row["usage_warning"])
        append(self.path, usage(1300, 130, 2000))
        row = self.row(engine)
        self.assertIsNone(row["cached_input_tokens_60s"])
        self.assertIsNone(row["cache_hit_ratio_60s"])
        self.assertIn("超过输入", row["usage_warning"])
        self.assertIn("缺口", row["usage_coverage"])

    def test_store_path_rejects_codex_home_and_io_failure_is_warning(self):
        with self.assertRaises(ValueError):
            MonitorEngine(self.home, usage_store_path=self.home / "usage.sqlite3")
        self.store.parent.mkdir()
        self.store.mkdir()
        engine = self.engine()
        row = self.row(engine)
        self.assertEqual(row["total_input_tokens"], 1000)
        self.assertIn("失败", row["usage_warning"])
        self.store.rmdir()
        row = self.row(engine)
        self.assertEqual(row["usage_warning"], "")


if __name__ == "__main__":
    unittest.main()
