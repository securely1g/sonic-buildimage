#!/usr/bin/env python3
"""Pure checks for the SNMP workflow's extracted installed-image phase."""

import contextlib
import copy
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import snmp_loaded as loaded


class SnmpLoadedTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.images = ("docker-snmp", "docker-snmp-dbg")
        self.receipt = {"status": "passed", "architecture": "amd64", "revision": "a" * 40, "artifacts": {}}
        for filename in (*(name + ".gz" for name in self.images), "test-redis.tar"):
            data = filename.encode()
            (self.directory / filename).write_bytes(data)
            self.receipt["artifacts"][filename] = {"sha256": hashlib.sha256(data).hexdigest()}
        self.write_receipt()
        self.expected = {"rootfs": {"diff_ids": ["sha256:" + "c" * 64]},
                         "config": {"User": "101:102", "Entrypoint": ["/usr/bin/supervisord"]}}
        for name in self.images:
            (self.directory / (name + ".validation.json")).write_text(json.dumps(self.expected))
        self.actual = {"Os": "linux", "Architecture": "amd64", "RootFS": {"Layers": self.expected["rootfs"]["diff_ids"]},
                       "Config": {**self.expected["config"], "Empty": None}}
        self.calls = []

    def write_receipt(self):
        (self.directory / "validation.json").write_text(json.dumps(self.receipt))

    def execute(self, failure=None):
        def check_output(command, **kwargs):
            self.calls.append((command, kwargs))
            if command == ["git", "rev-parse", "HEAD"]:
                return "a" * 40 + "\n"
            if command[:3] == ["docker", "image", "inspect"]:
                return json.dumps([self.actual]).encode()
            self.fail("Unexpected command: " + repr(command))

        def run(command, **kwargs):
            self.calls.append((command, kwargs))
            if command[:2] == ["docker", "run"]:
                kwargs["stdout"].write('{"status":"passed"}\n')
                if failure:
                    raise subprocess.CalledProcessError(failure, command)
            return subprocess.CompletedProcess(command, 0)

        with patch.object(loaded.subprocess, "check_output", side_effect=check_output), \
                patch.object(loaded.subprocess, "run", side_effect=run), contextlib.redirect_stdout(io.StringIO()):
            loaded.run_loaded(self.directory)

    def test_original_loaded_checks_and_container_arguments_are_preserved(self):
        self.execute()
        fixture = self.directory / "redis-fixture"
        self.assertEqual(self.calls[:6], [
            (["git", "rev-parse", "HEAD"], {"cwd": loaded.ROOT, "text": True}),
            (["docker", "load", "--input", str(self.directory / "docker-snmp.gz")], {"check": True}),
            (["docker", "image", "inspect", "docker-snmp:latest"], {}),
            (["docker", "load", "--input", str(self.directory / "docker-snmp-dbg.gz")], {"check": True}),
            (["docker", "image", "inspect", "docker-snmp-dbg:latest"], {}),
            (["tar", "--no-same-owner", "-xf", str(self.directory / "test-redis.tar"), "-C", str(fixture)], {"check": True}),
        ])
        for name, (command, kwargs) in zip(self.images, self.calls[6:]):
            expected = ["docker", "run", "--rm", "--network=none", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                        "--read-only", "--user=101:102", "--pids-limit=128", "--memory=4g", "--cpus=2",
                        "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=256m",
                        "--mount", "type=bind,src=" + str(loaded.ROOT / "dockers/docker-snmp/bazel/runtime_probe.py") + ",dst=/probe/runtime_probe.py,readonly",
                        "--mount", "type=bind,src=" + str(fixture) + ",dst=/probe/redis,readonly",
                        "--entrypoint", "/usr/bin/python3", name + ":latest", "-B", "/probe/runtime_probe.py"]
            self.assertEqual(command, expected)
            self.assertTrue(kwargs["check"])
            self.assertEqual((self.directory / (name + ".runtime.json")).read_text(), '{"status":"passed"}\n')
        self.assertEqual(len(self.calls), 8)

    def test_invalid_report_stops_before_loading_or_probing(self):
        original = copy.deepcopy(self.receipt)
        for field, value in (("status", "failed"), ("architecture", "arm64"), ("revision", "b" * 40)):
            with self.subTest(field=field):
                self.calls.clear()
                self.receipt = {**original, field: value}
                self.write_receipt()
                with self.assertRaises(ValueError):
                    self.execute()
                self.assertFalse(any(command[0] != "git" for command, _ in self.calls))

    def test_changed_archives_or_fixture_stop_before_container_probe(self):
        for filename in ("docker-snmp.gz", "docker-snmp-dbg.gz", "test-redis.tar"):
            with self.subTest(filename=filename):
                self.calls.clear()
                path = self.directory / filename
                saved = path.read_bytes()
                path.write_bytes(b"changed")
                with self.assertRaises(ValueError):
                    self.execute()
                self.assertFalse(any(command[:2] == ["docker", "run"] or command[0] == "tar" for command, _ in self.calls))
                path.write_bytes(saved)

    def test_loaded_platform_layers_and_configuration_must_match(self):
        original = copy.deepcopy(self.actual)
        for changes in ({"Os": "other"}, {"Architecture": "arm64"}, {"RootFS": {"Layers": []}},
                        {"Config": {**original["Config"], "User": "0:0"}}):
            with self.subTest(changes=changes):
                self.calls.clear()
                self.actual = {**original, **changes}
                with self.assertRaises(ValueError):
                    self.execute()
                self.assertFalse(any(command[:2] == ["docker", "run"] or command[0] == "tar" for command, _ in self.calls))

    def test_probe_failure_keeps_output_and_stops_the_debug_probe(self):
        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.execute(failure=7)
        self.assertEqual(error.exception.returncode, 7)
        self.assertEqual(len(self.calls), 7)
        self.assertEqual((self.directory / "docker-snmp.runtime.json").read_text(), '{"status":"passed"}\n')
        self.assertFalse((self.directory / "docker-snmp-dbg.runtime.json").exists())


if __name__ == "__main__":
    unittest.main()
