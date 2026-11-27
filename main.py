# picoWClock - desk clock for Raspberry Pi Pico 2 W + Pimoroni Display Pack
# 1.14" (PIM543, 240x135), portrait. Fork of chriskalv/picoWClock (Display
# Pack 2.0), rebuilt for the small panel with one screen per button:
#   A  clock (default): hours white on top, minutes teal below
#   B  date: weekday, day number, month
#   X  focus timer, 25 minutes: X again starts or pauses, hold X on the
#      timer screen to reset; the RGB LED glows when a session finishes
#   Y  London weather from Open-Meteo (no key): Y again refreshes
# A, B and Y act the moment they go down; X acts on release, because a
# hold means reset. Other screens fall back to the clock after 20 s without
# a press, except while the timer runs. The backlight dims at night. UK
# time (GMT / BST). WiFi networks live in WIFI_CONFIG.py (git-ignored; the
# owner fills it in).
#
# If WiFi or NTP is unreachable the clock keeps running from the RTC and
# retries in the background instead of hanging or rebooting. An unexpected
# error is written to crash.txt and the board restarts itself.

import gc
import machine
import network
import sys
import utime as time
import usocket as socket
import ustruct as struct

import WIFI_CONFIG

gc.enable()

BRIGHTNESS = 0.55
NIGHT_BRIGHTNESS = 0.15
NIGHT_HOURS = (22, 7)           # UK local time: dim from 22:00 until 07:00
# Finished-timer glow on the pack's RGB LED (0-255 per channel), dimmer at
# night. It stays on until any button is pressed, or GLOW_MAX_MS passes.
GLOW_RGB = (0, 90, 80)
GLOW_NIGHT_RGB = (0, 20, 18)
GLOW_MAX_MS = 30 * 60 * 1000
CRASH_LOG = 'crash.txt'
NTP_HOST = 'pool.ntp.org'
RETRY_EVERY_S = 60
# Network calls block the loop (a weather fetch takes 0.1-0.5 s, longer
# on a stall), so they run only once the buttons have been left alone
# this long.
NET_QUIET_MS = 3000
WIFI_ATTEMPT_MS = 15 * 1000     # longest a background join attempt may hang
WIFI_FAIL_WAIT_MS = 1500        # minimum spacing when a join fails outright
# Attempts on one network before moving to the next. A weak network can
# refuse most joins yet still work: hidden CSEFB at -87 dBm accepted 4 of
# 14 back-to-back attempts, most failing within 0.5 s.
WIFI_TRIES_PER_NET = 5
WEATHER_TIMEOUT_S = 8
RESYNC_EVERY_S = 6 * 3600
MIN_PLAUSIBLE_YEAR = 2024

POLL_MS = 30
HOLD_MS = 1000
IDLE_RETURN_MS = 20 * 1000
FOCUS_MS = 25 * 60 * 1000
DONE_FLASH_MS = 6 * 1000

# Hourly precipitation_probability is for the hour ending at each stamp,
# starting with the current hour (already partly over). Slots 1-4 cover
# the top of this hour to four hours ahead, so whatever the minute they
# span the whole of the next three hours.
# Plain HTTP on purpose: on weak campus Wi-Fi (CSEFB, -85 dBm) the TLS
# handshake took 1.3-12.5 s and froze the buttons, while HTTP took
# 0.1-0.5 s. The request carries only public coordinates and no key.
WEATHER_URL = ('http://api.open-meteo.com/v1/forecast'
               '?latitude=51.5246&longitude=-0.1340'
               '&current=temperature_2m,weather_code,is_day'
               '&hourly=precipitation_probability&forecast_hours=5'
               '&daily=temperature_2m_max,temperature_2m_min'
               '&timezone=Europe%2FLondon&forecast_days=1')
WEATHER_EVERY_MS = 15 * 60 * 1000
WEATHER_RETRY_MS = 2 * 60 * 1000
WEATHER_MAX_AGE_MS = 3 * 3600 * 1000

