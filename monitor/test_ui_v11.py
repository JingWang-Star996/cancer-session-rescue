"""Behavior and actual widget geometry regression tests; no screen capture."""
import json
from pathlib import Path
import tempfile
import time
import tkinter as tk
import unittest
from unittest.mock import patch

from app import DEFAULTS, MonitorApp, aggregate_metrics, load_settings


class MonitorV11UI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = Path(self.temp.name)
        self.patcher = patch('app.CONFIG_DIR', self.config)
        self.patcher.start()
        self.root = tk.Tk()
        self.root.geometry('+0+0')
        self.app = MonitorApp(self.root, codex_home=self.config, start_worker=False)
        self.root.update()
        now = time.time()
        self.row = dict(id='test-session', title='Private title', path='private-path', cwd='private-project',
                        size_bytes=1048576, mtime=now, output_tps=10, output_tokens_60s=600,
                        input_tokens_60s=10000, cached_input_tokens_60s=8000, cache_hit_ratio_60s=.8,
                        total_input_tokens=100000, total_output_tokens=5000, total_cached_input_tokens=80000)
        self.app.rows = {self.row['id']: self.row}
        self.app.render()
        self.root.update()

    def tearDown(self):
        self.app.close()
        self.patcher.stop()
        self.temp.cleanup()

    def test_tracking_default_off_and_settings_save_preserves_preferences(self):
        self.assertFalse(self.app.config['usage_tracking_enabled'])
        self.app.privacy.set(True)
        self.app.toggle_privacy()
        self.app.settings()
        controls = self.app.settings_controls
        controls['usage'].set(True)
        controls['metrics']['input_tokens_60s'].set(True)
        controls['metrics']['cache_hit_ratio_60s'].set(True)
        controls['save'].invoke()
        self.root.update()
        saved = json.loads((self.config/'settings.json').read_text(encoding='utf-8'))
        self.assertTrue(saved['privacy_mode'])
        self.assertTrue(saved['usage_tracking_enabled'])
        self.assertIn('input_tokens_60s', self.app.tree.cget('displaycolumns'))
        self.assertIn('cache_hit_ratio_60s', self.app.compact_widgets)

    def test_compact_one_metric_keeps_target_and_topmost_and_privacy(self):
        self.app.selected_id = self.row['id']
        self.app.toggle_compact()
        self.root.update()
        self.assertEqual(set(self.app.compact_widgets), {'output_tokens_60s'})
        self.assertEqual(self.app.compact_widgets['output_tokens_60s'].get(), '600')
        self.assertFalse(self.app.normal_frame.winfo_ismapped())
        self.app.rows['new'] = dict(self.row, id='new', title='New active', mtime=time.time()+10)
        self.app.render()
        self.assertEqual(self.app.selected_id, self.row['id'])
        self.app.toggle_topmost(invert=True)
        self.root.update()
        self.assertTrue(self.root.attributes('-topmost'))
        self.app.privacy.set(True)
        self.app.toggle_privacy()
        self.assertNotIn('Private title', str(self.app.compact_selector.cget('values')))
        self.assertIn('会话', self.app.compact_session.get())
        self.app.toggle_compact()
        self.root.update()
        self.assertTrue(self.app.normal_frame.winfo_ismapped())
        self.assertTrue(self.root.attributes('-topmost'))

    def test_compact_missing_session_is_not_silently_replaced(self):
        self.app.selected_id = self.row['id']
        self.app.toggle_compact()
        self.app.rows = {'other':dict(self.row,id='other')}
        self.app.render()
        self.assertEqual(self.app.selected_id, self.row['id'])
        self.assertEqual(self.app.compact_widgets['output_tokens_60s'].get(), '—')
        self.assertIn('暂不可读', self.app.compact_status.cget('text'))

    def test_compact_target_survives_app_restart(self):
        self.app.tree.selection_set(self.row['id'])
        self.app.select()
        self.app.toggle_compact()
        self.app.close()
        self.root=tk.Tk()
        self.app=MonitorApp(self.root,start_worker=False)
        self.root.update()
        self.assertTrue(self.app.compact.get())
        self.assertEqual(self.app.selected_id,self.row['id'])
        self.app.rows={'new':dict(self.row,id='new',mtime=time.time()+10)}
        self.app.render()
        self.assertEqual(self.app.selected_id,self.row['id'])
        self.assertEqual(self.app.compact_widgets['output_tokens_60s'].get(),'—')

    def test_small_windows_keep_at_least_eight_list_rows_at_three_dpi_scales(self):
        self.app.rows = {str(i):dict(self.row,id=str(i)) for i in range(100)}
        self.app.render()
        for scaling in (1.3333, 1.6667, 2.0):
            self.root.tk.call('tk','scaling',scaling)
            self.root.geometry('960x600+0+0')
            self.root.update()
            self.app.apply_layout()
            self.root.update()
            self.assertFalse(self.app.cards.winfo_ismapped())
            self.assertFalse(self.app.detail_panel.winfo_ismapped())
            height = self.app.tree.winfo_height()
            self.assertGreaterEqual(height, 270, (scaling, height))
            shown = sum(bool(self.app.tree.bbox(iid)) for iid in self.app.tree.get_children())
            self.assertGreaterEqual(shown, 8, (scaling, height, shown))

    def test_disabled_metric_selection_and_settings_validation(self):
        self.app.settings()
        controls = self.app.settings_controls
        controls['metrics']['output_tokens_60s'].set(False)
        with patch('app.messagebox.showerror') as error:
            controls['save'].invoke()
            error.assert_called_once()
        self.assertTrue(controls['window'].winfo_exists())
        controls['metrics']['output_tokens_60s'].set(True)
        controls['save'].invoke()
        self.assertFalse(self.app.config['usage_tracking_enabled'])

    def test_all_compact_metrics_fit_and_unknowns_are_not_zero(self):
        from app import METRICS
        self.app.config.update(usage_tracking_enabled=True, compact_metrics=list(METRICS))
        self.app.rebuild_compact_metrics()
        self.app.toggle_compact()
        self.root.update()
        self.assertEqual(self.app.compact_widgets['cache_hit_ratio_60s'].get(), '80.0%')
        bottom=self.app.compact_values.winfo_rooty()+self.app.compact_values.winfo_height()
        for widget in self.app.compact_values.winfo_children():
            self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(), bottom)
        self.app.rows[self.row['id']]['total_input_tokens']=None
        self.app.render()
        self.assertEqual(self.app.compact_widgets['total_input_tokens'].get(),'—')

    def test_load_rejects_wrong_types_and_unknown_metric(self):
        (self.config/'settings.json').write_text(json.dumps({'usage_tracking_enabled':1,
            'compact_metrics':['unknown','output_tokens_60s','output_tokens_60s'], 'page_size':False}),encoding='utf-8')
        loaded=load_settings()
        self.assertFalse(loaded['usage_tracking_enabled'])
        self.assertEqual(loaded['compact_metrics'],['output_tokens_60s'])
        self.assertEqual(loaded['page_size'],DEFAULTS['page_size'])

    def choose_scope(self, scope):
        if not self.app.compact.get():
            self.app.toggle_compact()
        self.app.compact_scope.set(scope)
        self.app.change_compact_scope()
        self.root.update()

    def test_compact_scope_filters_and_preserves_single_target(self):
        now=time.time()
        self.app.rows={
            'main':dict(self.row,id='main',mtime=now,output_tokens_60s=100),
            'child':dict(self.row,id='child',mtime=now,is_subagent=True,output_tokens_60s=200),
            'old':dict(self.row,id='old',mtime=now-90000,output_tokens_60s=0),
            'archive':dict(self.row,id='archive',mtime=now,archived=True,output_tokens_60s=400)}
        self.app.selected_id='main'
        for scope,total,count in [('全部未归档',300,3),('仅主会话 · 未归档',100,2),
            ('仅子会话 · 未归档',200,1),('全目录 · 含归档',700,4),
            ('最近 24 小时 · 未归档',300,2),('近 60 秒有输出 · 未归档',300,2)]:
            self.choose_scope(scope)
            self.assertEqual(self.app.compact_widgets['output_tokens_60s'].get(),str(total))
            self.assertIn(f'{count} 个',self.app.compact_status.cget('text'))
            self.assertFalse(self.app.compact_selector.winfo_ismapped())
            self.assertEqual(self.app.selected_id,'main')
        self.choose_scope('单会话')
        self.assertTrue(self.app.compact_selector.winfo_ismapped())
        self.assertEqual(self.app.compact_widgets['output_tokens_60s'].get(),'100')

    def test_current_filter_uses_new_snapshot_and_all_pages(self):
        self.app.rows={str(i):dict(self.row,id=str(i),title='match',output_tokens_60s=1) for i in range(230)}
        self.app.query.set('match')
        self.app.render();self.root.update()
        self.assertEqual(len(self.app.tree.get_children()),100)
        self.choose_scope('当前列表筛选')
        self.assertEqual(self.app.compact_widgets['output_tokens_60s'].get(),'230')
        self.app.rows['fresh']=dict(self.row,id='fresh',title='match',output_tokens_60s=3)
        self.app.rows['other']=dict(self.row,id='other',title='exclude',output_tokens_60s=999)
        self.app.render()
        self.assertEqual(self.app.compact_widgets['output_tokens_60s'].get(),'233')

    def test_aggregate_coverage_weighted_cache_and_empty_unknown(self):
        rows=[dict(input_tokens_60s=100,cached_input_tokens_60s=90,output_tokens_60s=10),
              dict(input_tokens_60s=900,cached_input_tokens_60s=90,output_tokens_60s=None),
              dict(input_tokens_60s=None,cached_input_tokens_60s=None,output_tokens_60s=20)]
        values,coverage=aggregate_metrics(rows,('input_tokens_60s','output_tokens_60s','cache_hit_ratio_60s'))
        self.assertEqual(values['input_tokens_60s'],1000)
        self.assertEqual(values['output_tokens_60s'],30)
        self.assertEqual(values['cache_hit_ratio_60s'],.18)
        self.assertEqual(coverage['cache_hit_ratio_60s'],2)
        self.assertIsNone(aggregate_metrics([{}],['output_tokens_60s'])[0]['output_tokens_60s'])
        self.assertEqual(aggregate_metrics([],['output_tokens_60s'])[0]['output_tokens_60s'],0)
        self.assertIsNone(aggregate_metrics([dict(input_tokens_60s=10,cached_input_tokens_60s=20)],['cache_hit_ratio_60s'])[0]['cache_hit_ratio_60s'])
        self.app.rows={'a':dict(self.row,output_tokens_60s=10),'b':dict(self.row,id='b',output_tps=None,output_tokens_60s=None)}
        self.choose_scope('全部未归档')
        self.assertIn('1/2',self.app.compact_metric_hints['output_tokens_60s'].cget('text'))
        self.assertIn('小计',self.app.compact_metric_hints['output_tokens_60s'].cget('text'))

    def test_aggregate_scope_persists_and_no_identity_in_summary(self):
        self.choose_scope('全目录 · 含归档')
        self.assertEqual(load_settings()['compact_scope'],'全目录 · 含归档')
        self.app.privacy.set(True);self.app.toggle_privacy()
        self.assertNotIn('Private title',self.app.compact_status.cget('text'))
        self.app.close();self.root=tk.Tk();self.app=MonitorApp(self.root,start_worker=False)
        self.root.update()
        self.assertEqual(self.app.compact_scope.get(),'全目录 · 含归档')
        self.assertFalse(self.app.compact_selector.winfo_ismapped())


if __name__=='__main__':
    unittest.main()
