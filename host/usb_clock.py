#!/usr/bin/python3
"""Finite, non-interrupting USB time/weather update for the Pico desk clock."""
import argparse
from contextlib import contextmanager
import fcntl
import glob
import json
import math
import os
from pathlib import Path
import plistlib
import re
import select
import signal
import subprocess
import sys
import tempfile
import termios
import time
import urllib.request

SERIAL = "04a212ce9def14d2"
WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude=51.5246&longitude=-0.1340"
    "&current=temperature_2m,weather_code,is_day&hourly=precipitation_probability"
    "&forecast_hours=5&daily=temperature_2m_max,temperature_2m_min"
    "&timezone=Europe%2FLondon&forecast_days=1")
DEFAULT_RECEIPT = "~/Library/Application Support/pico-clock/receipt.json"


def write_receipt(path, receipt):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=str(path.parent),
                                         prefix=".receipt-", delete=False) as handle:
            name = handle.name
            json.dump(receipt, handle, separators=(",", ":"), allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, str(path))
        name = None
    finally:
        if name is not None:
            os.unlink(name)


def discover_port():
    result = subprocess.run(["/usr/sbin/ioreg", "-p", "IOUSB", "-a", "-l"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            check=True, timeout=3)
    tree = plistlib.loads(result.stdout)
    if isinstance(tree, dict):
        tree = [tree]   # macOS ioreg may return its IORegistry root directly
    if not isinstance(tree, list):
        raise ClockError("invalid USB registry")
    count = 0
    pending = list(tree)
    while pending:
        node = pending.pop()
        if not isinstance(node, dict):
            raise ClockError("invalid USB registry node")
        if node.get("USB Serial Number") == SERIAL:
            count += 1
        pending.extend(node.get("IORegistryEntryChildren", []))
    if count == 0:
        return None
    if count != 1:
        raise ClockError("ambiguous USB board identity")
    ports = glob.glob("/dev/cu.usbmodem*")
    if len(ports) != 1:
        raise ClockError("ambiguous USB modem port")
    return ports[0]


class ClockError(Exception):
    """A fixed, credential-free operational error suitable for the receipt."""


@contextmanager
def serial_port(port):
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    exclusive = False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.ioctl(fd, termios.TIOCEXCL)
        exclusive = True
        attrs = termios.tcgetattr(fd)
        attrs[0] = 0
        attrs[1] = 0
        attrs[2] &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB | termios.HUPCL |
                      getattr(termios, "CRTSCTS", 0))
        attrs[2] |= termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] = 0
        attrs[4] = attrs[5] = termios.B115200
        attrs[6][termios.VMIN] = 0
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
        # Do not restore HUPCL: dropping modem control lines on close can reset boards.
        yield fd
    finally:
        try:
            if exclusive:
                fcntl.ioctl(fd, termios.TIOCNXCL)
        finally:
            os.close(fd)


def exchange(fd, packet):
    wire = ("#PC1 " + json.dumps(packet, separators=(",", ":"), allow_nan=False) + "\n").encode("ascii")
    if len(wire) > 512:
        raise ClockError("packet exceeds 512 bytes")
    deadline = time.monotonic() + 2
    sent = 0
    while sent < len(wire):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
            raise ClockError("USB write timed out")
        try:
            written = os.write(fd, wire[sent:])
        except BlockingIOError:
            continue
        if written <= 0:
            raise ClockError("USB disconnected")
        sent += written
    line = bytearray()
    overlong = False
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise ClockError("USB ACK timed out")
        try:
            data = os.read(fd, 512)
        except BlockingIOError:
            continue
        if not data:
            raise ClockError("USB disconnected")
        for char in data:
            if char == 10:
                match = None if overlong else re.fullmatch(rb"#PC1 OK ([0-9]+) ([01])\r?", line)
                if match and int(match[1]) == packet["id"]:
                    return match[2] == b"1"
                line.clear()
                overlong = False
            elif not overlong:
                line.append(char)
                if len(line) > 512:
                    line.clear()
                    overlong = True


def fetch_weather():
    def timed_out(signum, frame):
        raise ClockError("weather request timed out")

    previous_handler = signal.signal(signal.SIGALRM, timed_out)
    previous_timer = signal.setitimer(signal.ITIMER_REAL, 15)
    try:
        with urllib.request.urlopen(WEATHER_URL, timeout=15) as response:
            data = json.loads(response.read(65537))
        current, daily = data["current"], data["daily"]
        rain = [value for value in data.get("hourly", {}).get(
            "precipitation_probability", [])[1:5] if value is not None]
        temperatures = [current["temperature_2m"], daily["temperature_2m_max"][0],
                        daily["temperature_2m_min"][0]]
        code, day = current["weather_code"], current["is_day"]
        if any(type(value) not in (int, float) or not math.isfinite(value)
               for value in temperatures):
            raise ClockError("invalid weather temperatures")
        if type(code) is not int or not 0 <= code <= 99 or type(day) is not int or day not in (0, 1):
            raise ClockError("invalid weather code or day flag")
        if any(type(value) is not int or not 0 <= value <= 100 for value in rain):
            raise ClockError("invalid precipitation probability")
        temperature, high, low = [round(value) for value in temperatures]
        return [temperature, code, high, low, max(rain) if rain else None, day]
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0]:
            signal.setitimer(signal.ITIMER_REAL, *previous_timer)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", default=DEFAULT_RECEIPT)
    args = parser.parse_args(argv)
    receipt = {"timestamp": int(time.time()), "serial": SERIAL, "port": None,
               "time_ok": False, "weather_ok": False, "weather_error": None}
    status = 0
    try:
        receipt["port"] = discover_port()
        if receipt["port"] is not None:
            packet_id = int(time.time() * 1000)
            with serial_port(receipt["port"]) as fd:
                weather_needed = exchange(fd, {"id": packet_id, "utc": int(time.time())})
                receipt["time_ok"] = True
            if weather_needed:
                weather = fetch_weather()
                receipt["port"] = discover_port()
                if receipt["port"] is None:
                    raise ClockError("USB board disappeared before weather")
                now = int(time.time())
                with serial_port(receipt["port"]) as fd:
                    exchange(fd, {"id": packet_id + 1, "utc": now,
                                  "weather": weather, "weather_at": now})
                    receipt["weather_ok"] = True
    except Exception as exc:
        status = 1
        fallback = "weather update failed" if receipt["time_ok"] else "USB operation failed"
        message = str(exc) if isinstance(exc, ClockError) else fallback
        receipt["error"] = message
        if receipt["time_ok"]:
            receipt["weather_error"] = message
        print("pico-clock: " + message, file=sys.stderr)
    try:
        write_receipt(args.receipt, receipt)
    except OSError:
        print("pico-clock: receipt write failed", file=sys.stderr)
        return 1
    return status


if __name__ == "__main__":
    raise SystemExit(main())
