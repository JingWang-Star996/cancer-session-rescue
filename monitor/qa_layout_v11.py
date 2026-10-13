"""Read native widget geometry at separate DPI scales; never capture the screen."""
import ctypes
import json
from pathlib import Path
import tempfile
import time
import tkinter as tk
from unittest.mock import patch
from app import APP_VERSION, MonitorApp


def main():
    try:ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError,OSError):pass
    results=[]
    for scale in (1.3333,1.6667,2.0):
        with tempfile.TemporaryDirectory() as folder, patch('app.CONFIG_DIR',Path(folder)):
            root=tk.Tk()
            root.attributes('-alpha',0)
            root.tk.call('tk','scaling',scale)
            app=MonitorApp(root,start_worker=False)
            app.rows={str(i):dict(id=str(i),title='Synthetic session',mtime=time.time(),size_bytes=1024,
                                 output_tps=2,output_tokens_60s=120) for i in range(5000)}
            for width,height in ((960,600),(1100,650),(1280,800)):
                root.geometry(f'{width}x{height}+0+0')
                root.update()
                app.apply_layout()
                start,cpu=time.perf_counter(),time.process_time()
                app.render();root.update()
                shown=sum(bool(app.tree.bbox(iid)) for iid in app.tree.get_children())
                record=dict(dpi_percent=round(scale/1.3333*100),width=root.winfo_width(),height=root.winfo_height(),
                            table_height=app.tree.winfo_height(),visible_rows=shown,mounted_rows=len(app.tree.get_children()),
                            render_cpu_seconds=time.process_time()-cpu,render_seconds=time.perf_counter()-start,
                            cards_visible=bool(app.cards.winfo_ismapped()),details_visible=bool(app.detail_panel.winfo_ismapped()))
                assert shown>=8,record
                assert len(app.tree.get_children())<=100
                results.append(record)
            app.toggle_compact();root.update()
            app.toggle_topmost(invert=True);root.update()
            assert root.attributes('-topmost')
            assert app.compact_widgets['output_tokens_60s'].get()=='120'
            app.compact_scope.set('全部未归档')
            start=time.perf_counter()
            app.change_compact_scope();root.update()
            assert app.compact_widgets['output_tokens_60s'].get()=='600,000'
            assert not app.compact_selector.winfo_ismapped()
            assert '5000/5000' in app.compact_metric_hints['output_tokens_60s'].cget('text')
            results[-1]['aggregate_5000_seconds']=time.perf_counter()-start
            app.close()
    output=dict(cases=results,screenshot_calls=0,topmost_verified=True,compact_value_verified=True)
    target=Path(__file__).parent/f'qa/layout-{APP_VERSION}.json'
    target.write_text(json.dumps(output,indent=2),encoding='utf-8')
    print(json.dumps(output))


if __name__=='__main__':main()
