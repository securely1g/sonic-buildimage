"""Keep full-VS disk reports useful when Docker shares storage or a build fails.

Check filesystem deduplication and simultaneous peaks, then run the recorder as
a real background process to verify that cleanup stops it and retains evidence.
"""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "native_vs/disk_usage.py"
SPEC = importlib.util.spec_from_file_location("native_vs_disk_usage", SCRIPT)
disk = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(disk)


def filesystem(device, used, available):
    return {"device": device, "paths": ["/" + device], "mount": "/" + device,
            "total_bytes": 1000, "used_bytes": used, "available_bytes": available}


def snapshot(timestamp, values, errors=None):
    return {"timestamp": timestamp, "filesystems": values, "errors": errors or []}


class DiskUsageTest(unittest.TestCase):
    def test_workspace_and_docker_on_same_filesystem_are_counted_once(self):
        """A bind alias must not double the reported disk; reserved blocks stay separate."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "docker").mkdir()
            fake = mock.Mock(f_blocks=100, f_bfree=30, f_bavail=20, f_frsize=4096)
            with mock.patch.object(disk.os, "statvfs", return_value=fake) as statvfs:
                row = disk.sample([root, root / "docker"])
            statvfs.assert_called_once()
            self.assertEqual(row["errors"], [])
            self.assertEqual(len(row["filesystems"]), 1)
            measured = row["filesystems"][0]
            self.assertEqual(measured["paths"], [str(root), str(root / "docker")])
            self.assertEqual(measured["used_bytes"], 70 * 4096)
            self.assertEqual(measured["available_bytes"], 20 * 4096)

    def test_combined_peak_is_simultaneous_and_errors_do_not_hide_missing_storage(self):
        """Do not sum separate filesystem peaks reached at different build stages."""
        samples = [
            snapshot("start", [filesystem("workspace", 100, 890), filesystem("docker", 200, 780)]),
            snapshot("compile", [filesystem("workspace", 500, 490), filesystem("docker", 200, 780)]),
            snapshot("images", [filesystem("workspace", 200, 790), filesystem("docker", 700, 280)]),
            snapshot("missing", [filesystem("workspace", 950, 40)], ["Docker unavailable"]),
        ]
        result = disk.summarize(samples, True, "requested stop")
        self.assertEqual(result["sample_count"], 4)
        self.assertEqual(result["complete_sample_count"], 3)
        self.assertEqual(result["initial_used_bytes"], 300)
        self.assertEqual(result["maximum_sampled_used_bytes"], 900)
        self.assertEqual(result["maximum_sampled_growth_bytes"], 600)
        self.assertEqual(result["maximum_sampled_at"], "images")
        self.assertEqual(result["minimum_sampled_available_bytes"], 1070)
        self.assertEqual([fs["minimum_available_bytes"] for fs in result["filesystems"]], [490, 280])
        self.assertNotIn("initial_used_bytes", disk.summarize(samples[-1:], False, "incomplete"))

    def test_finish_stops_real_background_recorder_and_keeps_final_sample(self):
        """Always-run cleanup must wake a sleeping recorder and save its final measurements."""
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "measurements"
            process = subprocess.Popen([
                sys.executable, "-B", str(SCRIPT), "record", "--output", str(output),
                "--path", temporary, "--interval", "30", "--max-duration", "60",
            ], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 10
                while not (output / "ready").exists() and time.monotonic() < deadline:
                    self.assertIsNone(process.poll())
                    time.sleep(0.05)
                self.assertTrue((output / "ready").exists())
                result = subprocess.run([
                    sys.executable, "-B", str(SCRIPT), "finish", "--output", str(output), "--wait", "5",
                ], text=True, capture_output=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                stdout, stderr = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, stdout + stderr)
                summary = json.loads((output / "summary.json").read_text())
                self.assertTrue(summary["finished"])
                self.assertGreaterEqual(summary["sample_count"], 2)
                self.assertEqual(summary["sample_count"], summary["complete_sample_count"])
                self.assertEqual((output / "done").read_text(), "requested stop\n")
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate()

    def test_duration_limit_keeps_partial_evidence_and_finish_reports_failure(self):
        """An abandoned sampler has a time bound and must not claim a complete build measurement."""
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "measurements"
            result = subprocess.run([
                sys.executable, "-B", str(SCRIPT), "record", "--output", str(output),
                "--path", temporary, "--interval", "30", "--max-duration", "0.05",
            ], text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run([
                sys.executable, "-B", str(SCRIPT), "finish", "--output", str(output), "--wait", "1",
            ], text=True, capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            summary = json.loads((output / "summary.json").read_text())
            self.assertFalse(summary["finished"])
            self.assertEqual(summary["stop_reason"], "duration limit")
            self.assertGreaterEqual(summary["sample_count"], 1)

    def test_missing_docker_storage_fails_before_claiming_sampler_is_ready(self):
        """A missing mount must not silently omit Docker while a costly build proceeds."""
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "measurements"
            result = subprocess.run([
                sys.executable, "-B", str(SCRIPT), "record", "--output", str(output),
                "--path", temporary, "--path", str(Path(temporary) / "missing-docker"),
            ], text=True, capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((output / "ready").exists())
            summary = json.loads((output / "summary.json").read_text())
            self.assertFalse(summary["finished"])
            self.assertNotIn("initial_used_bytes", summary)
            self.assertEqual(summary["complete_sample_count"], 0)


if __name__ == "__main__":
    unittest.main()
