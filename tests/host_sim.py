"""Host-side simulation of main.py: stubbed board, scripted button presses.

Runs the real main.py under CPython with fake machine/network/picographics/
utime modules, then walks the four screens, the focus timer (start, pause,
finish, reset), the idle return to the clock, NTP sync from a 2021 RTC and
UK BST edges. The tick counter starts one minute before its 2**30 wrap so
every ticks_diff path crosses the wrap. Plain asserts, no pytest.

Usage: python3 tests/host_sim.py
"""
import calendar
import json
import os
import struct as cstruct
import sys
import time as ctime
import types

HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(HERE, '..', 'main.py')
SAMPLE = os.path.join(HERE, 'open_meteo_sample.json')
with open(os.path.join(HERE, 'bitmap8_widths.json'), encoding='utf-8') as _fh:
    GLYPH_W = json.load(_fh)['widths']

TICKS_PERIOD = 1 << 30
TRUE_START = calendar.timegm((2026, 11, 10, 12, 34, 50, 0, 0, 0))


class StopSim(BaseException):
    """BaseException so main.py's own `except Exception` blocks cannot eat it."""


# ---------------- fake time ----------------
class Clock:
    def __init__(self):
        self.elapsed_ms = 0
        self.tick_base = TICKS_PERIOD - 60 * 1000   # wraps after 60 s
        self.rtc_base = calendar.timegm((2021, 1, 1, 0, 0, 0, 0, 0, 0))
        self.hook = None

    def true_now(self):
        return TRUE_START + self.elapsed_ms // 1000

    def rtc_now(self):
        return self.rtc_base + self.elapsed_ms // 1000

    def advance(self, ms):
        self.elapsed_ms += ms
        if self.hook:
            self.hook(self.elapsed_ms)


clock = Clock()

utime = types.ModuleType('utime')
utime.time = lambda: clock.rtc_now()
utime.gmtime = lambda t=None: tuple(ctime.gmtime(clock.rtc_now() if t is None else t))[:8]
utime.localtime = utime.gmtime
utime.mktime = lambda tup: calendar.timegm(tuple(tup[:6]) + (0, 0, 0))
utime.ticks_ms = lambda: (clock.tick_base + clock.elapsed_ms) % TICKS_PERIOD
utime.ticks_add = lambda a, b: (a + b) % TICKS_PERIOD
utime.ticks_diff = lambda a, b: ((a - b + TICKS_PERIOD // 2) % TICKS_PERIOD) - TICKS_PERIOD // 2
utime.sleep = lambda s: clock.advance(int(s * 1000))
utime.sleep_ms = lambda ms: clock.advance(ms)

# ---------------- fake machine ----------------
button_down = {12: False, 13: False, 14: False, 15: False}
led_rgb = []


irq_handlers = {}


class Pin:
    IN, OUT, PULL_UP, IRQ_FALLING = 0, 1, 2, 4

    def __init__(self, ident, mode=None, pull=None, value=None):
        self.ident = ident

    def irq(self, trigger=None, handler=None):
        assert trigger == Pin.IRQ_FALLING
        irq_handlers[self.ident] = (self, handler)

    def value(self, v=None):
        if self.ident in button_down:
            return 0 if button_down[self.ident] else 1
        return 0

    def toggle(self):
        pass

    def off(self):
        pass


class RTC:
    def datetime(self, t):
        y, mo, d, _wd, h, mi, s, _sub = t
        clock.rtc_base = calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0)) - clock.elapsed_ms // 1000


machine = types.ModuleType('machine')
machine.Pin = Pin
machine.RTC = RTC

# ---------------- fake network / NTP ----------------
net_state = {'connected': False}


class WLAN:
    def __init__(self, iface):
        pass

    def isconnected(self):
        return net_state['connected']

    def active(self, on=None):
        return True

    def scan(self):
        return [(b'GCMembers', b'', 1, -50, 3, False), (b'Other', b'', 6, -70, 3, False)]

    def connect(self, ssid, psk):
        net_state['connected'] = ssid == 'GCMembers'
        net_state['ssid'] = ssid
        net_state['connects'] = net_state.get('connects', 0) + 1

    def status(self):
        return 3 if net_state['connected'] else -2

    def disconnect(self):
        net_state['connected'] = False
        net_state['disconnects'] = net_state.get('disconnects', 0) + 1


network = types.ModuleType('network')
network.WLAN = WLAN
network.STA_IF = 0


class Sock:
    def settimeout(self, t):
        pass

    def sendto(self, data, addr):
        pass

    def recv(self, n):
        pkt = bytearray(48)
        pkt[40:44] = cstruct.pack('!I', clock.true_now() + 2208988800)
        return bytes(pkt)

    def close(self):
        pass


