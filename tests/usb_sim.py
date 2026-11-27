"""Run the real clock with failed Wi-Fi and USB host input, no hardware."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('board_sim', ROOT / 'tests/host_sim.py')
sim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sim)

class Input:
    def __init__(self):
        self.data = ''
    def read(self, n):
        text, self.data = self.data[:n], self.data[n:]
        return text

stream = Input()
class Poll:
    def register(self, stream, flags):
        self.stream = stream
    def poll(self, timeout):
        assert timeout == 0, 'USB polling must never block the UI'
        return [(self.stream, 1)] if self.stream.data else []

sel = types.ModuleType('select')
sel.poll, sel.POLLIN = Poll, 1
sys.modules['select'] = sel
sim._usb_module.select = sel
sys.path.insert(0, str(ROOT))
sys.stdin = stream
sim.net_state.update(down=True)
g = {'__name__': '__main__', 'open': sim.fake_open}
started = None
stage = 0
joins_at_usb = None
remaining_before_step = None

def hook(elapsed):
    global started, stage, joins_at_usb, remaining_before_step
    if 'last_retry' not in g:
        return
    if started is None:
        started = elapsed
        stream.data += '#PC1 ' + json.dumps({'id': 1, 'utc': sim.clock.true_now(),
            'weather': [3, 3, 4, 2, 10, 1], 'weather_at': sim.clock.true_now()}) + '\n'
    age = elapsed - started
    if stage == 0 and age >= 600:
        assert g['uk_time']()[0] == 2026, 'USB host must restore a cold 2021 RTC without Wi-Fi'
        assert g['current_weather']() == (3, 3, 4, 2, 10, 1), 'USB weather must reach the real screen data'
        assert not sim.net_state['connected'], 'test must not succeed through Wi-Fi'
        joins_at_usb = sim.net_state.get('connects', 0)
        g['focus'].toggle()
        g['screen'] = 'timer'
        remaining_before_step = g['focus'].remaining()
        stream.data += '#PC1 ' + json.dumps({'id': 2, 'utc': sim.clock.true_now()+3600}) + '\n'
        stage = 1
    elif stage == 1 and age >= 3000:
        assert g['uk_time']()[3] == 13, 'USB time correction was not consumed'
        left = g['focus'].remaining()
        assert remaining_before_step - 2600 <= left <= remaining_before_step - 2300, 'RTC step disturbed countdown'
        sim.button_down[sim.B] = True
        sim.edge(sim.B)
        stage = 2
    elif stage == 2 and age >= 3090:
        assert sim.texts()[0] == 'TUESDAY', 'button was blocked by USB processing'
        sim.button_down[sim.B] = False
        stage = 3
    elif stage == 3 and age >= 4000:
        sim.button_down[sim.Y] = True
        sim.edge(sim.Y)
        stage = 4
    elif stage == 4 and age >= 4090:
        assert sim.texts()[0] == '3\u00b0C', 'USB weather did not reach the actual display'
        sim.button_down[sim.Y] = False
        stage = 5
    elif stage == 5 and age >= 89000:
        assert sim.net_state.get('connects', 0) == joins_at_usb, 'fresh USB must suppress failing Wi-Fi work'
        stage = 6
    elif stage == 6 and age >= 94000:
        assert sim.net_state.get('connects', 0) > joins_at_usb, 'native Wi-Fi must resume when USB updates stop'
        assert sim.weather_http['calls'] == 0, 'USB fallback must not rely on on-board HTTP'
        raise sim.StopSim

sim.clock.hook = hook
try:
    exec(compile((ROOT / 'main.py').read_text(), 'main.py', 'exec'), g)
except sim.StopSim:
    pass
else:
    raise AssertionError('clock loop stopped unexpectedly')
print('PASS USB cold-start, timer preservation, buttons, weather screen and Wi-Fi fallback')

# Parser boundaries: no allocation growth or accidental RTC changes from noise.
usb = g['usb']
usb.out = io.StringIO()
now = g['mono_ms']()
weather = {'data': None, 'at': 0, 'due': now, 'fails': 0}
valid = {'id': 3, 'utc': sim.TRUE_START, 'weather': [3, 3, 4, 2, None, 1],
         'weather_at': sim.TRUE_START}
def feed(text):
    stream.data += text
    while stream.data:
        before = len(stream.data)
        usb.tick(weather, now)
        assert before - len(stream.data) <= 128, 'USB work per loop must be bounded'

before = sim.clock.rtc_base
for packet in ({'id': True, 'utc': sim.TRUE_START}, {'id': 1, 'utc': 0},
               {'id': 1, 'utc': sim.TRUE_START, 'weather': [3, 3]},
               {'id': 1, 'utc': sim.TRUE_START, 'weather': [3, 3, 4, 2, 101, 1], 'weather_at': sim.TRUE_START},
               {'id': 1, 'utc': sim.TRUE_START, 'weather': [3, 3, 4, 2, 0, 1], 'weather_at': sim.TRUE_START+61}):
    feed('#PC1 '+json.dumps(packet)+'\n')
assert sim.clock.rtc_base == before and not usb.out.getvalue(), 'malformed packet changed the RTC'
feed('#PC1 '+('x'*2000)+'\n')
assert not usb.buffer and not usb.discard, 'oversized frame did not recover at newline'
wire = '#PC1 '+json.dumps(valid)+'\n'
feed(wire[:20])
assert not usb.out.getvalue(), 'partial packet must not be applied'
feed(wire[20:])
assert weather['data'] == (3, 3, 4, 2, None, 1) and '#PC1 OK 3 0' in usb.out.getvalue()
weather.update(data=None, due=now)
old = dict(valid, id=4, weather_at=sim.TRUE_START-10801)
feed('#PC1 '+json.dumps(old)+'\n')
assert weather['data'] is None and '#PC1 OK 4 1' in usb.out.getvalue(), 'stale weather was renewed'
assert g['synced'] is False, 'USB time must not claim successful NTP'
print('PASS USB malformed, fragmented, oversized and stale-weather frames; bounded loop work')