DAYS = ('MONDAY', 'TUESDAY', 'WEDNESDAY', 'THURSDAY',
        'FRIDAY', 'SATURDAY', 'SUNDAY')
MONTHS = (None, 'JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
          'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC')

# ---------------- monotonic time ----------------
# ticks_ms() wraps every 2**30 ms (12.4 days) and ticks_diff() is only
# valid for stamps less than half that apart, so a stamp left alone for a
# week (the last press, the last weather reading) would read as recent or
# in the future. Every stamp that can age is therefore kept on this
# non-wrapping millisecond count, advanced by the loop many times a second.
# Only the button interrupt keeps raw ticks (it must not allocate), and
# those stamps are consumed within one loop pass.
_mono = [0, time.ticks_ms()]


def mono_ms():
    t = time.ticks_ms()
    _mono[0] += time.ticks_diff(t, _mono[1])
    _mono[1] = t
    return _mono[0]


# ---------------- display ----------------
from picographics import PicoGraphics, DISPLAY_PICO_DISPLAY

display = PicoGraphics(display=DISPLAY_PICO_DISPLAY, rotate=270)
width, height = display.get_bounds()
display.set_backlight(BRIGHTNESS)
display.set_font('bitmap8')

# The framebuffer is RGB332 (3-3-2 bits), so accents are picked on levels
# that survive the quantisation: teal is green 5/7 with blue 2/3.
black = display.create_pen(0, 0, 0)
white = display.create_pen(255, 255, 255)
teal = display.create_pen(0, 182, 170)
dim_teal = display.create_pen(0, 73, 85)

led_onboard = machine.Pin('LED', machine.Pin.OUT, value=0)

# The pack's RGB LED is common-anode. Left unconfigured, the GPIO pull-downs
# sink enough current to make the red die glow, so drive it explicitly off.
from pimoroni import RGBLED

rgb_led = RGBLED(6, 7, 8)
rgb_led.set_rgb(0, 0, 0)

display.set_pen(black)
display.clear()
display.update()


# ---------------- WiFi ----------------
wifi = {'idx': 0, 'attempt_at': None, 'tries': 0}


def wifi_connected():
    wlan = network.WLAN(network.STA_IF)
    return wlan.isconnected()


def known_networks():
    """(ssid, psk) pairs from WIFI_CONFIG: NETWORKS list, or legacy SSID/PSK."""
    nets = getattr(WIFI_CONFIG, 'NETWORKS', None)
    if nets:
        return list(nets)
    return [(WIFI_CONFIG.SSID, WIFI_CONFIG.PSK)]


