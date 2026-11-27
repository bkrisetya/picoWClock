# On-board USB activity history

Owner: Beltsazar Krisetya. Canonical project: `~/work/pico-clock`.

## Decision

Record only timestamps and up/down state for the Pico's own USB link. Do not identify computers, people or sibling peripherals; do not inspect their traffic. Keep the display, timer, Wi-Fi time/weather path and existing USB descriptors unchanged. No host service, remote receiver or alert schedule is added.

Use a read-only RP2350 USB `SIE_STATUS` sample at `0x50110050`: `CONNECTED` bit 16 must be set and `SUSPENDED` bit 4 clear for `up`. MicroPython's `USBDevice.active()` is unsuitable because it describes local USB activation, not host presence. Avoid USB callbacks and descriptor reconfiguration because they could disturb the serial recovery path. Avoid custom firmware when the installed firmware exposes `machine.mem32`.

## Behaviour and limits

- Poll at most four times per second, confirm a state for one second, timestamp its first observation. Do not log a short reset as another connection.
- Append `start`, `up`, `down`, and a first `time_anchor` when the RTC becomes plausible after an unsynchronised boot. Keep monotonic milliseconds and a separate run number. Unknown wall time stays null, never 2021. A later anchor can date earlier events in that same run, but never reconstruct time while the board was off.
- Two rolling JSONL files, at most 16,384 bytes each. Append only events, not a periodic flash heartbeat. Newline boundaries isolate a torn final record. Rotation replaces only the previous history file.
- If reading or writing fails, disable history for that run and retain a short error available over the diagnostic REPL. Never alter the face, light an LED or reboot for a history error.
- No identifier, USB payload, Wi-Fi name, password or network address enters the history.

`up` means USB activity available at this port, not physical dock occupancy. Sleep, selective suspend, dock faults and power loss can look like disconnection. A power cut cannot be logged at the moment it occurs; a new run marks the observation gap. Existing synchronous weather/NTP work can delay observations. This is a convenience record, not an access-control or forensic audit.

## Verification and retirement

Run `python3 -I tests/docklog_sim.py` and `python3 -I tests/host_sim.py`. Prove broken variants fail. On the board, test isolated log files and rotation, install atomically without resetting, compare code hashes, read back the actual current `up` event and normal clock state. Physical laptop detachment remains a separate acceptance check; do not label synthetic USB status tests as a real unplug test.

History belongs to the Pico application and is bounded to 32 KiB. Retire it by removing the history integration and `docklog.py`; retain or delete the two history files only as explicitly requested. Temporary install/test files retire after successful live readback, with the previous main retained only through that verification. The Git base provides the durable rollback.

Sources:
- https://docs.micropython.org/en/latest/library/machine.USBDevice.html
- https://raw.githubusercontent.com/raspberrypi/pico-sdk/master/src/rp2350/hardware_regs/include/hardware/regs/usb.h
- https://raw.githubusercontent.com/raspberrypi/pico-sdk/master/src/rp2350/hardware_regs/include/hardware/regs/addressmap.h
