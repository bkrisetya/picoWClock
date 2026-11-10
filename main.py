# picoWClock - desk clock for Raspberry Pi Pico 2 W + Pimoroni Display Pack
# 1.14" (PIM543, 240x135), portrait. Fork of chriskalv/picoWClock (Display
# Pack 2.0), rebuilt for the small panel with one screen per button:
#   A  clock (default): hours white on top, minutes teal below
#   B  date: weekday, day number, month
#   X  focus timer, 25 minutes: X again starts or pauses, hold X to reset
#   Y  London weather from Open-Meteo (no key): Y again refreshes
# Other screens fall back to the clock after 20 s without a press, except
# while the timer runs. UK time (GMT / BST). WiFi networks live in
# WIFI_CONFIG.py (git-ignored; the owner fills it in).
#
# If WiFi or NTP is unreachable the clock keeps running from the RTC and
# retries in the background instead of hanging or rebooting.

import gc
import machine
import network
import utime as time
import usocket as socket
import ustruct as struct

import WIFI_CONFIG

gc.enable()

BRIGHTNESS = 0.55
NTP_HOST = 'pool.ntp.org'
RETRY_EVERY_S = 60
RESYNC_EVERY_S = 6 * 3600
MIN_PLAUSIBLE_YEAR = 2024

POLL_MS = 30
HOLD_MS = 1000
IDLE_RETURN_MS = 20 * 1000
FOCUS_MS = 25 * 60 * 1000
DONE_FLASH_MS = 6 * 1000

WEATHER_URL = ('https://api.open-meteo.com/v1/forecast'
               '?latitude=51.5246&longitude=-0.1340'
               '&current=temperature_2m,weather_code,is_day'
               '&daily=temperature_2m_max,temperature_2m_min,'
               'precipitation_probability_max'
               '&timezone=Europe%2FLondon&forecast_days=1')
WEATHER_EVERY_MS = 15 * 60 * 1000
WEATHER_RETRY_MS = 2 * 60 * 1000
WEATHER_MAX_AGE_MS = 3 * 3600 * 1000

DAYS = ('MONDAY', 'TUESDAY', 'WEDNESDAY', 'THURSDAY',
        'FRIDAY', 'SATURDAY', 'SUNDAY')
MONTHS = (None, 'JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN',
          'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC')

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
    nets = known_networks()
    try:
        visible = [s[0].decode('utf-8', 'replace') for s in wlan.scan()]
        nets = [n for n in nets if n[0] in visible]
    except OSError:
        pass  # scan failed: try every known network
    for ssid, psk in nets:
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


# ---------------- NTP ----------------
def ntp_set_rtc():
    """Query NTP over UDP and set the RTC to UTC. Returns True on success."""
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
    seconds = struct.unpack('!I', msg[40:44])[0] - 2208988800
    tm = time.gmtime(seconds)
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


# ---------------- buttons ----------------
BUTTONS = [(name, machine.Pin(pin, machine.Pin.IN, machine.Pin.PULL_UP))
           for name, pin in (('A', 12), ('B', 13), ('X', 14), ('Y', 15))]
DEBOUNCE_MS = 80
pressed_at = {}
hold_fired = {}
released_at = {}

# The loop only samples the pins between redraws and network calls, so a
# quick tap can start and end unseen. A falling-edge interrupt latches it.
# fall_edges counts every electrical press per button (contact bounce
# included); read it over the REPL to tell a dead switch from a missed tap.
fell_at = {}
fall_edges = {'A': 0, 'B': 0, 'X': 0, 'Y': 0}


def _fell(name):
    fell_at[name] = time.ticks_ms()
    fall_edges[name] += 1


for _name, _pin in BUTTONS:
    _pin.irq(trigger=machine.Pin.IRQ_FALLING,
             handler=lambda _p, n=_name: _fell(n))


def poll_buttons():
    """Return (name, 'press' | 'hold') events. A press fires on release,
    a hold fires once after HOLD_MS while still down. A tap that began and
    ended between two polls is recovered from the edge latch, unless it is
    contact bounce within DEBOUNCE_MS of the previous release."""
    events = []
    now = time.ticks_ms()
    for name, pin in BUTTONS:
        fell = fell_at.pop(name, None)
        if pin.value() == 0:
            if name not in pressed_at:
                pressed_at[name] = now
                hold_fired[name] = False
            elif not hold_fired[name] and time.ticks_diff(now, pressed_at[name]) >= HOLD_MS:
                hold_fired[name] = True
                events.append((name, 'hold'))
        elif name in pressed_at:
            held = time.ticks_diff(now, pressed_at.pop(name))
            released_at[name] = now
            if not hold_fired.pop(name) and held >= 20:
                events.append((name, 'press'))
        elif fell is not None:
            last = released_at.get(name)
            if last is None or time.ticks_diff(fell, last) > DEBOUNCE_MS:
                released_at[name] = now
                events.append((name, 'press'))
    return events


