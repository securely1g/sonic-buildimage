"""Failure and evidence checks without starting a guest or host services."""

import base64
import gzip
import hashlib
import json
from pathlib import Path
import re
import shlex
import tempfile
import unittest
from unittest.mock import patch
import zlib

from tools.ci import guest_probe, guest_validation


def healthy():
    return {"schema_version": 1, "status": "passed", "services": {
        name: {"critical_processes": {name: "RUNNING"}, "errors": []}
        for name in guest_probe.SERVICES
    }, "database_ping": {"returncode": 0, "output": "PONG"}}


class Serial:
    def __init__(self, results):
        self.results = iter(results)
        self.commands = []
        self.timeouts = []

    def sendline(self, command):
        self.commands.append(command)

    def expect(self, pattern, timeout):
        self.timeouts.append(timeout)
        if pattern.startswith("(?m)"):
            value = next(self.results)
            if isinstance(value, Exception):
                raise value
            output = guest_probe.MARKER + json.dumps(value) + "\r\n"
            self.match = re.search(pattern, output)
            assert self.match


class ChunkedSerial(Serial):
    """Match each received chunk, as the real serial expect loop does."""

    def __init__(self, chunks):
        super().__init__([])
        self.chunks = iter(chunks)
        self.buffer = ""

    def expect(self, pattern, timeout):
        self.timeouts.append(timeout)
        if pattern.startswith("(?m)"):
            for chunk in self.chunks:
                self.buffer += chunk
                self.match = re.search(pattern, self.buffer)
                if self.match:
                    return
            raise TimeoutError("No complete guest receipt")


class CriticalProcessesTest(unittest.TestCase):
    def test_program_and_group_with_unrelated_finished_process(self):
        self.assertEqual(guest_probe.critical_status(
            "program:syncd\ngroup:bgp\n", "syncd RUNNING pid 1\nbgp:zebra RUNNING pid 2\nbgp:bgpd RUNNING pid 3\ninit EXITED Oct 1"),
            {"syncd": "RUNNING", "bgp:zebra": "RUNNING", "bgp:bgpd": "RUNNING"})

    def test_missing_program_or_group_fails(self):
        for declaration in ("program:syncd", "group:bgp"):
            with self.subTest(declaration=declaration), self.assertRaisesRegex(ValueError, "Missing"):
                guest_probe.critical_status(declaration, "unrelated RUNNING pid 1")

    def test_malformed_or_empty_declarations_fail(self):
        for declaration in ("", "program:", "unexpected:syncd", "program:group:syncd", "error from Docker"):
            with self.subTest(declaration=declaration), self.assertRaises(ValueError):
                guest_probe.critical_status(declaration, "syncd RUNNING pid 1")

    def test_duplicate_or_unrecognized_supervisor_output_fails(self):
        for output in ("syncd RUNNING pid 1\nsyncd RUNNING pid 2", "socket failure", "syncd INVALID pid 1"):
            with self.subTest(output=output), self.assertRaises(ValueError):
                guest_probe.critical_status("program:syncd", output)

    def test_stopped_critical_process_is_retained(self):
        self.assertEqual(guest_probe.critical_status("program:syncd", "syncd FATAL exited"), {"syncd": "FATAL"})


class ProbeTest(unittest.TestCase):
    def execute(self, broken=None):
        def run(args):
            service = args[-1].removesuffix(".service") if args[0] == "systemctl" else args[2] if args[1] == "exec" else args[-1]
            if args[0] == "systemctl":
                result = {"returncode": 0, "output": "active"}
            elif args[1] == "inspect":
                result = {"returncode": 0, "output": "true"}
            elif "cat" in args:
                result = {"returncode": 0, "output": "program:" + service}
            elif "supervisorctl" in args:
                result = {"returncode": 0, "output": service + " RUNNING pid 1"}
            else:
                result = {"returncode": 0, "output": "PONG"}
            return broken(args, result) if broken else result
        with patch.object(Path, "is_file", return_value=True), patch.object(Path, "read_text", return_value="build_version: test"):
            return guest_probe.probe(run)

    def test_all_services_and_database_are_required(self):
        self.assertTrue(guest_validation.passed(self.execute()))

    def test_stopped_container_fails_even_if_systemd_active(self):
        value = self.execute(lambda args, result: {"returncode": 0, "output": "false"} if args[1] == "inspect" else result)
        self.assertFalse(guest_validation.passed(value))

    def test_failed_or_missing_critical_process_fails(self):
        for output in ("syncd FATAL exited", "unrelated RUNNING pid 1"):
            value = self.execute(lambda args, result: {"returncode": 0, "output": output} if args[:3] == ["docker", "exec", "syncd"] and "supervisorctl" in args else result)
            self.assertFalse(guest_validation.passed(value))

    def test_database_refusal_fails(self):
        value = self.execute(lambda args, result: {"returncode": 1, "output": "Connection refused"} if "redis-cli" in args else result)
        self.assertFalse(guest_validation.passed(value))

    def test_command_failure_cannot_look_healthy(self):
        value = self.execute(lambda args, result: {**result, "returncode": 1} if args[0] == "systemctl" else result)
        self.assertFalse(guest_validation.passed(value))