usocket = types.ModuleType('usocket')
usocket.AF_INET, usocket.SOCK_DGRAM = 2, 2
usocket.getaddrinfo = lambda host, port: [(2, 2, 0, '', ('192.0.2.1', port))]
usocket.socket = lambda *a: Sock()

rp2 = types.ModuleType('rp2')
rp2.country = lambda c: None

# ---------------- fake weather ----------------
weather_http = {'ok': True, 'calls': 0}


class Resp:
    status_code = 200

    def json(self):
        with open(SAMPLE) as fh:
            return json.load(fh)

    def close(self):
        pass


def fake_get(url, timeout=None):
    weather_http['calls'] += 1
    assert timeout == 10, 'weather fetch must pass a timeout'
    if not weather_http['ok']:
        raise OSError('simulated network failure')
    return Resp()


urequests = types.ModuleType('urequests')
urequests.get = fake_get

# ---------------- fake display ----------------
frames = []


class PicoGraphics:
    def __init__(self, display=None, rotate=0):
        assert rotate == 270
        self.w, self.h = 135, 240
        self.pen = None
        self.ops = []
        self.bg = None

    def get_bounds(self):
        return self.w, self.h

    def set_backlight(self, v):
        pass

    def set_font(self, f):
        assert f == 'bitmap8', 'this firmware has bitmap fonts only'

    def create_pen(self, r, g, b):
        return {(0, 0, 0): 'black', (255, 255, 255): 'white',
                (0, 182, 170): 'teal', (0, 73, 85): 'dim_teal'}[(r, g, b)]

    def set_pen(self, p):
        self.pen = p

    def clear(self):
        self.bg = self.pen
        self.ops = []

    def measure_text(self, text, scale):
        # Real bitmap8 metrics recorded from the board (tests/bitmap8_widths.json).
        return scale * (sum(GLYPH_W[c] for c in text) + max(0, len(text) - 1))

    def text(self, text, x, y, wrap, scale):
        assert isinstance(scale, int) and scale >= 1, scale
        assert x >= 0 and x + self.measure_text(text, scale) <= self.w + scale, (text, x, scale)
        assert y >= 0 and y + 8 * scale <= self.h, (text, y, scale)
        self.ops.append((text, self.pen, scale, x, y))

    def rectangle(self, x, y, w, h):
        assert x >= 0 and y >= 0 and x + w <= self.w and y + h <= self.h, (x, y, w, h)

    def update(self):
        frames.append({'bg': self.bg, 'ops': list(self.ops), 't': clock.elapsed_ms})


picographics = types.ModuleType('picographics')
picographics.PicoGraphics = PicoGraphics
picographics.DISPLAY_PICO_DISPLAY = 0


class RGBLED:
    def __init__(self, r, g, b):
        pass

    def set_rgb(self, r, g, b):
        led_rgb.append((r, g, b))


pimoroni = types.ModuleType('pimoroni')
pimoroni.RGBLED = RGBLED

wifi_config = types.ModuleType('WIFI_CONFIG')
wifi_config.NETWORKS = [('GCMembers', 'x' * 64), ('CSEFB', 'y' * 13)]
wifi_config.COUNTRY = 'GB'

for name, mod in (('utime', utime), ('machine', machine), ('network', network),
                  ('usocket', usocket), ('ustruct', cstruct), ('rp2', rp2),
                  ('urequests', urequests), ('picographics', picographics),
                  ('pimoroni', pimoroni), ('WIFI_CONFIG', wifi_config)):
    sys.modules[name] = mod


# ---------------- scenario ----------------
def texts():
    return [op[0] for op in frames[-1]['ops']]


def edge(pin):
    """Fire the falling-edge interrupt, as the real pin would."""
    obj, handler = irq_handlers[pin]
    handler(obj)


def press(pin, hold_ms=100):
    """Script a press: down now, up after hold_ms (simulated)."""
    button_down[pin] = True
    edge(pin)
    run_for(hold_ms)
    button_down[pin] = False
    run_for(60)


def tap(pin):
    """A tap that starts and ends between two polls: only the edge is seen."""
    button_down[pin] = True
    edge(pin)
    button_down[pin] = False
    run_for(60)


def run_for(ms):
    target = clock.elapsed_ms + ms
    while clock.elapsed_ms < target:
        yield_point()


def yield_point():
    raise RuntimeError('replaced at runtime')


A, B, X, Y = 12, 13, 14, 15
checks = []


