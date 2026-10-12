import contextlib
import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from agent_api import DEFAULT_BUDGET, diagnose_session, list_sessions, run_cli
from optimization import build_plan


SID = "12345678-1234-1234-1234-1234567890ab"


class AgentApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        folder = self.home / "sessions" / "2026" / "10" / "12"
        folder.mkdir(parents=True)
        self.source = folder / f"rollout-2026-10-12T00-00-00-{SID}.jsonl"

    def write_events(self, *events):
        self.source.write_bytes(b"".join(json.dumps(row, ensure_ascii=False).encode("utf-8") + b"\n"
                                         for row in events))

    def test_diagnosis_plan_read_only_json_and_privacy(self):
        self.write_events({"type": "session_meta", "payload": {"secret": "秘密标题"}},
                          {"type": "response_item", "payload": {"output": [
                              {"type": "input_image", "image_url": "data:image/png;base64,YWJj"}]}},
                          {"type": "event_msg", "payload": {"type": "task_complete"}})
        before = hashlib.sha256(self.source.read_bytes()).hexdigest()
        private = diagnose_session(self.home, SID)
        self.assertEqual(private["image_estimate"]["count"], 1)
        self.assertEqual(private["events"]["valid_json_lines"], 3)
        self.assertTrue(private["coverage"]["complete"])
        anonymous = diagnose_session(self.home, SID, privacy=True)
        output = json.dumps(build_plan(self.home, SID, privacy=True), ensure_ascii=False)
        self.assertNotIn(SID, output)
        self.assertNotIn(str(self.home), output)
        self.assertNotIn("秘密标题", output)
        self.assertNotIn("秘密标题", json.dumps(anonymous, ensure_ascii=False))
        self.assertIn("history_base", output)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), before)

    def test_invalid_id_and_symlink_escape(self):
        self.write_events({"type": "session_meta"})
        with self.assertRaises(ValueError):
            diagnose_session(self.home, "../../outside")
        external = self.home.parent / f"external-{SID}.jsonl"
        # A source symlink resolving outside home must fail even if catalogued.
        external.write_bytes(self.source.read_bytes())
        self.addCleanup(lambda: external.unlink(missing_ok=True))
        escaped = SimpleNamespace(id=SID, path=external,
                                  meta={"archived": False, "title": ""})
        with patch("agent_api._catalog", return_value=([escaped], [])):
            with self.assertRaises(ValueError):
                diagnose_session(self.home, SID)

    def test_linked_sessions_root_outside_home_when_available(self):
        other = tempfile.TemporaryDirectory()
        self.addCleanup(other.cleanup)
        home = Path(other.name) / "codex-home"
        home.mkdir()
        try:
            (home / "sessions").symlink_to(self.source.parent.parent.parent.parent,
                                             target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory symlink unavailable")
        self.write_events({"type": "session_meta"})
        with self.assertRaises(ValueError):
            diagnose_session(home, SID)

    def test_budget_oversize_utf8_and_partial(self):
        self.source.write_bytes(b'{"type":"response_item","x":"' + b"x" * (2 * 1024 * 1024 + 1)
                                + b'"}\n' + b'\xff\n' + b'{"type":"event_msg"}\n'
                                + b" " * DEFAULT_BUDGET)
        result = diagnose_session(self.home, SID, privacy=True)
        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertEqual(result["events"]["oversize_lines"], 1)
        self.assertEqual(result["events"]["invalid_json_lines"], 1)
        self.assertEqual(result["events"]["valid_json_lines"], 1)
        self.assertLessEqual(result["coverage"]["read_bytes"], DEFAULT_BUDGET)
        self.assertIn("部分覆盖", result["warnings"][0])

    def test_eligible_images_exclude_other_fields_duplicates_and_oversize(self):
        png = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nDATA").decode()
        jpeg = "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8\xffDATA").decode()
        good = {"type": "response_item", "payload": {"output": [
            {"type": "input_image", "image_url": png},
            {"type": "input_image", "image_url": jpeg},
            {"type": "text", "image_url": png},
            {"type": "input_image", "image_url": "data:image/png;base64,YWJj"}]},
            "other": png}
        lines = [json.dumps(good).encode(),
                 b'{"type":"response_item","type":"response_item","payload":{"output":[]}}',
                 json.dumps({"type": "private-secret", "payload": {"text": png}}).encode(),
                 b'{"type":"response_item","image_url":"' + png.encode() + b'",' +
                 b'"other":"' + b"x" * (2 * 1024 * 1024) + b'"}']
        self.source.write_bytes(b"\n".join(lines) + b"\n")
        result = diagnose_session(self.home, SID, privacy=True)
        self.assertEqual(result["image_estimate"]["eligible_image_count"], 2)
        self.assertEqual(result["image_estimate"]["eligible_image_bytes"], 19)
        self.assertGreater(result["image_estimate"]["count"], 2)
        self.assertEqual(result["events"]["duplicate_key_lines"], 1)
        self.assertEqual(result["events"]["oversize_lines"], 1)
        self.assertEqual(result["events"]["types"]["unknown"], 1)
        self.assertNotIn("private-secret", json.dumps(result))

    def test_output_never_overwrites_source_or_existing_report(self):
        self.write_events({"type": "session_meta"})
        original = self.source.read_bytes()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run_cli(["diagnose", "--codex-home", str(self.home),
                                      "--session", SID, "--private", "--output", str(self.source)]), 2)
        self.assertEqual(self.source.read_bytes(), original)
        report = self.home.parent / ("report-" + self.home.name + "-" + SID + ".json")
        report.write_text("existing", encoding="utf-8")
        self.addCleanup(lambda: report.unlink(missing_ok=True))
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(run_cli(["plan", "--codex-home", str(self.home),
                                      "--session", SID, "--private", "--output", str(report)]), 2)
        self.assertEqual(report.read_text(encoding="utf-8"), "existing")

    def test_source_growth_marks_partial(self):
        self.write_events({"type": "session_meta"})
        from agent_api import _image_scan
        changed = False

        def grow(data, state):
            nonlocal changed
            _image_scan(data, state)
            if not changed:
                changed = True
                with self.source.open("ab") as stream:
                    stream.write(b'{"type":"event_msg"}\n')

        with patch("agent_api._image_scan", side_effect=grow):
            result = diagnose_session(self.home, SID, privacy=True)
        self.assertEqual(result["coverage"]["status"], "partial")
        self.assertTrue(any("源文件发生变化" in x for x in result["warnings"]))

    def test_cli_and_catalog_pagination(self):
        self.write_events({"type": "session_meta"})
        rows = list_sessions(self.home, privacy=True, page=1, limit=100)
        self.assertEqual(rows["total"], 1)
        self.assertEqual(rows["sessions"][0]["alias"], "session-1")
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(run_cli(["plan", "--codex-home", str(self.home),
                                      "--session", SID, "--privacy"]), 0)
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(json.loads(stdout.getvalue())["kind"], "plan")
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            self.assertEqual(run_cli(["list", "--codex-home", str(self.home)]), 2)


if __name__ == "__main__":
    unittest.main()