def connect_wifi(timeout_s=15, show=False):
    """Join whichever known network is in range. Returns True on success.

    show=True paints a status screen (boot only); background retries stay
    silent so the face on screen is never blanked.
    """
    try:
        import rp2
        rp2.country(WIFI_CONFIG.COUNTRY)
    except (ImportError, AttributeError, ValueError):
        pass
    wlan = network.WLAN(network.STA_IF)
    if wlan.isconnected():
        return True
    if show:
        display.set_pen(black)
        display.clear()
        draw_centred('WiFi', (height - 32) // 2, 4, white)
        display.update()
    wlan.active(True)
    known = known_networks()
    # Networks the scan can see go first, strongest first. The rest are
    # still tried afterwards: a hidden network (CSEFB at UCL CS) never
    # shows its name in a scan, yet joins when asked for by name.
    signal = {}
    try:
        for s in wlan.scan():
            name = s[0].decode('utf-8', 'replace')
            signal[name] = max(signal.get(name, -999), s[3])
    except OSError:
        pass  # scan failed: try every known network in order
    nets = sorted(known, key=lambda n: -signal.get(n[0], -999))
    for ssid, psk in nets:
        wifi['idx'] = known.index((ssid, psk))
        wlan.connect(ssid, psk)
        for _ in range(timeout_s):
            if wlan.status() < 0 or wlan.status() >= 3:
                break
            led_onboard.toggle()
            time.sleep(1)
        led_onboard.off()
        if wlan.isconnected():
            return True
        wlan.disconnect()
    return False


def wifi_tick():
    """Background (re)join that never blocks: start one attempt, look at
    it on later ticks, and retry when it fails or takes longer than
    WIFI_ATTEMPT_MS, moving to the next known network after
    WIFI_TRIES_PER_NET attempts. It starts with the network that last
    worked. Returns True while connected."""
    wlan = network.WLAN(network.STA_IF)
    if wlan.isconnected():
        wifi['attempt_at'] = None
        wifi['tries'] = 0
        return True
    now = mono_ms()
    started = wifi['attempt_at']
    nets = known_networks()
    if started is not None:
        age = now - started
        failed = wlan.status() < 0
        if age < WIFI_ATTEMPT_MS and not (failed and age >= WIFI_FAIL_WAIT_MS):
            return False                      # attempt still in progress
        wifi['tries'] += 1
        if wifi['tries'] >= WIFI_TRIES_PER_NET:
            wifi['tries'] = 0
            wifi['idx'] = (wifi['idx'] + 1) % len(nets)
    ssid, psk = nets[wifi['idx'] % len(nets)]
    try:
        wlan.active(True)
        if started is not None:
            wlan.disconnect()        # abandon the stale attempt first
        wlan.connect(ssid, psk)
    except OSError:
        pass
    wifi['attempt_at'] = now
    return False


# ---------------- NTP ----------------
def ntp_set_rtc():
    """Query NTP over UDP and set the RTC to UTC. Returns True on success.

    A reply is used only if it is a full server packet (mode 4) from a
    synchronised server (stratum 1-15, leap indicator not 3) and gives a
    plausible year. Anything else, such as a short packet or a
    kiss-of-death (stratum 0, zero timestamp), leaves the RTC alone."""
    query = bytearray(48)
    query[0] = 0x1B
    try:
        addr = socket.getaddrinfo(NTP_HOST, 123)[0][-1]
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.settimeout(2)
            s.sendto(query, addr)
            msg = s.recv(48)
        finally:
            s.close()
    except OSError:
        return False
    if (len(msg) < 48 or msg[0] & 7 != 4 or msg[0] >> 6 == 3
            or not 0 < msg[1] < 16):
        return False
    seconds = struct.unpack('!I', msg[40:44])[0] - 2208988800
    if seconds < 0:
        return False
    tm = time.gmtime(seconds)
    if not MIN_PLAUSIBLE_YEAR <= tm[0] < 2100:
        return False
    # RTC stays in UTC; local time is derived at render time.
    machine.RTC().datetime((tm[0], tm[1], tm[2], tm[6], tm[3], tm[4], tm[5], 0))
    return True


# ---------------- UK time ----------------
def last_sunday(year, month):
    """Day number of the last Sunday of the given month."""
    weekday_of_31st = time.localtime(time.mktime((year, month, 31, 12, 0, 0, 0, 0)))[6]
    return 31 - ((weekday_of_31st + 1) % 7)


def is_bst(now):
    """True inside UK summer time (last Sun Mar 01:00 UTC - last Sun Oct 01:00 UTC)."""
    year = time.gmtime(now)[0]
    march = time.mktime((year, 3, last_sunday(year, 3), 1, 0, 0, 0, 0))
    october = time.mktime((year, 10, last_sunday(year, 10), 1, 0, 0, 0, 0))
    return march <= now < october


def uk_time():
    """Local (GMT/BST) time tuple derived from the UTC RTC."""
    now = time.time()
    if is_bst(now):
        now += 3600
    return time.localtime(now)


def is_night(tm):
    """True between NIGHT_HOURS (UK local). Never before the first sync,
    when the RTC still reads 2021 and the hour means nothing."""
    if tm[0] < MIN_PLAUSIBLE_YEAR:
        return False
    start, end = NIGHT_HOURS
    return tm[3] >= start or tm[3] < end


backlight = [BRIGHTNESS]


def set_brightness(level):
    if backlight[0] != level:
        backlight[0] = level
        display.set_backlight(level)


# ---------------- finished-timer glow ----------------
glow = {'since': None, 'rgb': (0, 0, 0)}


def led(rgb):
    if glow['rgb'] != rgb:
        glow['rgb'] = rgb
        rgb_led.set_rgb(*rgb)


def glow_tick(night):
    """Keep the LED at the right level: lit from a finished session until a
    press or GLOW_MAX_MS, dimmer at night, otherwise off."""
    if glow['since'] is not None and mono_ms() - glow['since'] > GLOW_MAX_MS:
        glow['since'] = None
    if glow['since'] is None:
        led((0, 0, 0))
    else:
        led(GLOW_NIGHT_RGB if night else GLOW_RGB)


# ---------------- buttons ----------------
BUTTONS = [(name, machine.Pin(pin, machine.Pin.IN, machine.Pin.PULL_UP))
           for name, pin in (('A', 12), ('B', 13), ('X', 14), ('Y', 15))]
DEBOUNCE_MS = 80
INSTANT = ('A', 'B', 'Y')   # act on press-down; X waits for release (hold)
SETTLE_MS = 20              # contact bounce lasts a few ms
pressed_at = {}
hold_fired = {}
released_at = {}

# The loop samples the pins only between redraws and network calls, so a
# press can begin and end unseen. A hard (true hardware) interrupt stamps
# every falling edge at the moment it happens, even while the loop is
# blocked, and poll_buttons() hands presses back in the order they were made.
# The handler must not allocate, so it writes only into these fixed lists.
# fall_edges counts electrical presses per button (A, B, X, Y order, contact
# bounce included); read it over the REPL to tell a dead switch from software.
fell_seen = bytearray(len(BUTTONS))
fell_first = [0] * len(BUTTONS)
fell_last = [0] * len(BUTTONS)
fall_edges = [0] * len(BUTTONS)


def _fell(i):
    t = time.ticks_ms()
    if not fell_seen[i]:
        fell_first[i] = t
        fell_seen[i] = 1
    fell_last[i] = t
    fall_edges[i] += 1


for _i, (_name, _pin) in enumerate(BUTTONS):
    _pin.irq(trigger=machine.Pin.IRQ_FALLING,
             handler=lambda _p, i=_i: _fell(i), hard=True)


def take_edges(i, now):
    """(seen, first, last) for button i since the previous call, with the
    interrupt's tick stamps moved onto mono_ms() (now)."""
    irq_state = machine.disable_irq()
    seen, first, last = fell_seen[i], fell_first[i], fell_last[i]
    fell_seen[i] = 0
    machine.enable_irq(irq_state)
    if not seen:
        return 0, 0, 0
    t = _mono[1]
    return seen, now - time.ticks_diff(t, first), now - time.ticks_diff(t, last)


def poll_buttons():
    """Return (name, 'press' | 'hold') events, oldest press first.

    A, B and Y fire as soon as the press begins. X fires on release, so a
    hold (once, after HOLD_MS while still down) can mean reset instead. A
    press that began and ended between two polls is recovered from the edge
    stamps. A press starting within DEBOUNCE_MS of the button's previous
    release is contact bounce from that release and is ignored, whether
    the poll saw only its edge or caught the pin low in the middle of it."""
    events = []
    now = mono_ms()
    for i, (name, pin) in enumerate(BUTTONS):
        seen, first, last = take_edges(i, now)
        down = pin.value() == 0
        if name in pressed_at:
            start = pressed_at[name]
            if down:
                if not hold_fired[name] and now - start >= HOLD_MS:
                    hold_fired[name] = True
                    events.append((start, name, 'hold'))
                continue
            del pressed_at[name]
            released_at[name] = now
            held = hold_fired.pop(name)
            if name not in INSTANT and not held and now - start >= 20:
                events.append((start, name, 'press'))
            continue
        if not (down or seen):
            continue
        start = first if seen else now
        prev = released_at.get(name)
        if prev is not None and start - prev <= DEBOUNCE_MS:
            continue
        if not down and now - last > SETTLE_MS:
            # Pin up and no falling edge for SETTLE_MS: a whole tap made
            # while the loop was busy, over already.
            released_at[name] = now
            events.append((start, name, 'press'))
            continue
        # Pressed now, or pin up just after an edge, which may be a poll
        # sampling a gap in the contact bounce of a press still going down.
        # Mark it down; the next poll sees the release (X fires then) or the
        # button still held, so one press never fires twice.
        pressed_at[name] = start
        hold_fired[name] = False
        if name in INSTANT:
            events.append((start, name, 'press'))
    events.sort(key=lambda e: e[0])
    return [(name, kind) for _t, name, kind in events]


# ---------------- focus timer ----------------
class FocusTimer:
    """25-minute countdown on mono_ms(), so an NTP step of the RTC never
    disturbs it."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.state = 'ready'   # ready | running | paused | done
        self.left = FOCUS_MS
        self.end = 0
        self.done_at = 0

    def remaining(self):
        if self.state == 'running':
            return max(0, self.end - mono_ms())
        return self.left

    def toggle(self):
        if self.state in ('ready', 'paused'):
            self.end = mono_ms() + self.left
            self.state = 'running'
        elif self.state == 'running':
            self.left = self.remaining()
            self.state = 'paused'
        else:
            self.reset()

    def update(self):
        """True exactly once, when a running countdown reaches zero."""
        if self.state == 'running' and self.remaining() == 0:
            self.state = 'done'
            self.left = 0
            self.done_at = mono_ms()
            return True
        return False

    def flashing(self):
        return (self.state == 'done'
                and mono_ms() - self.done_at < DONE_FLASH_MS)


focus = FocusTimer()


# ---------------- weather ----------------
# WMO weather codes, as (highest code in band, name). 0 and 1 depend on
# day or night and are handled in condition_name().
CONDITIONS = ((2, 'Partly cloudy'), (3, 'Cloudy'), (48, 'Fog'),
              (57, 'Drizzle'), (67, 'Rain'), (77, 'Snow'), (82, 'Showers'),
              (86, 'Snow showers'), (99, 'Thunderstorm'))

WEATHER_FAILS_BEFORE_RECONNECT = 3

weather = {'data': None, 'at': 0, 'due': mono_ms(), 'fails': 0}


def condition_name(code, is_day=1):
    """WMO weather code to a short phrase."""
    if code == 0:
        return 'Sunny' if is_day else 'Clear'
    if code == 1:
        return 'Mostly sunny' if is_day else 'Mostly clear'
    for ceiling, name in CONDITIONS:
        if code <= ceiling:
            return name
    return 'Unknown'


def fetch_weather():
    """(temp, code, high, low, rain_pct, is_day) for London, or None.
    rain_pct is the highest hourly chance over the next three hours."""
    try:
        import urequests
        gc.collect()
        r = urequests.get(WEATHER_URL, timeout=WEATHER_TIMEOUT_S)
        try:
            if r.status_code != 200:
                return None
            d = r.json()
        finally:
            r.close()
        cur = d['current']
        day = d['daily']
        hours = [p for p in d['hourly']['precipitation_probability'][1:5]
                 if p is not None]
        rain = max(hours) if hours else None
        return (round(cur['temperature_2m']), int(cur['weather_code']),
                round(day['temperature_2m_max'][0]),
                round(day['temperature_2m_min'][0]),
                None if rain is None else int(rain),
                int(cur.get('is_day', 1)))
    except Exception:
        return None


def request_weather():
    """Ask for a fresh reading on the next quiet network tick (Y on the
    weather screen). Fetching inline would freeze the buttons."""
    weather['due'] = mono_ms()


def refresh_weather():
    if mono_ms() < weather['due']:
        return
    if not wifi_connected():
        return
    data = fetch_weather()
    now = mono_ms()
    if data:
        weather['data'] = data
        weather['at'] = now
        weather['due'] = now + WEATHER_EVERY_MS
        weather['fails'] = 0
    else:
        weather['due'] = now + WEATHER_RETRY_MS
        weather['fails'] += 1
        # A link can report connected while passing no TCP (seen live).
        # Drop it so the main loop's retry makes a clean reconnect.
        if weather['fails'] >= WEATHER_FAILS_BEFORE_RECONNECT:
            weather['fails'] = 0
            network.WLAN(network.STA_IF).disconnect()


def current_weather():
    if weather['data'] is None:
        return None
    if mono_ms() - weather['at'] > WEATHER_MAX_AGE_MS:
        return None
    return weather['data']


# ---------------- drawing ----------------
def fit_scale(text, max_w, start):
    """Largest integer bitmap scale whose rendered width fits max_w."""
    scale = int(start)
    while scale > 1 and display.measure_text(text, scale) > max_w:
        scale -= 1
    return scale


def draw_centred(text, y, scale, pen):
    display.set_pen(pen)
    x = max(0, (width - display.measure_text(text, scale)) // 2)
    display.text(text, x, y, width, scale)


def band_scale(text, y0, y1, max_scale):
    """Largest scale that fits text across the panel and inside band y0..y1."""
    return max(1, min(fit_scale(text, width - 8, max_scale), (y1 - y0 - 4) // 8))


def draw_band(text, y0, y1, pen, scale):
    """Draw text centred horizontally and vertically inside band y0..y1."""
    draw_centred(text, y0 + max(0, (y1 - y0 - 8 * scale) // 2), scale, pen)


def draw_pair(top, bottom, y0, y1, top_pen, bottom_pen):
    """Two equal-size rows splitting y0..y1 (the clock's digits)."""
    mid = (y0 + y1) // 2
    scale = min(band_scale(top, y0, mid, 15), band_scale(bottom, mid, y1, 15))
    draw_band(top, y0, mid, top_pen, scale)
    draw_band(bottom, mid, y1, bottom_pen, scale)


def draw_clock(tm, synced_time):
    if synced_time:
        hh = '{:02d}'.format(tm[3])
        mm = '{:02d}'.format(tm[4])
    else:
        hh = mm = '--'
    draw_pair(hh, mm, 0, height, white, teal)


def draw_date(tm, synced_time):
    if synced_time:
        day_name, day_num, month = DAYS[tm[6]], str(tm[2]), MONTHS[tm[1]]
    else:
        day_name, day_num, month = '--', '--', '--'
    draw_band(day_name, 0, 60, teal, band_scale(day_name, 0, 60, 4))
    draw_band(day_num, 60, 176, white, band_scale(day_num, 60, 176, 15))
    draw_band(month, 176, height, teal, band_scale(month, 176, height, 8))


# Timer layout: unlike the clock's two stacked numbers, one 'MM:SS' line
# with a colon and a bar that drains as the session runs, so a glance can
# never read a countdown as the time of day.
TIMER_BAR = (10, 160, 12)   # (side margin, top y, height)


def draw_countdown(mm, ss, y0, y1, pen):
    """'MM:SS' on one line. The font's own colon sits a pixel from each
    digit, so it is drawn on its own with two glyph-widths either side.
    The size is fixed by the widest digits, so it never jumps mid-session."""
    scale = 6
    while scale > 1 and 2 * display.measure_text('00', scale) + 5 * scale > width - 8:
        scale -= 1
    wm = display.measure_text(mm, scale)
    x = (width - wm - display.measure_text(ss, scale) - 5 * scale) // 2
    y = y0 + (y1 - y0 - 8 * scale) // 2
    display.set_pen(pen)
    display.text(mm, x, y, width, scale)
    display.text(':', x + wm + 2 * scale, y, width, scale)
    display.text(ss, x + wm + 5 * scale, y, width, scale)


def draw_timer(secs, flash):
    fg, accent = (black, black) if flash else (white, teal)
    label = {'ready': 'READY', 'paused': 'PAUSED', 'done': 'DONE'}.get(focus.state, '')
    draw_band('FOCUS', 0, 40, accent, band_scale('FOCUS', 0, 40, 3))
    draw_countdown('{:02d}'.format(secs // 60), '{:02d}'.format(secs % 60), 40, 150, fg)
    margin, top, tall = TIMER_BAR
    full = width - 2 * margin
    left = (full * secs + FOCUS_MS // 1000 - 1) // (FOCUS_MS // 1000)
    if not flash:
        display.set_pen(dim_teal)
        display.rectangle(margin, top, full, tall)
    if left:
        display.set_pen(accent)
        display.rectangle(margin, top, min(full, left), tall)
    if label:
        draw_band(label, 190, height, fg, band_scale(label, 190, height, 3))


# Weather layout (portrait 135 x 240): big temperature, condition caption,
# a divider, then three label/value rows. Each slot is a fixed band, so the
# layout never shifts; smaller text is centred inside its band. Spacing:
# 16 px top and bottom, 18 px either side of the divider, 19 px between rows.
WX_MARGIN = 10
WX_HERO = (16, 48, 6, 2 * WX_MARGIN)   # (top y, band height, max scale, side room)
WX_CAPTION = (76, 24, 3, 8)            # long names may use more width to stay at 2
WX_DIVIDER_Y = 118
WX_ROWS_Y = (138, 173, 208)
WX_ROW_BAND = 16
WX_ROW_MAX_SCALE = 2   # 3 cannot fit 'High' and '10°C' with a readable gap
# 'Rain 3h' plus '100%' is the widest row: 112 of 115 px at scale 2.


def degrees_c(value):
    return '{}\u00b0C'.format('--' if value is None else value)


def draw_slot(text, slot, pen):
    top, band, max_scale, side_room = slot
    scale = fit_scale(text, width - side_room, max_scale)
    draw_centred(text, top + (band - 8 * scale) // 2, scale, pen)


def row_scale(rows):
    """One scale for every row: the largest at which each label and value
    fit side by side with at least two glyph-widths of space between."""
    scale = WX_ROW_MAX_SCALE
    room = width - 2 * WX_MARGIN
    while scale > 1 and any(display.measure_text(label, scale)
                            + display.measure_text(value, scale)
                            + 6 * scale > room for label, value in rows):
        scale -= 1
    return scale


def draw_weather(data):
    if data is None:
        temp = high = low = rain = None
        caption = 'No data'
    else:
        temp, code, high, low, rain, is_day = data
        caption = condition_name(code, is_day)
    rows = (('High', degrees_c(high)),
            ('Low', degrees_c(low)),
            ('Rain 3h', '--' if rain is None else '{}%'.format(rain)))

    draw_slot(degrees_c(temp), WX_HERO, white)
    draw_slot(caption, WX_CAPTION, teal)

    display.set_pen(dim_teal)
    display.rectangle(WX_MARGIN, WX_DIVIDER_Y, width - 2 * WX_MARGIN, 2)

    scale = row_scale(rows)
    for (label, value), y in zip(rows, WX_ROWS_Y):
        y += (WX_ROW_BAND - 8 * scale) // 2
        display.set_pen(teal)
        display.text(label, WX_MARGIN, y, width, scale)
        display.set_pen(white)
        x = width - WX_MARGIN - display.measure_text(value, scale)
        display.text(value, x, y, width, scale)


last_frame = None


def render(screen):
    """Redraw only when what the screen shows has changed."""
    global last_frame
    tm = uk_time()
    synced_time = tm[0] >= MIN_PLAUSIBLE_YEAR
    night = is_night(tm)
    set_brightness(NIGHT_BRIGHTNESS if night else BRIGHTNESS)
    glow_tick(night)
    secs = (focus.remaining() + 999) // 1000
    flash_on = focus.flashing() and (mono_ms() // 500) % 2 == 0
    wx = current_weather()
    if screen == 'clock':
        frame = ('clock', synced_time, tm[3], tm[4])
    elif screen == 'date':
        frame = ('date', synced_time, tm[0], tm[1], tm[2])
    elif screen == 'timer':
        frame = ('timer', focus.state, secs, flash_on)
    else:
        frame = ('weather', wx)
    if frame == last_frame:
        return
    last_frame = frame

    timer_flash = screen == 'timer' and flash_on
    display.set_pen(teal if timer_flash else black)
    display.clear()
    if screen == 'clock':
        draw_clock(tm, synced_time)
    elif screen == 'date':
        draw_date(tm, synced_time)
    elif screen == 'timer':
        draw_timer(secs, timer_flash)
    else:
        draw_weather(wx)
    display.update()


# ---------------- crash guard ----------------
def log_crash(e):
    """Keep the latest unexpected error (overwritten, so it stays small)."""
    try:
        with open(CRASH_LOG, 'w') as f:
            t = uk_time()
            f.write('{:02d}/{:02d}/{} {:02d}:{:02d}:{:02d} UK, uptime {} s\n'.format(
                t[2], t[1], t[0], t[3], t[4], t[5], mono_ms() // 1000))
            sys.print_exception(e, f)
    except Exception:
        pass


# Everything below runs inside the guard. An unexpected error would
# otherwise leave the last frame frozen on the panel (a wrong time) until
# someone power-cycles the board, so log it and restart instead. Ctrl-C at
# the REPL is a KeyboardInterrupt (BaseException) and still gets through.
try:
    # ---------------- startup ----------------
    synced = False
    wifi_ok = connect_wifi(show=True)
    if wifi_ok:
        synced = ntp_set_rtc()

    last_retry = time.time()
    last_sync = time.time()
    last_net = mono_ms()
    wifi_up = wifi_ok
    # Count boot as quiet so the first network tick (weather) runs at once.
    last_input = mono_ms() - NET_QUIET_MS
    screen = 'clock'

    while True:
        for name, kind in poll_buttons():
            last_input = mono_ms()
            glow['since'] = None    # any press acknowledges a finished session
            if name == 'A':
                screen = 'clock'
            elif name == 'B':
                screen = 'date'
            elif name == 'X':
                # A hold resets only where you can see the timer: from
                # another screen it just shows it, so a long press cannot
                # wipe a running session unseen.
                if kind == 'hold':
                    if screen == 'timer':
                        focus.reset()
                elif screen == 'timer':
                    focus.toggle()
                screen = 'timer'
            elif name == 'Y':
                if screen == 'weather' and kind == 'press':
                    request_weather()
                screen = 'weather'

        if focus.update():
            screen = 'timer'
            last_input = mono_ms()
            glow['since'] = mono_ms()

        if (screen != 'clock'
                and not (screen == 'timer' and focus.state == 'running')
                and mono_ms() - last_input > IDLE_RETURN_MS):
            screen = 'clock'

        render(screen)

        if mono_ms() - last_net >= 1000 and mono_ms() - last_input >= NET_QUIET_MS:
            last_net = mono_ms()
            now = time.time()
            up = wifi_tick()
            if up and not wifi_up:
                last_retry = 0          # link just came back: sync now if needed
            wifi_up = up
            if up:
                if not synced:
                    if now - last_retry > RETRY_EVERY_S:
                        last_retry = now
                        synced = ntp_set_rtc()
                        last_sync = time.time()
                elif now - last_sync > RESYNC_EVERY_S:
                    if ntp_set_rtc():
                        last_sync = time.time()
                refresh_weather()

        time.sleep_ms(POLL_MS)

except Exception as e:
    log_crash(e)
    rgb_led.set_rgb(0, 0, 0)
    time.sleep(5)
    machine.reset()
