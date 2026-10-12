"""Smoke the frozen Agent binary using synthetic logs, checking UTF-8 and no writes."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import time


def main():
    exe = Path(__file__).parent/'dist'/'codex-monitor-agent.exe'
    results = []
    sid = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
    with tempfile.TemporaryDirectory() as temp:
        folder = Path(temp)
        home = folder/'home'
        source = home/'sessions'/f'rollout-{sid}.jsonl'
        source.parent.mkdir(parents=True)
        source.write_text('{"type":"event_msg","payload":{"type":"task_complete"}}\n', encoding='utf-8')
        before = hashlib.sha256(source.read_bytes()).hexdigest()
        for command in ('capabilities', 'list', 'diagnose', 'plan'):
            args = [str(exe), command, '--codex-home', str(home), '--privacy']
            if command in ('diagnose', 'plan'):
                args += ['--session', sid]
            started = time.perf_counter()
            done = subprocess.run(args, capture_output=True, timeout=30)
            assert done.returncode == 0, done.stderr
            text = done.stdout.decode('utf-8')
            data = json.loads(text)
            assert sid not in text and str(home) not in text
            if command == 'plan':
                assert '备份' in data['prompt'] and data['execution']['apply_available'] is False
            results.append(dict(command=command, passed=True, wall_seconds=time.perf_counter()-started))
        denied = subprocess.run([str(exe), 'diagnose', '--codex-home', str(home), '--session', sid,
                                 '--private', '--output', str(source)], capture_output=True, timeout=30)
        assert denied.returncode == 2
        assert hashlib.sha256(source.read_bytes()).hexdigest() == before
        report = dict(frozen_cli=results, source_unchanged=True, source_overwrite_rejected=True,
                      utf8_stdout=True, anonymous_outputs=True, no_screenshot=True)
    target = Path(__file__).parent/'qa'/'v1-release-smoke.json'
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report))


if __name__ == '__main__':
    main()
