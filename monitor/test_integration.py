"""Exercise data-to-widget integration without touching production logs."""
import json
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from app import MonitorApp
from monitor_core import MonitorEngine


class UIIntegration(unittest.TestCase):
    def setUp(self):
        self.config_folder = tempfile.TemporaryDirectory()
        self.config_patch = patch('app.CONFIG_DIR', Path(self.config_folder.name))
        self.config_patch.start()
        self.theme_patch = patch('app.ttk.Style.theme_use')
        self.theme_patch.start()
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = MonitorApp(self.root, start_worker=False)

    def tearDown(self):
        self.root.update()
        self.app.close()
        self.theme_patch.stop()
        self.config_patch.stop()
        self.config_folder.cleanup()

    def feed(self, rows):
        self.app.rows = {s['id']: s for s in rows}
        self.app.snapshot = {'sessions': rows, 'errors': [], 'scan_seconds': .01}
        self.app.render()
        self.root.update()

    def test_filter_sort_alert_and_selection(self):
        self.app.filter_var.set('全部会话')
        self.app.show_archived.set(True)
        now = time.time()
        first = dict(id='a', title='Test active', path='a.jsonl', size_bytes=200 * 1048576,
                     output_tps=20, growth_bps=512, mtime=now, model='test', cwd='project', history=[(now-2, 15), (now, 20)])
        second = dict(id='b', title='Archived worker', path='b.jsonl', size_bytes=1024,
                      output_tps=None, growth_bps=0, mtime=now-300, model='', archived=True, is_subagent=True)
        self.feed([first, second])
        self.assertEqual(len(self.app.tree.get_children()), 2)
        self.assertEqual(self.app.card_vars[3].get(), '1')
        self.assertEqual(self.app.tree.item('b', 'values')[2], '—')
        self.app.query.set('worker')
        self.app.render()
        self.root.update()
        self.assertEqual(self.app.tree.get_children(), ('b',))
        self.app.query.set('')
        self.app.show_children.set(False)
        self.app.render()
        self.root.update()
        self.assertEqual(self.app.tree.get_children(), ('a',))
        self.app.tree.selection_set('a')
        self.app.select()
        self.assertIn('Test active', self.app.detail_title.cget('text'))
        self.assertIn('200.0 MiB', self.app.detail_text.cget('text'))

    def test_csv_export_escapes_untrusted_title(self):
        self.feed([dict(id='a', title='=SUM(A1)', path='a.jsonl', size_bytes=1, mtime=time.time())])
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'test.csv'
            with patch('app.filedialog.asksaveasfilename', return_value=str(target)):
                self.app.export()
            content = target.read_text(encoding='utf-8-sig')
            self.assertIn("'=SUM(A1)", content)

    def test_privacy_mode_hides_identity_and_exports_anonymously(self):
        now = time.time()
        secret = 'Private Client Project'
        row = dict(id='private-uuid-123', title=secret, path='C:/Users/Secret/private.jsonl',
                   cwd='C:/Users/Secret', size_bytes=1048576, mtime=now, model='test-model',
                   output_tps=3, growth_bps=0, warning='Could not read C:/Users/Secret/private.jsonl')
        self.feed([row])
        self.app.query.set(secret)
        self.app.privacy.set(True)
        self.app.toggle_privacy()
        self.root.update()
        self.assertEqual(self.app.query.get(), '')
        title = self.app.tree.item(row['id'], 'values')[0]
        self.assertTrue(title.startswith('会话 0001'))
        self.assertIn('▰', title)
        self.assertEqual(self.app.detail_title.cget('text'), title)
        visible = ' '.join(str(v) for v in self.app.tree.item(row['id'], 'values'))
        visible += self.app.detail_title.cget('text') + self.app.detail_text.cget('text')
        for private in (secret, row['id'], row['path'], row['cwd']):
            self.assertNotIn(private, visible)
        self.assertEqual(str(self.app.copy_button['state']), 'disabled')
        self.assertEqual(str(self.app.folder_button['state']), 'disabled')
        self.assertEqual(self.app.tree.item(row['id'], 'values')[2], '3.0')
        with patch.object(self.app, 'text_dialog') as dialog:
            self.app.show_errors()
            self.assertNotIn(row['cwd'], dialog.call_args.args[1])
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'privacy.csv'
            with patch('app.filedialog.asksaveasfilename', return_value=str(target)):
                self.app.export()
            csv_text = target.read_text(encoding='utf-8-sig')
            self.assertIn(title, csv_text)
            self.assertIn('3,', csv_text)
            for private in (secret, row['id'], row['path'], row['cwd']):
                self.assertNotIn(private, csv_text)
        self.feed([row])
        self.assertEqual(self.app.tree.item(row['id'], 'values')[0], title)
        self.app.privacy.set(False)
        self.app.toggle_privacy()
        self.root.update()
        self.assertEqual(self.app.tree.item(row['id'], 'values')[0], secret)
        self.assertIn(row['path'], self.app.detail_text.cget('text'))

    def test_csv_export_cannot_overwrite_or_write_inside_codex_home(self):
        self.feed([dict(id='a', title='Test', size_bytes=1, mtime=time.time())])
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            home = base / 'home'
            home.mkdir()
            self.app.home = str(home)
            protected = home / 'source.jsonl'
            protected.write_text('original', encoding='utf-8')
            existing = base / 'existing.csv'
            existing.write_text('existing', encoding='utf-8')
            for target in (protected, home / 'new.csv', existing):
                with patch('app.filedialog.asksaveasfilename', return_value=str(target)), patch('app.messagebox.showerror') as error:
                    self.app.export()
                    error.assert_called_once()
            self.assertEqual(protected.read_text(encoding='utf-8'), 'original')
            self.assertEqual(existing.read_text(encoding='utf-8'), 'existing')
            self.assertFalse((home / 'new.csv').exists())

    def test_privacy_choice_persists_and_cli_can_force_start(self):
        self.app.privacy.set(True)
        self.app.toggle_privacy()
        saved = json.loads((Path(self.config_folder.name) / 'settings.json').read_text(encoding='utf-8'))
        self.assertIs(saved['privacy_mode'], True)
        with patch.object(MonitorApp, '_build'):
            restored = MonitorApp(self.root, start_worker=False)
        self.assertTrue(restored.privacy.get())
        self.app.privacy.set(False)
        self.app.toggle_privacy()
        self.assertFalse(json.loads((Path(self.config_folder.name) / 'settings.json').read_text(encoding='utf-8'))['privacy_mode'])
        with patch.object(MonitorApp, '_build'):
            forced = MonitorApp(self.root, start_worker=False, privacy=True)
        self.assertTrue(forced.privacy.get())
        self.assertFalse(json.loads((Path(self.config_folder.name) / 'settings.json').read_text(encoding='utf-8'))['privacy_mode'])

    def test_real_parser_to_widget(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'sessions' / 'rollout-aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.jsonl'
            path.parent.mkdir()
            event = lambda total: {'type': 'event_msg', 'timestamp': '2026-10-12T00:00:00Z',
                'payload': {'type': 'token_count', 'info': {'total_token_usage': {'output_tokens': total},
                                                          'last_token_usage': {'output_tokens': 60}}}}
            path.write_text(json.dumps(event(100)) + '\n', encoding='utf-8')
            clock = [time.time()]
            engine = MonitorEngine(folder, clock=lambda: clock[0])
            engine.poll()
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(event(160)) + '\n')
            clock[0] += 5
            snapshot = engine.poll()
            self.feed(snapshot['sessions'])
            values = self.app.tree.item('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'values')
            self.assertEqual(values[2], '1.0')
            self.assertEqual(snapshot['sessions'][0]['size_bytes'], path.stat().st_size)

    def test_large_catalog_pagination_and_unchanged_rows(self):
        now = time.time()
        rows = [dict(id=f'session-{i}', title=f'Session {i}', size_bytes=i + 1,
                     mtime=now, output_tps=None, archived=False) for i in range(5000)]
        self.feed(rows)
        self.assertEqual(len(self.app.tree.get_children()), 100)
        self.assertEqual(len(self.app.filtered_rows), 5000)
        self.assertEqual(self.app.card_vars[0].get(), '5,000')
        first = set(self.app.tree.get_children())
        with patch.object(self.app.tree, 'item', wraps=self.app.tree.item) as item:
            self.app.render()
            writes = [c for c in item.call_args_list if 'values' in c.kwargs]
            self.assertEqual(writes, [])
        self.app.change_page(1)
        self.assertFalse(first.intersection(self.app.tree.get_children()))
        self.app.query.set('Session 4999')
        self.app.render()
        self.assertEqual(self.app.page, 0)
        self.assertEqual(self.app.tree.get_children(), ('session-4999',))

    def test_default_recent_view_and_global_volume_breakdown(self):
        now = time.time()
        self.feed([dict(id='old', title='Old', mtime=now-172800, size_bytes=1048576),
                   dict(id='archive', title='Archived', archived=True, mtime=now, size_bytes=2*1048576),
                   dict(id='new', title='New', mtime=now, size_bytes=3*1048576)])
        self.assertEqual(self.app.tree.get_children(), ('new',))
        self.assertEqual(self.app.card_vars[1].get(), '6.0 MiB')
        self.assertIn('未归档 4.0 MiB / 已归档 2.0 MiB', self.app.card_hints[1].cget('text'))

    def test_performance_settings_persist_and_validate(self):
        values = dict(self.app.config, sampling_interval=15, catalog_interval=180, page_size=50)
        self.app.validate_performance_settings(values)
        self.app.save_config(values)
        from app import load_settings
        self.assertEqual(load_settings()['sampling_interval'], 15)
        self.assertEqual(load_settings()['catalog_interval'], 180)
        for changes in ({'sampling_interval': 0}, {'catalog_interval': float('nan')},
                        {'page_size': 50.5}, {'github_url': 'file:///bad'}):
            with self.assertRaises(ValueError):
                self.app.validate_performance_settings(dict(values, **changes))

    def test_pause_and_resume(self):
        self.app.toggle_pause()
        self.assertTrue(self.app.paused.is_set())
        self.assertIn('已暂停', self.app.live.cget('text'))
        self.app.toggle_pause()
        self.assertFalse(self.app.paused.is_set())
        self.assertTrue(self.app.force_catalog)

    def test_pause_discards_inflight_snapshot(self):
        self.app.toggle_pause()
        self.app.results.put({'generation': 0, 'received_at': time.time(), 'sessions': [{'id': 'stale'}]})
        self.app._drain()
        self.assertEqual(self.app.rows, {})
        self.app.toggle_pause()
        self.root.after_cancel(self.app.drain_job)
        self.app.drain_job = None
        self.app.results.put({'generation': 0, 'received_at': time.time(), 'sessions': [{'id': 'stale'}]})
        self.app._drain()
        self.assertEqual(self.app.rows, {})

    def test_agent_entry_uses_privacy_placeholder(self):
        self.app.privacy.set(True)
        command = self.app.agent_command('diagnose', 'secret-id')
        self.assertIn('--privacy', command)
        self.assertNotIn('secret-id', command)
        self.assertNotIn(self.app.home, command)

    def test_agent_diagnosis_to_dialog_and_private_command(self):
        sid = 'bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'sessions' / f'rollout-{sid}.jsonl'
            source.parent.mkdir()
            source.write_text('{"type":"event_msg","payload":{}}\n', encoding='utf-8')
            self.app.home = folder
            self.feed([dict(id=sid, title='Local test', path=str(source), size_bytes=source.stat().st_size, mtime=time.time())])
            self.app.tree.selection_set(sid)
            self.app.select()
            self.assertIn('--private', self.app.agent_command('diagnose', sid))
            self.app.optimize_selected()
            win = self.app.info_dialogs[-1]
            frame = win.winfo_children()[0]
            widgets = frame.winfo_children()
            body = next(w for w in widgets if isinstance(w, tk.Text))
            buttons = widgets[-1].winfo_children()
            buttons[0].invoke()
            deadline = time.monotonic() + 5
            while self.app.agent_job_running and time.monotonic() < deadline:
                self.app._drain_agent()
                time.sleep(.02)
            self.assertFalse(self.app.agent_job_running)
            self.assertIn('完整覆盖', body.get('1.0', 'end'))
            self.assertNotIn('Traceback', body.get('1.0', 'end'))
            buttons[1].invoke()
            deadline = time.monotonic() + 5
            while self.app.agent_job_running and time.monotonic() < deadline:
                self.app._drain_agent()
                time.sleep(.02)
            self.assertFalse(self.app.agent_job_running)
            self.assertIn('备份', body.get('1.0', 'end'))


if __name__ == '__main__':
    unittest.main()
