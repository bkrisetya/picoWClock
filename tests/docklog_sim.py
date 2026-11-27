"""On-board USB activity history, using real disposable files. No hardware."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import sys

sys.dont_write_bytecode = True

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'docklog.py'
assert SOURCE.exists(), 'on-board USB activity recorder is not implemented'
spec = importlib.util.spec_from_file_location('docklog', SOURCE)
assert spec is not None and spec.loader is not None
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def records(directory):
    out = []
    for name in ('dock-history.previous.jsonl', 'dock-history.jsonl'):
        path = Path(directory) / name
        if path.exists():
            for line in path.read_text().splitlines():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
    return out


def test_edges():
    with tempfile.TemporaryDirectory(prefix='pico-dock-test-') as directory:
        status = [0x40051005]  # Read from the actual running RP2350.
        log = mod.DockLog(lambda: status[0], directory=directory)
        for ms in range(0, 2001, 250):
            log.tick(ms, '2026-11-27T13:45:48Z')
        assert [r['event'] for r in records(directory)] == ['start', 'up']
        status[0] |= 0x10  # SUSPENDED: no active USB traffic, not proof of unplugging.
        for ms in range(2250, 4251, 250):
            log.tick(ms, '2026-11-27T13:45:50Z')
        status[0] &= ~0x10
        for ms in range(4500, 6501, 250):
            log.tick(ms, '2026-11-27T13:45:52Z')
        rows = records(directory)
        assert [r['event'] for r in rows] == ['start', 'up', 'down', 'up'], rows
        assert rows[2]['ms'] == 2250, 'edge time is the first observation, not debounce completion'
        assert all(set(r) == {'v', 'run', 'ms', 'utc', 'event'} for r in rows)
        assert not log.error


def test_rotation_and_restart():
    with tempfile.TemporaryDirectory(prefix='pico-dock-test-') as directory:
        status = [0x10000]
        log = mod.DockLog(lambda: status[0], directory=directory)
        log.max_bytes = 512
        for edge in range(30):
            status[0] ^= 0x10
            for offset in range(0, 1501, 250):
                log.tick(edge * 2000 + offset, '2026-11-27T14:00:00Z')
        files = list(Path(directory).iterdir())
        assert {p.name for p in files} == {'dock-history.jsonl', 'dock-history.previous.jsonl'}
        assert all(p.stat().st_size <= 512 for p in files), 'history exceeds its byte budget'
        rows = records(directory)
        assert 2 <= len(rows) < 31, 'rotation must retain recent history, not everything'
        assert rows[-1]['event'] == 'up'
        next_log = mod.DockLog(lambda: status[0], directory=directory)
        next_log.max_bytes = 512
        next_log.tick(0, None)
        assert records(directory)[-1]['run'] == 2, 'restart must not join two separate runs'


def test_unsynchronised_time():
    with tempfile.TemporaryDirectory(prefix='pico-dock-test-') as directory:
        log = mod.DockLog(lambda: 0x10000, directory=directory)
        for ms in range(0, 2001, 250):
            log.tick(ms, None)
        assert all(r['utc'] is None for r in records(directory)), 'never log the reset 2021 RTC as real time'
        log.tick(5000, '2026-11-27T14:01:00Z')
        rows = records(directory)
        assert rows[-1]['event'] == 'time_anchor', 'later NTP must anchor earlier events in the same run'
        assert rows[-1]['ms'] == 5000 and rows[-1]['run'] == rows[0]['run']
        log.tick(6000, '2026-11-27T14:01:01Z')
        assert len(records(directory)) == len(rows), 'a stable clock must not write repeatedly'


def test_fail_safe():
    with tempfile.TemporaryDirectory(prefix='pico-dock-test-') as directory:
        log = mod.DockLog(lambda: 0x10000, directory=directory)
        def fail_open(*args, **kwargs):
            raise OSError('flash write refused')
        setattr(mod, 'open', fail_open)
        try:
            log.tick(0, None)
            assert log.error and 'flash write refused' in log.error
            log.tick(2000, None)  # no retry storm, exception, or clock reboot
        finally:
            delattr(mod, 'open')
        def fail_status():
            raise OSError('USB status unavailable')
        log = mod.DockLog(fail_status, directory=directory)
        log.tick(0, None)
        assert log.error and 'USB status unavailable' in log.error


def test_interrupted_write():
    with tempfile.TemporaryDirectory(prefix='pico-dock-test-') as directory:
        path = Path(directory) / 'dock-history.jsonl'
        path.write_text(json.dumps({'run': 7, 'event': 'up'}) + '\n{"run": 7')
        log = mod.DockLog(lambda: 0x10000, directory=directory)
        log.tick(0, None)
        rows = records(directory)
        assert rows[-1]['event'] == 'start' and rows[-1]['run'] == 8, rows
        for ms in range(250, 1251, 250):
            log.tick(ms, None)
        assert records(directory)[-1]['event'] == 'up'


def test_debounce_and_long_run():
    with tempfile.TemporaryDirectory(prefix='pico-dock-test-') as directory:
        status = [0x10000]
        log = mod.DockLog(lambda: status[0], directory=directory)
        for ms in range(0, 1251, 250):
            log.tick(ms, None)
        status[0] = 0  # No CONNECTED bit, even if SUSPENDED is clear.
        log.tick(1500, None)
        status[0] = 0x10000
        log.tick(1750, None)
        log.tick(3000, None)
        assert [r['event'] for r in records(directory)] == ['start', 'up'], 'ignore a short reset'
        status[0] = 0
        log.tick(8 * 86400000, None)
        log.tick(8 * 86400000 + 1000, None)
        assert records(directory)[-1]['event'] == 'down', 'long monotonic times must not wrap'


if __name__ == '__main__':
    tests = [test_edges, test_rotation_and_restart, test_unsynchronised_time,
             test_fail_safe, test_interrupted_write, test_debounce_and_long_run]
    for test in tests:
        test()
        print('PASS', test.__name__)
    print('ALL PASS ({} USB history checks)'.format(len(tests)))
