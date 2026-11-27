"""Bounded, non-blocking USB time/weather input. Frames are REPL-safe comments."""
import json
import machine
import select
import sys
import utime as time

PREFIX = '#PC1 '
MAX_LINE = 512
RECENT_MS = 90 * 1000
WEATHER_AGE_S = 3 * 3600
WEATHER_EVERY_MS = 15 * 60 * 1000


class USBHost:
    def __init__(self):
        self.stream = sys.stdin
        self.out = sys.stdout
        self.buffer = ''
        self.discard = False
        self.at = None
        try:
            self.poller = select.poll()
            self.poller.register(self.stream, select.POLLIN)
        except Exception:
            self.poller = None

    def recent(self, now):
        return self.at is not None and now - self.at < RECENT_MS

    def apply(self, line, weather, now):
        if not line.startswith(PREFIX):
            return
        try:
            packet = json.loads(line[len(PREFIX):])
            ident, utc = packet['id'], packet['utc']
            if type(ident) is not int or ident <= 0 or type(utc) is not int:
                return
            if not 1704067200 <= utc < 4102444800:   # UTC years 2024-2099
                return
            data = packet.get('weather')
            age = 0
            if data is not None:
                if not isinstance(data, list) or len(data) != 6:
                    return
                for value, low, high in zip(data, (-100, 0, -100, -100, 0, 0),
                                            (100, 99, 100, 100, 100, 1)):
                    if value is None and low == 0 and high == 100:
                        continue   # absent rain probability
                    if type(value) is not int or not low <= value <= high:
                        return
                at = packet['weather_at']
                if type(at) is not int:
                    return
                age = utc - at
                if age < -60:
                    return
            t = time.gmtime(utc)
            machine.RTC().datetime((t[0], t[1], t[2], t[6], t[3], t[4], t[5], 0))
            self.at = now
            if data is not None and age <= WEATHER_AGE_S:
                age_ms = max(0, age) * 1000
                weather.update(data=tuple(data), at=now - age_ms,
                               due=now + max(0, WEATHER_EVERY_MS - age_ms), fails=0)
            need = weather['data'] is None or now >= weather['due']
            self.out.write('#PC1 OK {} {}\n'.format(ident, int(need)))
        except (ValueError, KeyError, TypeError, OSError, OverflowError):
            return   # malformed/unplugged input must not stop the display

    def tick(self, weather, now):
        if self.poller is None:
            return
        try:
            for _ in range(128):   # at most 128 characters per 30 ms UI pass
                if not self.poller.poll(0):
                    break
                char = self.stream.read(1)
                if not char:
                    break
                if char == '\n':
                    if not self.discard:
                        self.apply(self.buffer.rstrip('\r'), weather, now)
                    self.buffer = ''
                    self.discard = False
                elif not self.discard:
                    if len(self.buffer) >= MAX_LINE:
                        self.buffer = ''
                        self.discard = True
                    else:
                        self.buffer += char
        except (OSError, ValueError):
            self.buffer = ''
