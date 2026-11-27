# USB fallback acceptance

27 November 2026. Owner: pico-clock project. Budget: 16 KiB for this receipt; retire or replace when the USB architecture changes. Canonical source and decision: `../plans/usb-fallback.md`.

## Installed and exercised

- Pico `main.py`: `ee533b3bf4b4650a5f4b3c580ae50f63ad60e60225a26460498d539cd174946d`.
- Pico `usb_host.py`: `d495e42dfb0913b11ed78fe044222b26e04755ceca52edd3912af723c0e664f9`.
- bk-air helper `~/bin/pico-clock-usb.py`: `a342e60eb88eeb7e04cc06131bf0be217683f78942f40b7fff9b1b83cd542165`.
- Native `gui/501/org.setyamarta.pico-clock-usb`, RunAtLoad and 30-second interval, no KeepAlive, no additional packages.

The Pico received real London weather `[3,3,4,2,10,1]` and correct UTC through USB while its Wi-Fi was disconnected. The normal weather renderer was captured and inspected without clipping; the owner's clock screen and ready timer were restored.

A native interval advanced the receipt timestamp from `1795781485` to `1795781575`, with `time_ok=true` and no error. After an idle-board `machine.reset()`, the next successful native interval recorded timestamp `1795781630`, `time_ok=true` and `weather_ok=true`. A short serial re-enumeration failure recovered on the next interval without intervention. Board read-back showed fresh uptime 76,520 ms, reset cause 3, correct UTC 12:14:35, recent USB input and the expected persisted file hashes.

CSEFB also rejoined during this restart. That does not establish why earlier associations failed, nor clear the access points of compromise. USB operation was independently proved with the Pico link down, so time/weather no longer depend on CSEFB while the Mac is awake and connected.

## Regression gates

- Original host simulation: 32 checks passed.
- Real-clock USB simulation: cold 2021 RTC, failed Wi-Fi, weather screen, buttons, timer during a one-hour RTC correction, USB preference and native fallback after USB stops.
- Parser: malformed/fragmented/oversized input, bounded 128-character work, stale weather and truthful NTP status.
- Negative variants failed as intended: old main, absent USB preference, unbounded input and boolean packet ID acceptance.
- Host helper: 15 tests, including the Mac dictionary-root registry regression, real pseudo-terminal serial/ACK paths and bounded weather failure.

Only one previous good board `main_previous.py` is retained for rollback. Runtime receipt is overwritten rather than appended; temporary target-side installation and diagnostic stages are removed at close. No Wi-Fi credentials were changed or transferred.
