#!/usr/bin/env python3
"""Behavioral checks for resource limits over manager-owned Bazel settings."""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    "configure_runner_cache", Path(__file__).with_name("configure_runner_cache.py"))
cache = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cache)
PRIVATE = "private-fixture-not-for-receipts"


class ConfigureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.rc = self.root / ".bazelrc"
        self.output = self.root / "artifacts/vs/cache-configuration.json"
        self.cgroups = self.root / "cgroup"
        self.cgroups.mkdir()
        self.original = b"startup --host_jvm_args=-Djavax.net.ssl.trustStore=/etc/ssl/certs/java/cacerts\r\n\r\n"
        self.rc.write_bytes(self.original)

    def run_main(self, *extra_args):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cache.main(["--rc", str(self.rc), "--output", str(self.output), *extra_args])
        return code, json.loads(self.output.read_text()), stdout.getvalue()

    def resource_block(self, jobs=4, memory=12288):
        return "\n".join([cache.RESOURCE_BEGIN, f"build --jobs={jobs}",
                          f"build --local_ram_resources={memory}", cache.RESOURCE_END]).encode()

    def write_cgroups(self, memory, cpu):
        (self.cgroups / "memory.max").write_text(str(memory) + "\n")
        (self.cgroups / "cpu.max").write_text(cpu + "\n")

    def test_cgroup_profiles_apply_limits_and_preserve_runner_settings_when_switching(self):
        original = self.original + (
            b"common --remote_cache=https://cache.example.invalid/\n"
            b"common --credential_helper=cache.example.invalid=/image/helper\n"
            b"common --disk_cache=/bazel_cache/disk\n"
            b"common --repository_cache=/bazel_cache/repository\n\n")
        self.rc.write_bytes(original)
        profiles = [("pilot", 6, 20, "600000 100000", 12288),
                    ("fleet", 4, 10, "200000 50000", 7168),
                    ("pilot", 6, 20, "600000 100000", 12288)]
        for name, cores, memory_gib, cpu, local_memory_mb in profiles:
            with self.subTest(profile=name), mock.patch.object(cache, "CGROUP_ROOT", self.cgroups):
                self.write_cgroups(memory_gib * 1024**3, cpu)
                code, receipt, stdout = self.run_main("--from-cgroup")
            self.assertEqual(code, 0)
            self.assertEqual(receipt, {
                "status": "configured",
                "resource_limits": {"jobs": 4, "local_memory_mb": local_memory_mb},
                "resource_profile": {"name": name, "cpu_cores": cores,
                                     "memory_limit_bytes": memory_gib * 1024**3,
                                     "sonic_build_memory": f"{memory_gib}g", "jobs": 4,
                                     "local_memory_mb": local_memory_mb},
            })
            self.assertEqual(json.loads(stdout), receipt)
            self.assertEqual(self.rc.read_bytes(), original + self.resource_block(4, local_memory_mb) + b"\n")
            self.assertNotIn("example.invalid", stdout + self.output.read_text())
            self.assertNotIn("endpoint", stdout + self.output.read_text())

    def test_invalid_or_mismatched_cgroups_fail_before_accessing_runner_rc(self):
        cases = [("max", "400000 100000", "invalid_cgroup_limits"),
                 (10 * 1024**3, "max 100000", "invalid_cgroup_limits"),
                 (10 * 1024**3, "400000", "invalid_cgroup_limits"),
                 (10 * 1024**3, "400000 0", "invalid_cgroup_limits"),
                 (10 * 1024**3, "450000 100000", "unsupported_resource_profile"),
                 (10 * 1024**3, "600000 100000", "unsupported_resource_profile"),
                 (20 * 1024**3, "400000 100000", "unsupported_resource_profile")]
        for memory, cpu, error_class in cases:
            with self.subTest(memory=memory, cpu=cpu), mock.patch.object(cache, "CGROUP_ROOT", self.cgroups), \
                    mock.patch.object(Path, "read_bytes") as read:
                self.write_cgroups(memory, cpu)
                code, receipt, _ = self.run_main("--from-cgroup")
                read.assert_not_called()
            self.assertEqual(code, 1)
            self.assertEqual(receipt, {"status": "failure", "error_class": error_class})
            self.assertEqual(self.rc.read_bytes(), self.original)

    def test_resource_limits_preserve_all_runner_modes_without_external_access(self):
        modes = {
            "authenticated": b"common --remote_cache=https://cache.example.invalid/\n"
                             b"common --credential_helper=cache.example.invalid=/image/helper\n",
            "anonymous": b"common --remote_cache=https://anonymous.example.invalid/\n",
            "local": b"common --remote_cache=\n"
                     b"# BEGIN SONiC runner cache connectivity fallback\n"
                     b"common --remote_cache=\n"
                     b"# END SONiC runner cache connectivity fallback\n",
        }
        for mode, selected in modes.items():
            with self.subTest(mode=mode):
                original = self.original + selected + (
                    b"common --disk_cache=/bazel_cache/disk\n"
                    b"common --repository_cache=/bazel_cache/repository\n\n")
                self.rc.write_bytes(original)
                with mock.patch.dict(os.environ, {"BAZEL_REMOTE_AUTH": PRIVATE}), \
                        mock.patch.object(socket, "create_connection", side_effect=AssertionError("network requested")) as network, \
                        mock.patch.object(subprocess, "run", side_effect=AssertionError("helper requested")) as child, \
                        mock.patch.object(importlib.util, "spec_from_file_location", side_effect=AssertionError("helper imported")) as helper, \
                        mock.patch.object(io, "open", wraps=io.open) as opened:
                    code, receipt, stdout = self.run_main("--jobs=4", "--local-memory-mb=12288")
                self.assertEqual(code, 0)
                self.assertEqual(receipt, {"status": "configured",
                                          "resource_limits": {"jobs": 4, "local_memory_mb": 12288}})
                self.assertEqual(json.loads(stdout), receipt)
                self.assertEqual(self.rc.read_bytes(), original + self.resource_block() + b"\n")
                self.assertEqual({Path(call.args[0]) for call in opened.call_args_list}, {self.rc, self.output})
                network.assert_not_called()
                child.assert_not_called()
                helper.assert_not_called()
                self.assertNotIn(PRIVATE, stdout + self.output.read_text())
                self.assertNotIn("example.invalid", stdout + self.output.read_text())

    def test_replacement_preserves_exact_prefix_and_suffix_and_is_idempotent(self):
        prefix = self.original + b"common --remote_cache=https://cache.example.invalid/\n\n"
        suffix = b"\r\n\r\n# Manager-owned trailing settings\r\ntest --test_output=errors"
        self.rc.write_bytes(prefix + self.resource_block(2, 8192) + b"\n" + suffix)
        cache.set_resource_limits(self.rc, 4, 12288)
        expected = prefix + suffix + b"\n" + self.resource_block() + b"\n"
        self.assertEqual(self.rc.read_bytes(), expected)
        with mock.patch.object(Path, "write_bytes") as write:
            cache.set_resource_limits(self.rc, 4, 12288)
        write.assert_not_called()
        cache.set_resource_limits(self.rc, 1, 4096)
        self.assertEqual(self.rc.read_bytes(), prefix + suffix + b"\n" + self.resource_block(1, 4096) + b"\n")

    def test_refreshed_limits_follow_conflicting_trailing_resource_settings(self):
        trailing = b"build --jobs=48\nbuild --local_ram_resources=65536\n\n"
        self.rc.write_bytes(self.original + self.resource_block(2, 8192) + b"\n" + trailing)
        cache.set_resource_limits(self.rc, 4, 12288)
        expected = self.original + trailing + self.resource_block() + b"\n"
        self.assertEqual(self.rc.read_bytes(), expected)
        lines = self.rc.read_text().splitlines()
        self.assertEqual([line for line in lines if line.startswith("build --jobs=")][-1], "build --jobs=4")
        self.assertEqual([line for line in lines if line.startswith("build --local_ram_resources=")][-1],
                         "build --local_ram_resources=12288")
        for _ in range(2):
            cache.set_resource_limits(self.rc, 4, 12288)
            self.assertEqual(self.rc.read_bytes(), expected)

    def test_appending_to_a_file_without_a_final_newline_preserves_its_content(self):
        original = self.original + b"build --keep_going"
        self.rc.write_bytes(original)
        cache.set_resource_limits(self.rc, 4, 12288)
        self.assertEqual(self.rc.read_bytes(), original + b"\n" + self.resource_block() + b"\n")

    def test_omitted_limits_validate_existing_rc_without_writing_it(self):
        with mock.patch.object(Path, "write_bytes") as write:
            code, receipt, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertEqual(receipt, {"status": "configured", "resource_limits": {}})
        write.assert_not_called()
        self.assertEqual(self.rc.read_bytes(), self.original)

    def test_missing_runner_rc_fails_without_creating_it(self):
        self.rc.unlink()
        code, receipt, stdout = self.run_main("--jobs=4", "--local-memory-mb=12288")
        self.assertEqual(code, 1)
        self.assertEqual(receipt, {"status": "failure", "error_class": "runner_rc_missing"})
        self.assertEqual(json.loads(stdout), receipt)
        self.assertFalse(self.rc.exists())

    def test_unreadable_runner_rc_fails_without_exposing_exception_text(self):
        with mock.patch.object(Path, "read_bytes", side_effect=PermissionError(PRIVATE)), \
                mock.patch.object(Path, "write_bytes") as write:
            code, receipt, stdout = self.run_main("--jobs=4", "--local-memory-mb=12288")
        self.assertEqual(code, 1)
        self.assertEqual(receipt["error_class"], "runner_rc_unreadable")
        write.assert_not_called()
        self.assertNotIn(PRIVATE, stdout + self.output.read_text())
        self.assertEqual(self.rc.read_bytes(), self.original)

    def test_resource_write_failure_is_sanitized(self):
        with mock.patch.object(Path, "write_bytes", side_effect=PermissionError(PRIVATE)):
            code, receipt, stdout = self.run_main("--jobs=4", "--local-memory-mb=12288")
        self.assertEqual(code, 1)
        self.assertEqual(receipt["error_class"], "runner_rc_write_failed")
        self.assertNotIn(PRIVATE, stdout + self.output.read_text())
        self.assertEqual(self.rc.read_bytes(), self.original)

    def test_invalid_values_fail_before_reading_or_writing_rc(self):
        cases = [("--jobs=0", "--local-memory-mb=12288"),
                 ("--jobs=-1", "--local-memory-mb=12288"),
                 ("--jobs=4", "--local-memory-mb=0"),
                 ("--jobs=4", "--local-memory-mb=-1"),
                 ("--jobs=1.5", "--local-memory-mb=12288"),
                 ("--jobs=4", "--local-memory-mb=invalid"),
                 ("--jobs=4",), ("--local-memory-mb=12288",),
                 ("--from-cgroup", "--jobs=4", "--local-memory-mb=12288"),
                 ("--from-cgroup", "--jobs=4"),
                 ("--from-cgroup", "--local-memory-mb=12288")]
        for arguments in cases:
            with self.subTest(arguments=arguments), mock.patch.object(Path, "read_bytes") as read:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                    self.run_main(*arguments)
                self.assertEqual(caught.exception.code, 2)
                read.assert_not_called()
                self.assertFalse(self.output.exists())
            self.assertEqual(self.rc.read_bytes(), self.original)

    def test_malformed_resource_blocks_fail_without_rewriting_rc(self):
        cases = [cache.RESOURCE_BEGIN, cache.RESOURCE_END,
                 cache.RESOURCE_END + "\n" + cache.RESOURCE_BEGIN,
                 cache.RESOURCE_BEGIN + "\n" + cache.RESOURCE_BEGIN + "\n" + cache.RESOURCE_END]
        for suffix in cases:
            with self.subTest(suffix=suffix):
                original = self.original + suffix.encode() + b"\n"
                self.rc.write_bytes(original)
                code, receipt, _ = self.run_main("--jobs=4", "--local-memory-mb=12288")
                self.assertEqual(code, 1)
                self.assertEqual(receipt["error_class"], "invalid_resource_block")
                self.assertEqual(self.rc.read_bytes(), original)

    def test_receipt_write_failure_reports_only_the_failure_class(self):
        self.output.parent.mkdir(parents=True)
        self.output.mkdir()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = cache.main(["--rc", str(self.rc), "--output", str(self.output)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout.getvalue()),
                         {"status": "failure", "error_class": "receipt_write_failed"})


if __name__ == "__main__":
    unittest.main()
