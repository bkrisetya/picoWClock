"""Host-side simulation of main.py: stubbed board, scripted button presses.

Runs the real main.py under CPython with fake machine/network/picographics/
utime modules, then walks the four screens, the focus timer (start, pause,
finish, reset, LED glow), the idle return to the clock, night dimming, NTP
sync from a 2021 RTC with bad replies rejected, the crash guard and UK BST
edges. The tick counter starts one minute before its 2**30 wrap so
every ticks_diff path crosses the wrap. Plain asserts, no pytest.

Usage: python3 tests/host_sim.py
"""
import builtins
import calendar
import io
import json
import os
import re
import struct as cstruct
import sys
import time as ctime
import traceback
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


class BoardReset(BaseException):
    """machine.reset(): the board restarts, so the run ends here."""


# MicroPython's sys.print_exception, used by the crash guard.
sys.print_exception = lambda e, f=sys.stdout: traceback.print_exception(
    type(e), e, e.__traceback__, file=f)

# Files main.py writes (the crash log) are kept in memory.
written = {}


class MemFile(io.StringIO):
    def __init__(self, path):
        super().__init__()
        self.path = path

    def close(self):
        written[self.path] = self.getvalue()
        super().close()


def fake_open(path, mode='r', *args, **kwargs):
    if 'w' in mode:
        return MemFile(path)
    return builtins.open(path, mode, *args, **kwargs)


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

    def irq(self, trigger=None, handler=None, hard=False):
        assert trigger == Pin.IRQ_FALLING
        assert hard, 'button edges need a hard IRQ to keep their real timing'
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
machine.disable_irq = lambda: 0
machine.enable_irq = lambda state: None


def board_reset():
    raise BoardReset


machine.reset = board_reset

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
        return net_state.get('scan', [(b'GCMembers', b'', 1, -50, 3, False),
                                      (b'Other', b'', 6, -70, 3, False)])

    def connect(self, ssid, psk):
        ok = ssid == net_state.get('joinable', 'GCMembers') and not net_state.get('down')
        if ok and net_state.get('flaky_every'):   # weak link: only every nth join works
            net_state['flaky_n'] = net_state.get('flaky_n', 0) + 1
            ok = net_state['flaky_n'] % net_state['flaky_every'] == 0
        net_state['connected'] = ok
        net_state['ssid'] = ssid
        net_state['connects'] = net_state.get('connects', 0) + 1
        net_state.setdefault('tried', []).append(ssid)

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
        net_state['ntp'] = net_state.get('ntp', 0) + 1

    def recv(self, n):
        if 'ntp_reply' in net_state:
            return net_state['ntp_reply']
        return ntp_packet(clock.true_now())

    def close(self):
        pass


def ntp_packet(unix_secs, head=0x24, stratum=2):
    """A server reply: LI 0, version 4, mode 4 (0x24), stratum 2."""
    pkt = bytearray(48)
    pkt[0], pkt[1] = head, stratum
    pkt[40:44] = cstruct.pack('!I', unix_secs + 2208988800)
    return bytes(pkt)


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
    assert timeout is not None and timeout <= 20, 'weather fetch must pass a bounded timeout'
    # Presses made while the fetch blocks the loop: only the edge IRQ sees them.
    for pin in weather_http.pop('presses_during', ()):
        button_down[pin] = True
        edge(pin)
        clock.elapsed_ms += 120
        button_down[pin] = False
        clock.elapsed_ms += 200
    if not weather_http['ok']:
        raise OSError('simulated network failure')
    # Link model: TLS costs extra round trips and crypto on top of HTTP.
    latency = weather_http.get('tls_s' if url.startswith('https:') else 'plain_s', 0)
    weather_http['blocked_s'] = min(latency, timeout)
    clock.elapsed_ms += int(min(latency, timeout) * 1000)
    if latency > timeout:
        raise OSError(110, 'ETIMEDOUT')
    return Resp()


urequests = types.ModuleType('urequests')
urequests.get = fake_get

# ---------------- fake display ----------------
frames = []
backlights = []
display_fault = {}


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
        backlights.append(v)

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
        if 'update' in display_fault:
            raise RuntimeError(display_fault.pop('update'))
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


MAIN_G = {}     # main.py's globals, filled in by main()


A, B, X, Y = 12, 13, 14, 15
checks = []