def scenario():
    # Boot: NTP corrects the 2021 RTC, clock shows 12:34 GMT, LED forced off.
    run_for(2000)
    assert led_rgb == [(0, 0, 0)], led_rgb
    assert net_state.get('ssid') == 'GCMembers'
    assert texts() == ['12', '34'], texts()
    assert [op[1] for op in frames[-1]['ops']] == ['white', 'teal']
    checks.append('boot: NTP sync from 2021, clock 12:34, LED off')

    # Weather fetched in the background within the first net tick.
    assert weather_http['calls'] == 1, weather_http
    checks.append('weather fetched once in background')

    # B: date. Then idle 20 s returns to the clock.
    press(B)
    assert texts() == ['TUESDAY', '10', 'NOV'], texts()
    run_for(21000)
    assert texts()[0].isdigit() and len(texts()) == 2, texts()
    checks.append('B date, idle return to clock after 20 s')

    # A tap shorter than one loop pass is recovered from the edge latch.
    tap(B)
    assert texts() == ['TUESDAY', '10', 'NOV'], ('quick tap on B missed', texts())
    press(A)
    assert len(texts()) == 2 and texts()[0].isdigit(), texts()
    checks.append('quick B tap between polls still opens the date')

    # Y: weather from the real Open-Meteo sample.
    press(Y)
    assert texts() == ['10\u00b0C', 'Cloudy', 'High', '10\u00b0C', 'Low', '6\u00b0C',
                       'Rain', '100%'], texts()
    assert [op[1] for op in frames[-1]['ops']] == ['white', 'teal', 'teal', 'white',
                                                   'teal', 'white', 'teal', 'white']
    press(Y)   # second press forces a refresh
    assert weather_http['calls'] == 2, weather_http
    checks.append('Y weather rendered, Y again refreshes')

    # X: timer shows READY 25:00 without starting.
    press(X)
    assert texts() == ['FOCUS', '25', '00', 'READY'], texts()
    # X again starts; after 10 s it reads 24:50 and stays on screen past 20 s.
    press(X)
    run_for(10000)
    assert texts()[:3] == ['FOCUS', '24', '50'], texts()
    run_for(30000)
    assert texts()[0] == 'FOCUS', 'running timer must not return to clock'
    checks.append('X start, countdown, no idle return while running')

    # Contact bounce just after a release must not toggle the timer again.
    press(X)                       # pause
    edge(X)                        # bounce edge after release, pin already up
    run_for(60)
    assert texts()[3:] == ['PAUSED'], ('bounce re-toggled the timer', texts())
    press(X)                       # resume
    assert len(texts()) == 3, ('timer should be running again', texts())
    checks.append('release bounce ignored, timer toggles once per press')

    # Pause holds the value; idle return still applies when paused.
    press(X)
    paused = texts()[:4]
    assert paused[3] == 'PAUSED', paused
    run_for(25000)
    assert len(texts()) == 2 and texts()[0].isdigit(), 'paused timer returns to clock'
    press(X)   # from clock: shows timer, does not toggle
    assert texts()[:4] == paused, (texts(), paused)
    checks.append('pause holds value, idle return, X from clock only shows')

    # Hold X resets to 25:00 READY.
    press(X, hold_ms=1300)
    assert texts() == ['FOCUS', '25', '00', 'READY'], texts()
    checks.append('hold X resets')

    # Run a full 25 minutes from the clock screen: auto-switch and flash.
    press(X)            # start
    press(A)            # go to clock while it runs
    assert len(texts()) == 2
    run_for(25 * 60 * 1000 + 3000)
    assert texts()[0] == 'FOCUS', 'finish switches to timer'
    flash_bgs = {f['bg'] for f in frames if f['ops'] and f['ops'][0][0] == 'FOCUS'
                 and clock.elapsed_ms - f['t'] < 4000}
    assert 'teal' in flash_bgs and 'black' in flash_bgs, flash_bgs
    run_for(5000)
    assert texts() == ['FOCUS', '00', '00', 'DONE'], texts()
    assert frames[-1]['bg'] == 'black'
    run_for(20000)
    assert len(texts()) == 2, 'done screen returns to clock'
    checks.append('25 min finish: auto-switch, teal flash, DONE, return')

    # X on DONE resets.
    press(X)
    press(X)
    assert texts() == ['FOCUS', '25', '00', 'READY'], texts()
    checks.append('X on DONE resets')

    # Weather failure keeps the last good reading until it is 3 h old.
    weather_http['ok'] = False
    press(Y)
    press(Y)
    assert texts()[0] == '10\u00b0C', texts()
    assert net_state.get('disconnects', 0) == 0, net_state
    checks.append('failed refresh keeps last reading')

    # Background retries every 2 min keep failing; the third consecutive
    # failure drops the link and the main loop reconnects on its next tick.
    connects_before = net_state['connects']
    run_for(4 * 60 * 1000 + 5000)
    assert net_state.get('disconnects') == 1, net_state
    assert net_state['connected'] and net_state['connects'] == connects_before + 1, net_state
    checks.append('3 failed fetches force a clean WiFi reconnect')

    # Network back: next scheduled fetch succeeds and resets the count.
    weather_http['ok'] = True
    calls = weather_http['calls']
    run_for(2 * 60 * 1000 + 3000)
    assert weather_http['calls'] > calls and net_state.get('disconnects') == 1, weather_http
    checks.append('fetch recovers after reconnect')
    raise StopSim


