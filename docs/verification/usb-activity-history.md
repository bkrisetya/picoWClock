# USB activity recorder verification

27 November 2026. Owner: Beltsazar Krisetya. Receipt belongs to the Pico clock
project; budget 8 KiB, retire with this feature after its replacement is verified.

## Installed and read back

Board serial `04a212ce9def14d2`, Pico 2 W / RP2350, Pimoroni MicroPython
`pico2_w v1.29.0-2`. Deployment used the existing USB path through bk-air only
for programming and resumed the existing main loop without a reset or startup
replay. No host updater, notifier, daemon or schedule was installed.

- `main.py` SHA-256: `46495fb8f531629a45a5c5f639914f82d0e870dd046d7e935c2690d238669238`.
- `docklog.py` SHA-256: `b670c1f9b2e795f132423b94baaa0b4e980d340ea2a60b6faa28434ef13292cb`.
- Actual initial events: `start` and `up`, both run 1, monotonic
  `6497068` ms, UTC `2026-11-27T14:01:35Z`.
- Readback at `2026-11-27T14:02:24Z`: RTC advancing, focus `ready` with
  `1500000` ms remaining, screen `clock`, Wi-Fi and native time sync true.
  Logger `last=True`, both error fields null. Register `0x40051005`.
- The actual `dock-history.jsonl` was 165 bytes. This contains real observations,
  not the synthetic self-test events.

## Tests

- Six recorder checks passed, exercising real disposable host files: up/down,
  bounded rotation and distinct restarts, unknown time and later anchoring,
  safe failure, a torn last record, debounce and a long monotonic run.
- 34 full-clock simulation checks passed, including all prior screens, buttons,
  timer, weather and time behaviour, plus recording through the real main loop
  and continued button operation after an injected flash failure.
- Five deliberately broken variants were rejected: ignoring suspend, disabled
  rotation, missing time anchor, escaping flash errors, and no torn-tail boundary.
- Separate on-board test files exercised 30 synthetic transitions, rotation
  replacing an existing previous file, per-file budgets and distinct run numbers
  using the actual MicroPython filesystem and JSON library. They were removed
  before the real recorder was started.

## Evidence boundary

No real physical laptop unplug/replug test was performed. Sleep, selective
suspend, dock resets and power loss are not distinguishable from physical
attachment from this USB endpoint alone. A sudden power cut has no exact final
event; times while the board was off remain unknown. The log is local and is
retrieved over temporary USB access, not automatically sent to Telegram.

Target-side diagnostic staging and test files were removed. The temporary
previous main was checksum-checked and removed after successful state/hash
readback. The final read at `2026-11-27T14:04:21Z` confirmed clock/ready timer,
native Wi-Fi/time sync, active USB history and no logger errors. The Git base
remains the durable rollback. Local scratch captures follow the native 24-hour
idle expiry rather than adding a cleanup schedule.
