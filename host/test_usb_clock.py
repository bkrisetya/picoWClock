"""Stdlib tests: the real CLI entrypoint, fake external discovery/API edges."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import pty
import runpy
import select
import signal
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from unittest import mock

HOST = Path(__file__).resolve().parent
SCRIPT = HOST / "usb_clock.py"
SERIAL = "04a212ce9def14d2"


class HelperTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "USB helper entrypoint is missing")
        spec = importlib.util.spec_from_file_location("usb_clock", str(SCRIPT))
        assert spec is not None and spec.loader is not None
        self.helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.helper)
        self.tmp = tempfile.TemporaryDirectory(prefix="pico-usb-test-")
        self.addCleanup(self.tmp.cleanup)
        self.receipt = Path(self.tmp.name) / "receipt.json"

    def invoke(self, tree, ports=()):
        result = subprocess.CompletedProcess([], 0, plistlib.dumps(tree), b"")
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(self.helper.subprocess, "run", return_value=result) as ioreg, \
                mock.patch.object(self.helper.glob, "glob", return_value=list(ports)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = self.helper.main(["--receipt", str(self.receipt)])
        self.assertEqual(ioreg.call_args.args[0],
                         ["/usr/sbin/ioreg", "-p", "IOUSB", "-a", "-l"])
        self.assertLessEqual(ioreg.call_args.kwargs["timeout"], 5)
        return status, out.getvalue(), err.getvalue()

    def read_receipt(self):
        return json.loads(self.receipt.read_text())

    @contextlib.contextmanager
    def board(self, reply):
        master, slave = pty.openpty()
        port = os.ttyname(slave)
        stop = threading.Event()
        packets, flags, failures = [], [], []

        def serve():
            pending = b""
            try:
                while not stop.is_set():
                    if not select.select([master], [], [], 0.02)[0]:
                        continue
                    pending += os.read(master, 1024)
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        self.assertTrue(line.startswith(b"#PC1 "), repr(line))
                        self.assertLessEqual(len(line) + 1, 512)
                        self.assertNotIn(b"\x03", line)
                        self.assertNotIn(b"\x04", line)
                        packet = json.loads(line[5:])
                        packets.append(packet)
                        flags.append(termios.tcgetattr(slave))
                        response = reply(packet, len(packets))
                        if response:
                            os.write(master, response)
            except BaseException as exc:
                failures.append(exc)

        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        try:
            yield port, packets, flags
        finally:
            stop.set()
            worker.join(3)
            os.close(slave)
            os.close(master)
            self.assertFalse(worker.is_alive(), "fake board did not stop")
            if failures:
                raise failures[0]

    def test_macos_dictionary_registry_root(self):
        def reply(packet, number):
            return ("#PC1 OK %d 0\n" % packet["id"]).encode()
        with self.board(reply) as (port, packets, flags):
            status, out, err = self.invoke({"IORegistryEntryChildren": [
                {"USB Serial Number": SERIAL}]}, [port])
            self.assertEqual((status, out, err), (0, "", ""))
            self.assertTrue(self.read_receipt()["time_ok"])
            self.assertEqual(len(packets), 1)

    def test_time_only_ack_updates_time_without_network(self):
        def reply(packet, number):
            self.assertEqual(number, 1)
            self.assertEqual(set(packet), {"id", "utc"})
            self.assertIs(type(packet["id"]), int)
            self.assertGreater(packet["id"], 0)
            self.assertLess(abs(packet["utc"] - time.time()), 2)
            return ("boot noise\r\n#PC1 OK %d 0\r\n" % packet["id"]).encode()

        real_ioctl = self.helper.fcntl.ioctl
        with self.board(reply) as (port, packets, flags), \
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("unexpected network")), \
                mock.patch.object(self.helper.fcntl, "ioctl", side_effect=real_ioctl) as ioctl:
            status, out, err = self.invoke([{"IORegistryEntryChildren": [
                {"USB Serial Number": SERIAL}]}], [port])
            self.assertEqual((status, out, err), (0, "", ""))
            self.assertEqual(len(packets), 1)
            self.assertEqual([call.args[1] for call in ioctl.call_args_list],
                             [termios.TIOCEXCL, termios.TIOCNXCL])
            raw = flags[0]
            self.assertEqual(raw[0], 0)
            self.assertEqual(raw[1], 0)
            self.assertEqual(raw[3], 0)
            self.assertFalse(raw[2] & termios.HUPCL)
            receipt = self.read_receipt()
            self.assertTrue(receipt["time_ok"])
            self.assertFalse(receipt["weather_ok"])
            self.assertIsNone(receipt["weather_error"])
            self.assertEqual(receipt["port"], port)

    def test_weather_needed_fetches_real_fixture_after_time_ack(self):
        expected_url = (
            "https://api.open-meteo.com/v1/forecast?latitude=51.5246&longitude=-0.1340"
            "&current=temperature_2m,weather_code,is_day&hourly=precipitation_probability"
            "&forecast_hours=5&daily=temperature_2m_max,temperature_2m_min"
            "&timezone=Europe%2FLondon&forecast_days=1")

        def reply(packet, number):
            return ("#PC1 OK %d %d\n" % (packet["id"], number == 1)).encode()

        with self.board(reply) as (port, packets, flags):
            def fetch(url, timeout):
                self.assertEqual(url, expected_url)
                self.assertEqual(timeout, 15)
                self.assertEqual(len(packets), 1, "time packet must precede HTTPS")
                remaining = signal.getitimer(signal.ITIMER_REAL)[0]
                self.assertGreater(remaining, 0, "HTTPS needs a whole-operation deadline")
                self.assertLessEqual(remaining, 15)
                # The port must be closed during a potentially slow network request.
                self.helper.fcntl.flock(flags_fd[0], self.helper.fcntl.LOCK_EX |
                                        self.helper.fcntl.LOCK_NB)
                self.helper.fcntl.flock(flags_fd[0], self.helper.fcntl.LOCK_UN)
                return io.BytesIO((HOST / "open_meteo_sample.json").read_bytes())

            # A separate open description can test the helper's lock state.
            flags_fd = [os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)]
            try:
                with mock.patch("urllib.request.urlopen", side_effect=fetch) as request:
                    status, out, err = self.invoke([{"USB Serial Number": SERIAL}], [port])
            finally:
                os.close(flags_fd[0])
            self.assertEqual((status, out, err), (0, "", ""))
            self.assertEqual(request.call_count, 1)
            self.assertEqual(len(packets), 2)
            first, second = packets
            self.assertNotEqual(first["id"], second["id"])
            self.assertGreater(second["id"], 0)
            self.assertEqual(set(second), {"id", "utc", "weather", "weather_at"})
            self.assertEqual(second["weather"], [10, 3, 10, 6, 67, 1])
            self.assertLess(abs(second["utc"] - time.time()), 2)
            self.assertLess(abs(second["weather_at"] - time.time()), 2)
            self.assertTrue(self.read_receipt()["time_ok"])
            self.assertTrue(self.read_receipt()["weather_ok"])
            self.assertIsNone(self.read_receipt()["weather_error"])
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)

    def test_fetch_failure_preserves_time_ack_and_hides_exception_details(self):
        def reply(packet, number):
            return ("#PC1 OK %d 1\n" % packet["id"]).encode()

        with self.board(reply) as (port, packets, flags), \
                mock.patch("urllib.request.urlopen", side_effect=ValueError("private-credential")):
            status, out, err = self.invoke([{"USB Serial Number": SERIAL}], [port])
            self.assertEqual(status, 1)
            self.assertEqual(out, "")
            self.assertEqual(len(packets), 1)
            receipt = self.read_receipt()
            self.assertTrue(receipt["time_ok"])
            self.assertFalse(receipt["weather_ok"])
            self.assertTrue(receipt["weather_error"])
            self.assertNotIn("private-credential", err + json.dumps(receipt))
            self.assertLess(len(err), 200)
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)

    def test_no_ack_or_malformed_ack_fails_within_two_seconds(self):
        replies = [None, "#PC1 OK {id} 2\n", "#PC1 OK 0 0\n",
                   "#PC1 OK {id} 0 trailing\n", "prefix #PC1 OK {id} 0\n"]
        for response in replies:
            def reply(packet, number):
                return None if response is None else response.format(id=packet["id"]).encode()

            with self.subTest(response=response), self.board(reply) as (port, packets, flags):
                started = time.monotonic()
                status, out, err = self.invoke([{"USB Serial Number": SERIAL}], [port])
                elapsed = time.monotonic() - started
                self.assertEqual(status, 1)
                self.assertEqual(out, "")
                self.assertIn("ACK timed out", err)
                self.assertLess(elapsed, 2.3)
                self.assertGreaterEqual(elapsed, 1.9)
                self.assertEqual(len(packets), 1)
                self.assertFalse(self.read_receipt()["time_ok"])

    def test_weather_ack_failure_keeps_initial_time_success(self):
        def reply(packet, number):
            return ("#PC1 OK %d 1\n" % packet["id"]).encode() if number == 1 else b"bad ACK\n"

        with self.board(reply) as (port, packets, flags), \
                mock.patch("urllib.request.urlopen", return_value=io.BytesIO(
                    (HOST / "open_meteo_sample.json").read_bytes())):
            status, out, err = self.invoke([{"USB Serial Number": SERIAL}], [port])
            self.assertEqual(status, 1)
            self.assertEqual(len(packets), 2)
            self.assertTrue(self.read_receipt()["time_ok"])
            self.assertFalse(self.read_receipt()["weather_ok"])
            self.assertIn("ACK timed out", self.read_receipt()["weather_error"])

    def test_receipt_write_failure_is_bounded_nonzero_without_traceback(self):
        self.receipt = Path(self.tmp.name)  # A directory cannot be replaced by a file.
        try:
            status, out, err = self.invoke([])
        except OSError:
            self.fail("receipt failure escaped the real entrypoint")
        self.assertEqual(status, 1)
        self.assertEqual(out, "")
        self.assertEqual(err, "pico-clock: receipt write failed\n")
        self.assertEqual(list(Path(self.tmp.name).iterdir()), [])

    def test_weather_schema_rejects_non_integer_code_or_invalid_fields(self):
        variants = [("weather_code", 3.5), ("is_day", 7), ("temperature_2m", True)]
        for key, value in variants:
            data = json.loads((HOST / "open_meteo_sample.json").read_text())
            data["current"][key] = value

            def reply(packet, number):
                return ("#PC1 OK %d 1\n" % packet["id"]).encode()

            with self.subTest(key=key), self.board(reply) as (port, packets, flags), \
                    mock.patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(data).encode())):
                status, out, err = self.invoke([{"USB Serial Number": SERIAL}], [port])
                self.assertEqual(status, 1)
                self.assertEqual(len(packets), 1)
                self.assertTrue(self.read_receipt()["time_ok"])
                self.assertFalse(self.read_receipt()["weather_ok"])
                self.assertTrue(self.read_receipt()["weather_error"])

    def test_rain_uses_only_slots_one_to_four_or_null(self):
        for rain, expected in [([100, 12, None, 49, 67], 67),
                               ([100, None, None, None, None], None), ([], None)]:
            data = json.loads((HOST / "open_meteo_sample.json").read_text())
            data["hourly"]["precipitation_probability"] = rain

            def reply(packet, number):
                return ("#PC1 OK %d %d\n" % (packet["id"], number == 1)).encode()

            with self.subTest(rain=rain), self.board(reply) as (port, packets, flags), \
                    mock.patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(data).encode())):
                status, out, err = self.invoke([{"USB Serial Number": SERIAL}], [port])
                self.assertEqual((status, out, err), (0, "", ""))
                self.assertEqual(packets[1]["weather"][4], expected)

    def test_busy_diagnostic_lock_fails_before_writing(self):
        with self.board(lambda packet, number: None) as (port, packets, flags):
            fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
            try:
                self.helper.fcntl.flock(fd, self.helper.fcntl.LOCK_EX | self.helper.fcntl.LOCK_NB)
                started = time.monotonic()
                status, out, err = self.invoke([{"USB Serial Number": SERIAL}], [port])
                self.assertLess(time.monotonic() - started, 0.5)
                self.assertEqual(status, 1)
                self.assertEqual(packets, [])
                self.assertFalse(self.read_receipt()["time_ok"])
            finally:
                os.close(fd)

    def test_slow_https_is_stopped_by_real_fifteen_second_deadline(self):
        def reply(packet, number):
            return ("#PC1 OK %d 1\n" % packet["id"]).encode()

        def stalled_fetch(*args, **kwargs):
            signal.pause()
            raise AssertionError("weather deadline did not raise")

        with self.board(reply) as (port, packets, flags), \
                mock.patch("urllib.request.urlopen", side_effect=stalled_fetch):
            started = time.monotonic()
            status, out, err = self.invoke([{"USB Serial Number": SERIAL}], [port])
            elapsed = time.monotonic() - started
            self.assertEqual(status, 1)
            self.assertGreaterEqual(elapsed, 14.9)
            self.assertLess(elapsed, 15.5)
            self.assertEqual(len(packets), 1)
            self.assertTrue(self.read_receipt()["time_ok"])
            self.assertEqual(self.read_receipt()["weather_error"], "weather request timed out")
        self.assertEqual(signal.getitimer(signal.ITIMER_REAL)[0], 0)

    def test_oversize_weather_packet_is_refused_without_partial_write(self):
        data = json.loads((HOST / "open_meteo_sample.json").read_text())
        data["current"]["temperature_2m"] = 1e308
        data["daily"]["temperature_2m_max"] = [1e308]
        data["daily"]["temperature_2m_min"] = [-1e308]

        def reply(packet, number):
            return ("#PC1 OK %d 1\n" % packet["id"]).encode()

        with self.board(reply) as (port, packets, flags), \
                mock.patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(data).encode())):
            status, out, err = self.invoke([{"USB Serial Number": SERIAL}], [port])
            self.assertEqual(status, 1)
            self.assertEqual(len(packets), 1)
            self.assertTrue(self.read_receipt()["time_ok"])
            self.assertEqual(self.read_receipt()["weather_error"], "packet exceeds 512 bytes")

    def test_script_main_guard_uses_cli_receipt_argument(self):
        result = subprocess.CompletedProcess([], 0, plistlib.dumps([]), b"")
        with mock.patch("subprocess.run", return_value=result), \
                mock.patch("sys.argv", [str(SCRIPT), "--receipt", str(self.receipt)]), \
                mock.patch("glob.glob", return_value=[]), \
                self.assertRaises(SystemExit) as stopped:
            runpy.run_path(str(SCRIPT), run_name="__main__")
        self.assertEqual(stopped.exception.code, 0)
        self.assertEqual(self.read_receipt()["serial"], SERIAL)

    def test_ambiguous_boards_or_modem_ports_are_never_opened(self):
        board = {"USB Serial Number": SERIAL}
        cases = [([board, board], ["/dev/cu.usbmodem1"]),
                 ([{"IORegistryEntryChildren": [board]}], []),
                 ([board], ["/dev/cu.usbmodem1", "/dev/cu.usbmodem2"])]
        real_open = self.helper.os.open

        def guarded_open(path, *args, **kwargs):
            if str(path).startswith("/dev/"):
                raise AssertionError("unsafe open")
            return real_open(path, *args, **kwargs)

        for tree, ports in cases:
            with self.subTest(tree=tree, ports=ports), \
                    mock.patch.object(self.helper.os, "open", side_effect=guarded_open):
                status, out, err = self.invoke(tree, ports)
            self.assertEqual(status, 1)
            self.assertEqual(out, "")
            self.assertLess(len(err), 200)
            self.assertIn("ambiguous", err)
            self.assertFalse(self.read_receipt()["time_ok"])
            self.assertIsNone(self.read_receipt()["port"])

    def test_no_board_is_quiet_success_with_latest_receipt(self):
        self.receipt.write_text('{"stale":true}')
        status, out, err = self.invoke([{"USB Serial Number": "unrelated"}])
        self.assertEqual((status, out, err), (0, "", ""))
        receipt = self.read_receipt()
        self.assertEqual(receipt["serial"], SERIAL)
        self.assertFalse(receipt["time_ok"])
        self.assertFalse(receipt["weather_ok"])
        self.assertIsNone(receipt["weather_error"])
        self.assertIsNone(receipt["port"])
        self.assertIsInstance(receipt["timestamp"], int)
        self.assertNotIn("stale", receipt)
        self.assertEqual([p.name for p in Path(self.tmp.name).iterdir()], ["receipt.json"])


if __name__ == "__main__":
    unittest.main()
