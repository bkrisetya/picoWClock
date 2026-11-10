# picoWClock - bare clock for Raspberry Pi Pico 2 W + Pimoroni Display Pack
# 1.14" (PIM543, 240x135). Fork of chriskalv/picoWClock (Display Pack 2.0),
# stripped to the clock itself and adapted for the small panel: no
# temperature, no language switching, no birthday wish, no buttons.
# UK time (GMT / BST) instead of CET. WiFi credentials live in
# WIFI_CONFIG.py (git-ignored; the owner fills it in).
#
# If WiFi or NTP is unreachable the clock keeps running from the RTC and
# retries in the background instead of hanging or rebooting.

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
NTP_HOST = 'pool.ntp.org'
RETRY_EVERY_S = 60
RESYNC_EVERY_S = 6 * 3600
MIN_PLAUSIBLE_YEAR = 2024

# ---------------- display ----------------
from picographics import PicoGraphics, DISPLAY_PICO_DISPLAY

display = PicoGraphics(display=DISPLAY_PICO_DISPLAY, rotate=0)
width, height = display.get_bounds()
display.set_backlight(BRIGHTNESS)
display.set_font('sans')

black = display.create_pen(0, 0, 0)
white = display.create_pen(255, 255, 255)
orange = display.create_pen(255, 128, 0)

led_onboard = machine.Pin('LED', machine.Pin.OUT, value=0)

MONTHS = (None, 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
          'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
DAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday',
        'Friday', 'Saturday', 'Sunday')

display.set_pen(black)
display.clear()
display.update()


# ---------------- WiFi ----------------
def wifi_connected():
    wlan = network.WLAN(network.STA_IF)
    return wlan.isconnected()


def connect_wifi(timeout_s=15):
    """Best-effort connect. Returns True on success."""
    try:
        import rp2
        rp2.country(WIFI_CONFIG.COUNTRY)
    except (ImportError, AttributeError, ValueError):
        pass
    wlan = network.WLAN(network.STA_IF)
    if wlan.isconnected():
        return True
    display.set_pen(black)
    display.clear()
    display.set_pen(white)
    display.text('Connecting WiFi...', 8, 56, width, 0.9)
    display.update()
    wlan.active(True)
    wlan.connect(WIFI_CONFIG.SSID, WIFI_CONFIG.PSK)
    for _ in range(timeout_s):
        if wlan.status() < 0 or wlan.status() >= 3:
            break
        led_onboard.toggle()
        time.sleep(1)
    led_onboard.off()
    return wlan.isconnected()


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


# ---------------- drawing ----------------
def fit_scale(text, max_w, start):
    scale = start
    while scale > 0.2 and display.measure_text(text, scale) > max_w:
        scale -= 0.05
    return scale


def draw_centred(text, y, scale, pen):
    display.set_pen(pen)
    x = max(0, (width - display.measure_text(text, scale)) // 2)
    display.text(text, x, y, width, scale)


def draw_clock():
    tm = uk_time()
    year, month, day, hour, minute, second, weekday, _ = tm
    if year < MIN_PLAUSIBLE_YEAR:
        display.set_pen(black)
        display.clear()
        draw_centred('--:--', 30, 2.6, white)
        draw_centred('waiting for time sync', 96, 0.6, orange)
    else:
        hhmm = '{:02d}:{:02d}'.format(hour, minute)
        date_str = '{} {} {}'.format(day, MONTHS[month], year)
        display.set_pen(black)
        display.clear()
        draw_centred(hhmm, 8, fit_scale(hhmm, 210, 3.0), white)
        draw_centred(DAYS[weekday], 86, fit_scale(DAYS[weekday], 220, 0.9), orange)
        draw_centred(date_str, 110, fit_scale(date_str, 220, 0.75), orange)
    display.update()


# ---------------- startup ----------------
synced = False
wifi_ok = connect_wifi()
if wifi_ok:
    synced = ntp_set_rtc()

last_retry = time.time()
last_sync = time.time()

while True:
    draw_clock()

    now = time.time()
    if not wifi_connected() and now - last_retry > RETRY_EVERY_S:
        last_retry = now
        led_onboard.toggle()
        if connect_wifi(8):
            synced = ntp_set_rtc()
            last_sync = time.time()
        led_onboard.off()
    elif wifi_connected() and synced and now - last_sync > RESYNC_EVERY_S:
        if ntp_set_rtc():
            last_sync = now

    time.sleep(1)
