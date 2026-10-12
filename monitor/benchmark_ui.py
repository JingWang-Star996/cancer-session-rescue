"""Measure the real Tk widget integration with synthetic rows, without screenshots."""
import json
from pathlib import Path
import tempfile
import time
import tkinter as tk
from unittest.mock import patch

from app import MonitorApp


def main():
    with tempfile.TemporaryDirectory() as folder, patch('app.CONFIG_DIR', Path(folder)):
        root = tk.Tk()
        root.withdraw()
        app = MonitorApp(root, start_worker=False)
        stamp = time.time()
        app.rows = {str(i): dict(id=str(i), title=f'Synthetic {i}', size_bytes=i*1024,
                                mtime=stamp, history=[], model='synthetic') for i in range(5000)}
        start, cpu = time.perf_counter(), time.process_time()
        app.render()
        root.update()
        first = time.perf_counter() - start
        first_cpu = time.process_time() - cpu
        times = []
        cpu = time.process_time()
        for _ in range(10):
            start = time.perf_counter()
            app.render()
            root.update()
            times.append(time.perf_counter()-start)
        result = dict(synthetic_rows=5000, mounted_rows=len(app.tree.get_children()),
                      initial_wall_seconds=first, initial_cpu_seconds=first_cpu,
                      steady_10_renders_cpu_seconds=time.process_time()-cpu,
                      steady_render_wall_mean_seconds=sum(times)/len(times),
                      screenshot_calls=0)
        app.close()
    target = Path(__file__).parent/'qa'/'v1-ui-performance.json'
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
