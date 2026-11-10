# picoWClock (PIM543 variant)

Bare desk clock on a Raspberry Pi Pico 2 W + Pimoroni Display Pack 1.14"
(PIM543, 240x135). Shows HH:MM, weekday and date, keeps time via NTP with
UK GMT/BST handling. Nothing else: no temperature, no language switching,
no birthday wish, no buttons.

Forked from chriskalv/picoWClock (Display Pack 2.0, used as the NTP, DST
and layout starting point) and adapted by Beltsazar Krisetya,
November 2026. Divergence from upstream:

- PicoGraphics `DISPLAY_PICO_DISPLAY` (240x135) instead of Pack 2.0,
  vector 'sans' font with fitted text centring.
- WiFi credentials moved out of `main.py` into `WIFI_CONFIG.py`
  (git-ignored; copy `WIFI_CONFIG.py.example` and fill it in).
- RTC stays in UTC; BST is applied at render time (last Sunday March
  01:00 UTC to last Sunday October 01:00 UTC).
- Failed WiFi/NTP no longer demands a reboot: the clock shows
  `--:-- / waiting for time sync`, retries WiFi every minute and resyncs
  NTP every 6 hours.

## Deploy

```sh
mpremote connect /dev/ttyACM1 cp main.py :/main.py
mpremote connect /dev/ttyACM1 cp WIFI_CONFIG.py :/WIFI_CONFIG.py
mpremote connect /dev/ttyACM1 reset
```

First flash of Pimoroni MicroPython (pico2_w UF2) can be done with no
BOOTSEL press from a running REPL via `mpremote exec "import machine;
machine.bootloader()"`. Canonical copy of this project:
`~/work/pico-clock` on sm-pi.
