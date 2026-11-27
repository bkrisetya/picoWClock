# Pico clock USB helper

Owner: pico-clock project. Canonical home: `host/` in this repository. Budget: 128 KiB of source, tests and fixture. Retire this helper when the desk clock no longer needs USB host time/weather updates. The generated receipt is one overwritten file, normally under 1 KiB.

`usb_clock.py` uses Python 3's standard library only. It is intended for macOS `/usr/bin/python3`, with a finite native launchd invocation, `StartInterval=30` and `RunAtLoad=true`. Do not use `KeepAlive`. Deployment is separate from these source files.

```sh
/usr/bin/python3 /absolute/path/to/pico-clock/host/usb_clock.py
```

Optional receipt override:

```sh
/usr/bin/python3 /absolute/path/to/pico-clock/host/usb_clock.py --receipt /absolute/path/to/receipt.json
```

Default: `~/Library/Application Support/pico-clock/receipt.json`. The helper creates the parent directory and atomically replaces the receipt. It never appends a log. Normal and absent-board runs are silent. Errors produce a short fixed message on stderr and exit 1, without exception details or credentials. Do not add accumulating launchd log files.

## Safety and protocol

- Discover the exact serial `04a212ce9def14d2` with `/usr/sbin/ioreg -p IOUSB -a -l`. Accept a dictionary or list plist root, both emitted by ioreg variants. Require exactly one matching registry node and exactly one `/dev/cu.usbmodem*` port. No matching board exits 0 without opening any port. Any board/port ambiguity refuses the update. This is deliberately conservative: another USB modem prevents updates, even if its identity is unrelated.
- Open nonblocking with `O_NOCTTY`, acquire nonblocking `flock` and `TIOCEXCL`, then configure raw 115200 8N1 with hardware/software flow control disabled. Clear `HUPCL` and leave it cleared, so close does not request a modem-line drop. Only exclusive-mode ioctls are used. No Ctrl-C, Ctrl-D, DTR ioctl, REPL takeover, reset or button interaction occurs.
- Existing diagnostics must honour the same lock or claim `TIOCEXCL`. Kernel exclusivity prevents subsequent non-privileged opens, but cannot detect an unrelated program that opened the port earlier without any lock. Do not run such diagnostics concurrently.
- Send only `#PC1 ` plus compact JSON plus newline, at most 512 bytes including framing. The first packet contains a positive integer `id` and Unix `utc`. Require the exact ACK line `#PC1 OK <same numeric id> <0 or 1>` within a two-second write/ACK deadline. Ignore startup noise, stale IDs and malformed lines without extending that deadline or growing an unbounded buffer.
- Close the port after the time ACK. Only if requested, fetch the configured London Open-Meteo HTTPS URL. Use TLS verification, a 15-second socket timeout and a 15-second wall-clock alarm covering DNS, connection and response reading. The response read is limited to 65,537 bytes. The alarm requires the normal CLI main thread.
- Weather is `[rounded_temperature, integer_weather_code, rounded_high, rounded_low, max_rain_or_null, is_day]`. Rain uses only slots 1 through 4 of `hourly.precipitation_probability`, ignoring null entries. An absent/all-null range becomes JSON `null`. Reject invalid required temperatures, codes, day flags and rain values rather than forwarding malformed data.
- Rediscover the board and port, reacquire exclusivity, send weather with a new ID and fresh `utc`/`weather_at`, verify its ACK, and close again. No serial handle is kept open during HTTPS. A fetch or second-ACK failure keeps `time_ok=true`, sets `weather_error`, and exits 1.
- The bounded I/O budget is two three-second discovery calls, two two-second exchanges and one 15-second fetch. Local filesystem/scheduling delays are not covered by that budget. The helper has no retry loop, scheduler or persistent background process.

## Receipt

Every normal invocation replaces one JSON receipt containing integer Unix `timestamp` (invocation start), `serial`, nullable `port`, `time_ok`, `weather_ok`, and nullable `weather_error`. A refusal/failure also contains `error`. `weather_ok=false` with `weather_error=null` means no weather update was requested or attempted, not necessarily a weather fault. Receipt write failure exits 1 with a bounded stderr message; the old receipt remains intact if replacement fails.

## Tests

```sh
cd /absolute/path/to/pico-clock/host
PYTHONDONTWRITEBYTECODE=1 /usr/bin/python3 -B -m unittest -v test_usb_clock
```

Tests call the real `main(argv)` entrypoint and execute the script's `__main__` guard. Only external discovery and HTTPS edges are faked. A pseudo-terminal peer exercises actual POSIX open, raw termios, exclusivity, reads/writes and ACKs. Temporary test files use the native `TMPDIR` and are removed afterwards. The timeout test waits a real 15 seconds; the full suite also includes real two-second ACK failures.

`open_meteo_sample.json` is a copy of the project's recorded real `tests/open_meteo_sample.json`, with a final newline. The unmodified fixture yields `[10,3,10,6,67,1]`. Null/range/schema tests deliberately vary that fixture; they are not presented as live weather.

TDD evidence: the initial absent-board test failed because the entrypoint did not exist. Further RED runs caught missing ambiguity refusal, absent time transmission, absent weather fetch, missing weather failure receipt, an escaping receipt-write exception and invalid weather schema acceptance. Each implementation slice was followed by a GREEN suite. Negative ACK, lock-conflict and real 15-second alarm tests additionally exercise the implemented refusal/deadline paths.

The parent verified macOS `/usr/bin/python3` 3.9.6 against the actual Pico, fixed and regression-tested its dictionary-root ioreg output, and read back live time/weather ACKs. Native launchd interval runs and automatic recovery after an idle-board restart were also verified. The installed program is `~/bin/pico-clock-usb.py`, with LaunchAgent `org.setyamarta.pico-clock-usb`. The test suite remains a POSIX simulation; it does not replace those hardware checks.