class TransportTest(unittest.TestCase):
    def test_receipt_waits_for_a_complete_serial_line(self):
        value = healthy()
        line = guest_probe.MARKER + json.dumps(value)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); image = root / "image"; image.write_bytes(b"image")
            for ending in ("\n", "\r\n"):
                output = line + ending
                for split in range(1, len(output)):
                    with self.subTest(ending=repr(ending), split=split):
                        serial = ChunkedSerial([output[:split], output[split:]])
                        result = guest_validation.validate(serial, "prompt", root / "receipt.json", image)
                        self.assertEqual(result["probes"], [value])
                        self.assertEqual(serial.buffer, output)

    def test_malformed_complete_receipt_remains_a_failure(self):
        serial = ChunkedSerial([guest_probe.MARKER + '{"sc', "\r\n"])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); image = root / "image"; image.write_bytes(b"image")
            with self.assertRaises(json.JSONDecodeError):
                guest_validation.validate(serial, "prompt", root / "receipt.json", image)
            saved = json.loads((root / "receipt.json").read_text())
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["probes"], [])
        self.assertIn("Unterminated string", saved["error"])

    def test_receipt_without_newline_times_out(self):
        serial = ChunkedSerial([guest_probe.MARKER + json.dumps(healthy())])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); image = root / "image"; image.write_bytes(b"image")
            with self.assertRaisesRegex(TimeoutError, "complete guest receipt"):
                guest_validation.validate(serial, "prompt", root / "receipt.json", image)
            saved = json.loads((root / "receipt.json").read_text())
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["probes"], [])

    def test_payload_is_exact_and_fits_serial_line(self):
        command = guest_validation.probe_command()
        self.assertLess(len(command.encode()), 3500)
        parsed = shlex.split(command)
        self.assertEqual(parsed[:4], ["sudo", "-n", "python3", "-c"])
        encoded = re.search(r"b64decode\('([^']+)'\)", parsed[4]).group(1)
        self.assertEqual(zlib.decompress(base64.b64decode(encoded)), Path(guest_probe.__file__).read_bytes())

    def test_success_after_retry_records_each_probe_and_exact_image(self):
        failed = healthy(); failed["status"] = "failed"
        serial = Serial([failed, healthy()])
        ticks = [0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); image = root / "image.qcow2"; image.write_bytes(b"specific image")
            result = guest_validation.validate(serial, "prompt", root / "receipt.json", image, timeout=60,
                                               clock=lambda: ticks[0], sleep=lambda seconds: ticks.__setitem__(0, ticks[0] + seconds))
            saved = json.loads((root / "receipt.json").read_text())
        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(saved["probes"]), 2)
        self.assertEqual(saved["image_sha256"], hashlib.sha256(b"specific image").hexdigest())
        self.assertEqual(saved["_generated"]["notice"], "AUTO-GENERATED. DO NOT EDIT MANUALLY.")
        self.assertEqual(len(serial.commands), 2)

    def test_serial_error_writes_failed_receipt(self):
        serial = Serial([TimeoutError("serial connection lost")])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); image = root / "image"; image.write_bytes(b"image")
            with self.assertRaisesRegex(TimeoutError, "lost"):
                guest_validation.validate(serial, "prompt", root / "receipt.json", image)
            saved = json.loads((root / "receipt.json").read_text())
        self.assertEqual(saved["status"], "failed")
        self.assertIn("lost", saved["error"])

    def test_deadline_does_not_report_healthy(self):
        failed = healthy(); failed["status"] = "failed"
        serial = Serial([failed]); ticks = [0]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); image = root / "image"; image.write_bytes(b"image")
            with self.assertRaisesRegex(RuntimeError, "deadline"):
                guest_validation.validate(serial, "prompt", root / "receipt.json", image, timeout=1,
                                          clock=lambda: ticks[0], sleep=lambda seconds: ticks.__setitem__(0, ticks[0] + seconds))
            saved = json.loads((root / "receipt.json").read_text())
        self.assertEqual(saved["status"], "failed")

    def test_incomplete_report_is_not_success(self):
        for value in ({}, {"status": "passed"}, {**healthy(), "services": {}}, {**healthy(), "database_ping": {}}):
            with self.subTest(value=value):
                self.assertFalse(guest_validation.passed(value))

    def test_uploaded_image_must_match_successful_receipt(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "image.gz"; image.write_bytes(gzip.compress(b"validated image"))
            report = root / "receipt.json"
            value = {"status": "passed", "image_sha256": hashlib.sha256(b"validated image").hexdigest(), "probes": [healthy()]}
            report.write_text(json.dumps(value))
            self.assertEqual(guest_validation.verify_report(report, image), value)
            image.write_bytes(gzip.compress(b"different image"))
            with self.assertRaisesRegex(ValueError, "does not match"):
                guest_validation.verify_report(report, image)
            value["status"] = "failed"; report.write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, "successful"):
                guest_validation.verify_report(report, image)

    def test_echoed_command_is_not_a_result_line(self):
        value = healthy()
        serial = ChunkedSerial([
            "sudo python3 -c 'print(\"" + guest_probe.MARKER + "{}\")'\r\n",
            guest_probe.MARKER + json.dumps(value) + "\r\n",
        ])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); image = root / "image"; image.write_bytes(b"image")
            result = guest_validation.validate(serial, "prompt", root / "receipt.json", image)
        self.assertEqual(result["probes"], [value])


if __name__ == "__main__":
    unittest.main()