def main():
    import threading
    # Run the scenario as a coroutine-like thread synchronised with sleep.
    to_sim = threading.Semaphore(0)
    to_main = threading.Semaphore(0)
    result = {}

    def scenario_thread():
        to_sim.acquire()
        try:
            scenario()
        except StopSim:
            result['ok'] = True
        except BaseException as e:   # report assertion failures
            result['error'] = e
        result['done'] = True
        to_main.release()

    def point():
        to_main.release()
        to_sim.acquire()

    global yield_point
    yield_point = point

    def hook(_elapsed):
        # Each simulated sleep hands control to the scenario once.
        to_sim.release()
        to_main.acquire()
        if result.get('done'):
            raise StopSim

    t = threading.Thread(target=scenario_thread, daemon=True)
    t.start()
    clock.hook = hook
    src = open(MAIN).read()
    g = {'__name__': '__main__'}
    try:
        exec(compile(src, 'main.py', 'exec'), g)
    except StopSim:
        pass
    if 'error' in result:
        raise result['error']
    assert result.get('ok'), result

    # Pure-function checks on the loaded module.
    def ts(*a):
        return calendar.timegm(a + (0, 0, 0))
    assert not g['is_bst'](ts(2026, 3, 29, 0, 59, 59))
    assert g['is_bst'](ts(2026, 3, 29, 1, 0, 0))
    assert g['is_bst'](ts(2026, 10, 25, 0, 59, 59))
    assert not g['is_bst'](ts(2026, 10, 25, 1, 0, 0))
    checks.append('BST edges 29 Mar / 25 Oct 2026 at 01:00 UTC')
    cn = g['condition_name']
    names = {c: cn(c) for c in (0, 1, 2, 3, 45, 53, 63, 73, 81, 85, 96)}
    assert names == {0: 'Sunny', 1: 'Mostly sunny', 2: 'Partly cloudy', 3: 'Cloudy',
                     45: 'Fog', 53: 'Drizzle', 63: 'Rain', 73: 'Snow', 81: 'Showers',
                     85: 'Snow showers', 96: 'Thunderstorm'}, names
    assert (cn(0, 0), cn(1, 0)) == ('Clear', 'Mostly clear')
    checks.append('WMO code names')

    # Weather layout edge cases: no data, and a cold night with long values.
    disp = g['display']
    for data, want in ((None, ['--\u00b0C', 'No data', 'High', '--\u00b0C', 'Low', '--\u00b0C',
                               'Rain', '--']),
                       ((-12, 85, -10, -15, 100, 0), ['-12\u00b0C', 'Snow showers', 'High',
                                                    '-10\u00b0C', 'Low', '-15\u00b0C',
                                                    'Rain', '100%'])):
        disp.set_pen('black')
        disp.clear()
        g['draw_weather'](data)
        got = [op[0] for op in disp.ops]
        assert got == want, got
        # Size 6 for normal readings; '-12°C' is 126 px at 6, so it steps to 5.
        want_hero = 6 if data is None or data[0] > -10 else 5
        assert disp.ops[0][2] == want_hero, ('hero size', disp.ops[0])
        assert disp.ops[1][2] >= 2, ('caption must never fall to size 1', disp.ops[1])
        rows = disp.ops[2:]
        scales = {op[2] for op in rows}
        assert len(scales) == 1, 'rows share one scale'
        for label, value in zip(rows[0::2], rows[1::2]):
            assert label[4] == value[4], 'label and value on one baseline'
            assert label[3] + disp.measure_text(label[0], label[2]) < value[3], (label, value)
    checks.append('weather no-data and cold-night layouts fit, rows aligned')

    for c in checks:
        print('PASS', c)
    print('ALL PASS ({} checks, {} frames drawn, ticks wrapped: {})'.format(
        len(checks), len(frames),
        clock.tick_base + clock.elapsed_ms >= TICKS_PERIOD))


if __name__ == '__main__':
    main()