def scenario():
    # Boot: NTP corrects the 2021 RTC, clock shows 12:34 GMT, LED forced off.
    run_for(2000)
    assert led_rgb == [(0, 0, 0)], led_rgb
    assert backlights == [0.55], ('daytime boot must not dim', backlights)
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

    # A, B and Y switch the moment they go down, not on release.
    button_down[B] = True
    edge(B)
    run_for(40)
    assert texts() == ['TUESDAY', '10', 'NOV'], ('B acts on press-down', texts())
    button_down[B] = False
    run_for(60)
    press(A)
    checks.append('A/B/Y switch screens on press-down')

    # Y: weather from the real Open-Meteo sample.
    press(Y)
    # Rain 3h: the highest of the hourly slots 15:00-18:00 (12, 28, 49, 67).
    assert texts() == ['10\u00b0C', 'Cloudy', 'High', '10\u00b0C', 'Low', '6\u00b0C',
                       'Rain 3h', '67%'], texts()
    assert [op[1] for op in frames[-1]['ops']] == ['white', 'teal', 'teal', 'white',
                                                   'teal', 'white', 'teal', 'white']
    press(Y)   # second press asks for a refresh, made once buttons are quiet
    assert weather_http['calls'] == 1, ('fetch must not block the button press', weather_http)
    run_for(3500)
    assert weather_http['calls'] == 2, weather_http
    checks.append('Y weather rendered, Y again refreshes after 3 s quiet')

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

    # A poll landing in a gap of the contact bounce as X goes down sees an
    # edge with the pin up. That must not count as a tap plus a press (it
    # toggled the timer twice on the board: start then pause at once).
    run_for(500)                   # well clear of the last release
    before = texts()[3:]
    edge(X)                        # first contact, pin bounces back up
    yield_point()                  # exactly one loop pass samples the gap
    button_down[X] = True          # contact settles: held
    edge(X)
    run_for(150)
    button_down[X] = False
    run_for(100)
    after = texts()[3:]
    assert after != before, ('press bounce toggled the timer twice', before, after)
    press(X)                       # restore the previous state for what follows
    assert texts()[3:] == before, (before, texts())
    checks.append('press bounce sampled mid-gap toggles the timer once')

    # The first poll after a release catching the pin low again (release
    # bounce) must not start a second press: X would toggle twice.
    run_for(500)
    before = texts()[3:]
    button_down[X] = True
    edge(X)
    run_for(150)
    button_down[X] = False
    yield_point()                  # one loop pass: the release, one toggle
    button_down[X] = True          # release bounce, caught low by the poll
    edge(X)
    yield_point()
    button_down[X] = False
    run_for(300)
    assert texts()[3:] != before, ('release bounce toggled the timer twice', before, texts())
    press(X)                       # back to the previous state
    assert texts()[3:] == before, (before, texts())
    checks.append('release bounce caught low does not press again')

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

    # A long X press from another screen only shows the timer: it must not
    # wipe a running session you cannot see.
    press(X)                       # start
    press(A)
    press(X, hold_ms=1300)
    assert texts()[0] == 'FOCUS' and len(texts()) == 3, ('hold off-screen reset', texts())
    press(X, hold_ms=1300)         # on the timer screen: resets
    assert texts() == ['FOCUS', '25', '00', 'READY'], texts()
    checks.append('hold X resets only on the timer screen')

    # Run a full 25 minutes from the clock screen: auto-switch and flash.
    press(X)            # start
    press(A)            # go to clock while it runs
    assert len(texts()) == 2
    run_for(25 * 60 * 1000 + 3000)
    assert texts()[0] == 'FOCUS', 'finish switches to timer'
    assert led_rgb[-1] == (0, 90, 80), ('LED glows when the session ends', led_rgb[-3:])
    flash_bgs = {f['bg'] for f in frames if f['ops'] and f['ops'][0][0] == 'FOCUS'
                 and clock.elapsed_ms - f['t'] < 4000}
    assert 'teal' in flash_bgs and 'black' in flash_bgs, flash_bgs
    run_for(5000)
    assert texts() == ['FOCUS', '00', '00', 'DONE'], texts()
    assert frames[-1]['bg'] == 'black'
    run_for(20000)
    assert len(texts()) == 2, 'done screen returns to clock'
    assert led_rgb[-1] == (0, 90, 80), 'glow outlasts the screen'
    checks.append('25 min finish: auto-switch, teal flash, DONE, return, LED glow')

    # X on DONE resets; the first press already ends the glow.
    press(X)
    assert led_rgb[-1] == (0, 0, 0), ('a press ends the glow', led_rgb[-3:])
    press(X)
    assert texts() == ['FOCUS', '25', '00', 'READY'], texts()
    checks.append('X on DONE resets')

    # Weather failure keeps the last good reading until it is 3 h old.
    weather_http['ok'] = False
    press(Y)
    press(Y)
    run_for(3500)
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

    # Presses made while a fetch blocks the loop are applied in press order,
    # not in A/B/X/Y order (seen live: 14 of 48 random presses went wrong).
    def fetch_with_presses(pins):
        press(Y)
        press(Y)                     # on weather: request a refresh
        calls = weather_http['calls']
        weather_http['presses_during'] = list(pins)
        run_for(3500)
        assert weather_http['calls'] == calls + 1 and 'presses_during' not in weather_http
        run_for(100)

    fetch_with_presses((B, X, A))
    assert len(texts()) == 2 and texts()[0].isdigit(), ('last press was A', texts())
    fetch_with_presses((A, Y, B))
    assert texts() == ['TUESDAY', '10', 'NOV'], ('last press was B', texts())
    fetch_with_presses((Y, A, X))
    assert texts()[0] == 'FOCUS', ('last press was X', texts())
    checks.append('presses during a blocking fetch apply in press order')

    # Network work waits while buttons are in use.
    press(Y)
    press(Y)                         # refresh requested
    calls = weather_http['calls']
    for pin in (A, B, A, B):
        run_for(1500)
        press(pin)
    assert weather_http['calls'] == calls, 'no fetch while the buttons are in use'
    run_for(3500)
    assert weather_http['calls'] == calls + 1, weather_http
    checks.append('network work deferred until buttons are quiet for 3 s')

    # WiFi down: background joins never block, and both networks are tried.
    net_state['down'] = True
    net_state['connected'] = False
    net_state['tried'] = []
    for pin, want in ((B, 'TUESDAY'), (A, None), (B, 'TUESDAY'), (A, None)) * 3:
        run_for(4500)                # quiet: network ticks run join attempts
        press(pin)
        got = texts()
        assert (got[0] == want) if want else (len(got) == 2 and got[0].isdigit()), \
            ('press not handled at once while WiFi is down', got)
    assert {'GCMembers', 'CSEFB'} <= set(net_state['tried']), net_state['tried']
    net_state['down'] = False
    run_for(25000)
    assert net_state['connected'], net_state
    checks.append('WiFi down: presses instant, joins alternate, link recovers')

    # ticks_ms() wraps every 12.4 days and ticks_diff() lies about stamps
    # more than 6.2 days apart. A clock nobody touches for a week must keep
    # fetching and syncing, then still take a quick tap and age its data.
    def idle_days(days):
        per_day = []
        for _day in range(days):
            fetches, syncs = weather_http['calls'], net_state.get('ntp', 0)
            for _step in range(144):             # 10-minute steps
                clock.elapsed_ms += 10 * 60 * 1000
                run_for(2000)                    # a burst of loop passes
            per_day.append((weather_http['calls'] - fetches,
                            net_state.get('ntp', 0) - syncs))
        return per_day

    per_day = idle_days(8)
    assert all(f >= 24 and s >= 1 for f, s in per_day), \
        ('network work stopped during a week without presses', per_day)
    tap(B)                                       # last B release was 8 days ago
    assert len(texts()) == 3 and texts()[0].endswith('DAY'), \
        ('quick tap after a week read as bounce', texts())
    press(A)
    weather_http['ok'] = False                   # a week of failed fetches
    idle_days(7)
    press(Y)
    assert texts()[:2] == ['--\u00b0C', 'No data'], ('week-old reading shown', texts())
    press(A)
    weather_http['ok'] = True
    checks.append('8 untouched days: fetches and NTP continue, tap seen, old data ages out')

    # Night: 22:00-07:00 UK time dims the backlight; a session that ends at
    # night glows dimmer, and the glow ends by itself after 30 minutes.
    def jump_to_hour(h):
        secs = clock.rtc_now()           # UTC, and UK is on GMT in November
        clock.elapsed_ms += ((h * 3600 - secs % 86400) % 86400) * 1000
        run_for(100)

    jump_to_hour(21)
    assert backlights[-1] == 0.55, backlights[-3:]
    jump_to_hour(22)
    assert backlights[-1] == 0.15, ('22:00 dims', backlights[-3:])
    press(X)
    if texts()[3:] == ['DONE']:
        press(X)
    assert texts() == ['FOCUS', '25', '00', 'READY'], texts()
    press(X)                             # start at night
    clock.elapsed_ms += 25 * 60 * 1000
    run_for(200)
    assert texts()[0] == 'FOCUS' and led_rgb[-1] == (0, 20, 18), ('night glow', led_rgb[-3:])
    clock.elapsed_ms += 31 * 60 * 1000
    run_for(100)
    assert led_rgb[-1] == (0, 0, 0), ('glow must end after 30 min', led_rgb[-3:])
    jump_to_hour(7)
    assert backlights[-1] == 0.55, ('07:00 back to full', backlights[-3:])
    checks.append('night dims 22:00-07:00, night glow dimmer, glow ends after 30 min')

    # Weak campus link (CSEFB at -85 dBm, measured 10/11/2026): a TLS fetch
    # took 1.3-12.5 s, plain HTTP 0.1-0.5 s. A fetch blocks the loop, so on
    # that link it must still succeed and freeze the buttons under a second.
    g = MAIN_G['g']
    weather_http.update(tls_s=12, plain_s=0.5)
    press(Y)
    press(Y)
    calls = weather_http['calls']
    run_for(3500)
    assert weather_http['calls'] == calls + 1, weather_http
    w = g['weather']
    assert w['fails'] == 0 and w['due'] - g['mono_ms']() > 10 * 60 * 1000, ('slow fetch failed', w)
    assert weather_http['blocked_s'] <= 1, ('fetch froze the buttons', weather_http['blocked_s'])
    for k in ('tls_s', 'plain_s'):
        del weather_http[k]
    checks.append('weak link: weather fetch succeeds and blocks under 1 s')

    # Weak join: hidden CSEFB at -87 dBm accepted 4 of 14 attempts, most
    # failing in 0.5 s (10/11/2026). Retry it quickly instead of waiting
    # 5 s and spending every other try on a network that is not there.
    net_state.update(joinable='CSEFB', flaky_every=4, flaky_n=0, tried=[], connected=False)
    t0 = clock.elapsed_ms
    while not net_state['connected'] and clock.elapsed_ms - t0 < 20000:
        run_for(500)
    assert net_state['connected'], ('weak network not joined within 20 s', net_state['tried'])
    for k in ('joinable', 'flaky_every', 'flaky_n'):
        del net_state[k]
    checks.append('weak join: a network accepting 1 in 4 joins is up within 20 s')

    # Crash guard: an unexpected error is logged and the board restarts
    # instead of freezing on its last frame.
    display_fault['update'] = 'simulated display fault'
    checks.append('crash guard armed')
    press(B)
    run_for(10000)
    raise AssertionError('an unexpected error did not restart the board')


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
    g = {'__name__': '__main__', 'open': fake_open}
    MAIN_G.update(g=g)
    try:
        exec(compile(src, 'main.py', 'exec'), g)
    except StopSim:
        pass
    except BoardReset:
        result['reset'] = True
    clock.hook = None
    if 'error' in result:
        raise result['error']
    assert result.get('reset') and checks[-1] == 'crash guard armed', (result, checks[-1:])
    log = written.get('crash.txt', '')
    assert re.match(r'\d\d/\d\d/\d{4} \d\d:\d\d:\d\d UK, uptime \d+ s\n', log), log
    assert 'simulated display fault' in log, log
    assert led_rgb[-1] == (0, 0, 0)
    checks[-1] = 'crash guard: error written to crash.txt, LED off, board restarts'

    # NTP replies that are not a usable time leave the RTC alone.
    ntp = g['ntp_set_rtc']
    good = clock.rtc_base
    now = clock.true_now()
    for label, reply in (('short', ntp_packet(now)[:20]),
                         ('kiss-of-death', ntp_packet(0 - 2208988800, stratum=0)),
                         ('zero timestamp', ntp_packet(0 - 2208988800)),
                         ('unsynchronised', ntp_packet(now, head=0xE4)),
                         ('client mode', ntp_packet(now, head=0x23)),
                         ('year 2021', ntp_packet(calendar.timegm((2021, 1, 1, 0, 0, 0))))):
        net_state['ntp_reply'] = reply
        assert ntp() is False and clock.rtc_base == good, ('bad NTP reply used', label)
    del net_state['ntp_reply']
    assert ntp() is True
    checks.append('NTP: short, kiss-of-death, unsynchronised and implausible replies rejected')

    # Boot join. CSEFB at UCL CS is hidden: a scan shows the UCL networks
    # and nameless entries, never 'CSEFB', yet it joins when asked by name.
    join = g['connect_wifi']
    net_state.update(connected=False, tried=[], joinable='CSEFB',
                     scan=[(b'eduroam', b'', 1, -65, 5, False), (b'', b'', 1, -66, 5, True),
                           (b'CSGUEST', b'', 11, -86, 0, False)])
    assert join() is True and net_state['ssid'] == 'CSEFB', ('hidden network skipped', net_state)
    # Networks the scan can see come first, strongest first.
    net_state.update(connected=False, tried=[], joinable='CSEFB',
                     scan=[(b'GCMembers', b'', 6, -88, 3, False), (b'CSEFB', b'', 11, -60, 3, False)])
    assert join() is True and net_state['tried'] == ['CSEFB'], net_state['tried']
    for k in ('scan', 'joinable'):
        del net_state[k]
    checks.append('boot join: hidden CSEFB tried after visible networks, strongest first')

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
                               'Rain 3h', '--']),
                       ((-12, 85, -10, -15, 100, 0), ['-12\u00b0C', 'Snow showers', 'High',
                                                    '-10\u00b0C', 'Low', '-15\u00b0C',
                                                    'Rain 3h', '100%'])):
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
        assert scales == {2}, ('rows share one readable scale', scales)
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
