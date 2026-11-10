# picoWClock (PIM543 variant)

Desk clock on a Raspberry Pi Pico 2 W + Pimoroni Display Pack 1.14"
(PIM543, 240x135), portrait, one screen per button:

White with a teal accent throughout.

![Clock, date, focus timer and weather screens, captured from the board's framebuffer](docs/screens.png)

- **A** clock (default): hours white on top, minutes teal below, each
  scaled to fill its half of the panel.
- **B** date: weekday, day number, month.
- **X** focus timer, 25 minutes: press X again to start or pause, hold X
  for a second to reset. At zero it switches itself to the timer screen
  and flashes teal for 6 s, then reads DONE. The RGB LED stays off.
- **Y** London weather from Open-Meteo (free, no key): current
  temperature large (e.g. `10°C`), the condition in words (Sunny,
  Partly cloudy, Rain, ...; Clear at night), a divider, then High, Low
  and Rain chance as aligned rows. Refreshes every 15 minutes; press Y
  again to refresh now. A reading older than 3 hours shows `No data`.

Any screen other than the clock returns to it after 20 s without a press,
except while the timer is running. Time comes from NTP with UK GMT/BST
handling.

Forked from chriskalv/picoWClock (Display Pack 2.0, used as the NTP, DST
and layout starting point) and adapted by Beltsazar Krisetya,
November 2026. Divergence from upstream:

- PicoGraphics `DISPLAY_PICO_DISPLAY` (240x135) rotated 270 degrees, hold
  the board with USB at the top. Font is `bitmap8` at integer scales: the
  pico2_w Pimoroni build ships no vector fonts.
- WiFi credentials moved out of `main.py` into `WIFI_CONFIG.py`
  (git-ignored; copy `WIFI_CONFIG.py.example` and fill it in). It holds a
  `NETWORKS` list; the clock scans and joins whichever one is in range, so
  one config works at home and at work.
- RTC stays in UTC; BST is applied at render time (last Sunday March
  01:00 UTC to last Sunday October 01:00 UTC).
- The board has no battery-backed clock: after any power loss it shows
  `-- / --` until it reaches a known network and NTP.
- Failed WiFi/NTP no longer demands a reboot: background retries run every
  minute without blanking the face, and NTP resyncs every 6 hours.
- The pack's RGB LED is driven off at boot (unconfigured, the GPIO
  pull-downs make the red die glow).

## Test on the host

```sh
python3 tests/host_sim.py
```

Runs the real `main.py` under CPython against stubbed board modules
(buttons, display, WiFi, NTP, and the recorded Open-Meteo response in
`tests/open_meteo_sample.json`) and walks every screen and timer path,
with the tick counter crossing its wrap. Text widths come from the
board's own font metrics (`tests/bitmap8_widths.json`), so layout fits are
checked against real pixels. Run it before every deploy.

## Deploy

```sh
TTY=$(readlink -f /dev/serial/by-id/usb-MicroPython_Board_in_FS_mode_*-if00)
mpremote connect "$TTY" cp main.py :/main.py
mpremote connect "$TTY" cp WIFI_CONFIG.py :/WIFI_CONFIG.py
mpremote connect "$TTY" reset
```

Resolve the port through `/dev/serial/by-id` each time: a hard reset
re-enumerates USB and the ttyACM number can change.

First flash of Pimoroni MicroPython (pico2_w UF2) can be done with no
BOOTSEL press from a running REPL via `mpremote exec "import machine;
machine.bootloader()"`. Canonical copy of this project:
`~/work/pico-clock` on sm-pi.