# ---------------- focus timer ----------------
class FocusTimer:
    """25-minute countdown on the monotonic tick clock, so an NTP step of
    the RTC never disturbs it."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.state = 'ready'   # ready | running | paused | done
        self.left = FOCUS_MS
        self.end = 0
        self.done_at = 0

    def remaining(self):
        if self.state == 'running':
            return max(0, time.ticks_diff(self.end, time.ticks_ms()))
        return self.left

    def toggle(self):
        if self.state in ('ready', 'paused'):
            self.end = time.ticks_add(time.ticks_ms(), self.left)
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
            self.done_at = time.ticks_ms()
            return True
        return False

    def flashing(self):
        return (self.state == 'done'
                and time.ticks_diff(time.ticks_ms(), self.done_at) < DONE_FLASH_MS)


focus = FocusTimer()


# ---------------- weather ----------------
# WMO weather codes, as (highest code in band, name). 0 and 1 depend on
# day or night and are handled in condition_name().
CONDITIONS = ((2, 'Partly cloudy'), (3, 'Cloudy'), (48, 'Fog'),
              (57, 'Drizzle'), (67, 'Rain'), (77, 'Snow'), (82, 'Showers'),
              (86, 'Snow showers'), (99, 'Thunderstorm'))

WEATHER_FAILS_BEFORE_RECONNECT = 3

weather = {'data': None, 'at': 0, 'due': time.ticks_ms(), 'fails': 0}


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
    """(temp, code, high, low, rain_pct, is_day) for London, or None."""
    try:
        import urequests
        gc.collect()
        r = urequests.get(WEATHER_URL, timeout=10)
        try:
            if r.status_code != 200:
                return None
            d = r.json()
        finally:
            r.close()
        cur = d['current']
        day = d['daily']
        rain = day['precipitation_probability_max'][0]
        return (round(cur['temperature_2m']), int(cur['weather_code']),
                round(day['temperature_2m_max'][0]),
                round(day['temperature_2m_min'][0]),
                None if rain is None else int(rain),
                int(cur.get('is_day', 1)))
    except Exception:
        return None


def refresh_weather(force=False):
    now = time.ticks_ms()
    if not force and time.ticks_diff(now, weather['due']) < 0:
        return
    if not wifi_connected():
        return
    data = fetch_weather()
    now = time.ticks_ms()
    if data:
        weather['data'] = data
        weather['at'] = now
        weather['due'] = time.ticks_add(now, WEATHER_EVERY_MS)
        weather['fails'] = 0
    else:
        weather['due'] = time.ticks_add(now, WEATHER_RETRY_MS)
        weather['fails'] += 1
        # A link can report connected while passing no TCP (seen live).
        # Drop it so the main loop's retry makes a clean reconnect.
        if weather['fails'] >= WEATHER_FAILS_BEFORE_RECONNECT:
            weather['fails'] = 0
            network.WLAN(network.STA_IF).disconnect()


def current_weather():
    if weather['data'] is None:
        return None
    if time.ticks_diff(time.ticks_ms(), weather['at']) > WEATHER_MAX_AGE_MS:
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
    """Two equal-size rows splitting y0..y1 (clock and timer digits)."""
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


def draw_timer(secs, fg, accent):
    label = {'ready': 'READY', 'paused': 'PAUSED', 'done': 'DONE'}.get(focus.state, '')
    draw_band('FOCUS', 0, 32, accent, band_scale('FOCUS', 0, 32, 3))
    draw_pair('{:02d}'.format(secs // 60), '{:02d}'.format(secs % 60),
              32, 212, fg, accent)
    if label:
        draw_band(label, 212, height, fg, band_scale(label, 212, height, 3))


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
            ('Rain', '--' if rain is None else '{}%'.format(rain)))

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
    secs = (focus.remaining() + 999) // 1000
    flash_on = focus.flashing() and (time.ticks_ms() // 500) % 2 == 0
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
        if timer_flash:
            draw_timer(secs, black, black)
        else:
            draw_timer(secs, white, teal)
    else:
        draw_weather(wx)
    display.update()


# ---------------- startup ----------------
synced = False
wifi_ok = connect_wifi(show=True)
if wifi_ok:
    synced = ntp_set_rtc()

last_retry = time.time()
last_sync = time.time()
last_net = time.ticks_ms()
last_input = time.ticks_ms()
screen = 'clock'

while True:
    for name, kind in poll_buttons():
        last_input = time.ticks_ms()
        if name == 'A':
            screen = 'clock'
        elif name == 'B':
            screen = 'date'
        elif name == 'X':
            if kind == 'hold':
                focus.reset()
            elif screen == 'timer':
                focus.toggle()
            screen = 'timer'
        elif name == 'Y':
            if screen == 'weather' and kind == 'press':
                refresh_weather(force=True)
            screen = 'weather'

    if focus.update():
        screen = 'timer'
        last_input = time.ticks_ms()

    if (screen != 'clock'
            and not (screen == 'timer' and focus.state == 'running')
            and time.ticks_diff(time.ticks_ms(), last_input) > IDLE_RETURN_MS):
        screen = 'clock'

    render(screen)

    if time.ticks_diff(time.ticks_ms(), last_net) >= 1000:
        last_net = time.ticks_ms()
        now = time.time()
        if not wifi_connected():
            if now - last_retry > RETRY_EVERY_S:
                last_retry = now
                if connect_wifi(8):
                    synced = ntp_set_rtc()
                    last_sync = time.time()
        elif not synced:
            if now - last_retry > RETRY_EVERY_S:
                last_retry = now
                synced = ntp_set_rtc()
                last_sync = time.time()
        elif now - last_sync > RESYNC_EVERY_S:
            if ntp_set_rtc():
                last_sync = time.time()
        refresh_weather()

    time.sleep_ms(POLL_MS)
