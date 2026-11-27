# USB time and weather fallback

Owner: Beltsazar; implementation/maintenance: Hermes. Canonical home: this repository and the installed `host/usb_clock.py` copy on bk-air. Runtime receipt budget: 16 KiB, one overwritten JSON file. Retire the Mac LaunchAgent when the Pico no longer uses that Mac's USB connection. Keep only one previous working board main file for rollback.

## Problem and decision

The Pico can run its timer locally but has no battery-backed RTC. A power loss resets the date to 2021. At the fixed campus location, CSEFB joins can fail before NTP or weather are reachable. The owner ruled out moving the device.

Use the cable already connecting the Pico to bk-air. A finite native macOS LaunchAgent sends the Mac's UTC time every 30 seconds. The Pico replies whether weather is due; only then does the helper fetch London weather over HTTPS and send a second packet. Network work happens on the Mac, not in the button/display loop. The Mac uses its existing internet connection. No Wi-Fi password, OAuth grant or credential is transferred.

Rejected alternatives:
- Moving hardware: explicitly excluded by the owner.
- More retries, larger timeouts or a speculative radio setting: direct joins, BSSID-pinned joins, a longer window and radio reinitialisation had not established a durable fix.
- A phone hotspot or new campus account: adds an owner-dependent network/credential change.
- Repeated REPL time-setting: interrupts the application and is not automatic.
- New cloud service or daemon: unnecessary; a finite launchd job and the existing USB console suffice.

## Contract

USB frames are single lines beginning `#PC1 ` followed by compact JSON. They are comments, not executable commands, if the board is at the REPL. A packet contains positive integer `id` and UTC Unix `utc`; optional `weather` is the six-element tuple used by the existing clock, with `weather_at` recording its age. The reply is `#PC1 OK id need_weather`, with the final value either 0 or 1. Invalid, oversized or stale weather data cannot renew the weather timestamp. The parser accepts at most 128 characters per 30 ms UI iteration and never waits for a complete frame.

A recent USB update suppresses on-board network work, keeping buttons responsive. If updates stop for 90 seconds, native Wi-Fi/NTP/weather retries resume. A time correction does not change the monotonic timer. USB time does not claim a successful NTP exchange on the Pico.

## Limits and acceptance

The Mac must be awake and the USB data link connected. On loss of the Mac internet, time still updates and the last weather reading remains bounded by the existing three-hour age limit. Without USB updates, the RTC continues while powered and native Wi-Fi remains a fallback. This does not assert that CSEFB itself has been repaired or cleared of security concerns.

Acceptance: original 32 host checks; USB cold boot with failed Wi-Fi; timer preservation during RTC correction; responsive button/display paths; malformed/fragmented/oversized input; automatic native Mac run with an acknowledged live packet; real weather on the board while its Wi-Fi is disconnected. Prove negative variants reject the old code, absent USB preference, unbounded parsing and malformed identifiers. Read back board hashes and native job receipts before reporting completion.
