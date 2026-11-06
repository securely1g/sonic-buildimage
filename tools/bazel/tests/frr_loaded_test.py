#!/usr/bin/env python3
"""Pure checks for the FRR workflow's extracted installed-image phase."""

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
from tools.bazel.ci import frr_loaded as loaded


class FrrLoadedTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.images = ("docker-fpm-frr", "docker-fpm-frr-dbg")
        self.receipt = {"status": "passed", "architecture": "amd64", "revision": "a" * 40, "artifacts": {}}
        for filename in (*(name + ".gz" for name in self.images), "test-redis.tar"):
            data = filename.encode()
            (self.directory / filename).write_bytes(data)
            self.receipt["artifacts"][filename] = {"sha256": hashlib.sha256(data).hexdigest()}
        self.write_receipt()
        self.expected = {"config_digest": "sha256:" + "b" * 64, "rootfs": {"diff_ids": ["sha256:" + "c" * 64]}}
        for name in self.images:
            (self.directory / (name + ".validation.json")).write_text(json.dumps(self.expected))
        self.actual = {"Os": "linux", "Architecture": "amd64", "RootFS": {"Layers": self.expected["rootfs"]["diff_ids"]}}
        self.calls = []

    def write_receipt(self):
        (self.directory / "validation.json").write_text(json.dumps(self.receipt))

    def execute(self, failure=None):
        def run(command, **kwargs):
            self.calls.append((command, kwargs))
            if command[0] == "docker":
                kwargs["stdout"].write('{"status":"passed"}\n')
                if failure:
                    raise subprocess.CalledProcessError(failure, command)
            return subprocess.CompletedProcess(command, 0)
        with patch.object(loaded.subprocess, "check_output", return_value="a" * 40 + "\n") as revision, \
                patch.object(loaded, "load_archive", return_value=(self.actual, {}, None)) as load, \
                patch.object(loaded, "verify_loaded_image") as verify, \
                patch.object(loaded.subprocess, "run", side_effect=run), contextlib.redirect_stdout(io.StringIO()):
            loaded.run_loaded(self.directory)
        return revision, load, verify

    def test_original_loaded_checks_and_container_arguments_are_preserved(self):
        revision, load, verify = self.execute()
        revision.assert_called_once_with(["git", "rev-parse", "HEAD"], cwd=loaded.ROOT, text=True)
        self.assertEqual(load.call_count, 2)
        self.assertEqual(verify.call_count, 2)
        for name, call in zip(self.images, load.call_args_list):
            self.assertEqual(call.args, (self.directory / (name + ".gz"),
                                        "sonic-frr-validation-" + name + ":latest", self.expected["config_digest"]))
        fixture = self.directory / "redis-fixture"
        self.assertEqual(self.calls[0], (["tar", "--no-same-owner", "-xf", str(self.directory / "test-redis.tar"),
                                          "-C", str(fixture)], {"check": True}))
        for name, (command, kwargs) in zip(self.images, self.calls[1:]):
            expected = ["docker", "run", "--rm", "--network=none", "--cap-drop=ALL", "--security-opt=no-new-privileges",
                        "--read-only", "--user=300:300", "--group-add=102", "--pids-limit=128", "--memory=4g", "--cpus=2",
                        "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=256m",
                        "--mount", "type=bind,src=" + str(loaded.ROOT / "dockers/docker-fpm-frr/bazel/runtime_probe.py") + ",dst=/probe/runtime_probe.py,readonly",
                        "--mount", "type=bind,src=" + str(fixture) + ",dst=/probe/redis,readonly",
                        "--mount", "type=bind,src=" + str(self.directory / "validation.json") + ",dst=/probe/validation.json,readonly",
                        "--entrypoint", "/usr/bin/python3", "sonic-frr-validation-" + name + ":latest",
                        "-B", "/probe/runtime_probe.py", "--validation", "/probe/validation.json"]
            if name.endswith("-dbg"):
                expected.append("--debug")
            self.assertEqual(command, expected)
            self.assertTrue(kwargs["check"])
            self.assertEqual((self.directory / (name + ".runtime.json")).read_text(), '{"status":"passed"}\n')

    def test_invalid_report_stops_before_loading_or_probing(self):
        original = copy.deepcopy(self.receipt)
        for field, value in (("status", "failed"), ("architecture", "arm64"), ("revision", "b" * 40)):
            with self.subTest(field=field):
                self.receipt = {**original, field: value}
                self.write_receipt()
                with self.assertRaises(ValueError):
                    self.execute()
                self.assertEqual(self.calls, [])

    def test_changed_archives_or_fixture_stop_before_container_probe(self):
        for filename in ("docker-fpm-frr.gz", "docker-fpm-frr-dbg.gz", "test-redis.tar"):
            with self.subTest(filename=filename):
                path = self.directory / filename
                saved = path.read_bytes()
                path.write_bytes(b"changed")
                with self.assertRaises(ValueError):
                    self.execute()
                self.assertEqual(self.calls, [])
                path.write_bytes(saved)

    def test_loaded_platform_and_layers_must_match(self):
        for actual in ({**self.actual, "Os": "other"}, {**self.actual, "Architecture": "arm64"},
                       {**self.actual, "RootFS": {"Layers": []}}):
            with self.subTest(actual=actual):
                self.actual = actual
                with self.assertRaises(ValueError):
                    self.execute()
                self.assertEqual(self.calls, [])

    def test_probe_failure_keeps_output_and_stops_the_debug_probe(self):
        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.execute(failure=7)
        self.assertEqual(error.exception.returncode, 7)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual((self.directory / "docker-fpm-frr.runtime.json").read_text(), '{"status":"passed"}\n')
        self.assertFalse((self.directory / "docker-fpm-frr-dbg.runtime.json").exists())


if __name__ == "__main__":
    unittest.main()
