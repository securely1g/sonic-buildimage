#!/usr/bin/env python3
"""Preparation order, source preservation and retained evidence are contractual."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import rust


class RustPreparationTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.artifacts = self.root / "artifacts"
        for name in rust.COMPONENTS:
            component = self.root / "src" / name
            component.mkdir(parents=True)
            (component / "Cargo.lock").write_text("version = 4\n")
            (component / ".bazelrc").write_text(
                "common --registry=https://raw.githubusercontent.com/securely1g/sonic-bazel-registry/codex/common-rust-library\n"
                "common --registry=https://bcr.bazel.build/\n"
                "common --platforms=@sonic_build_infra//platforms:x86_64_trixie\n")
        self.order = []

    def generate(self, command, *, cwd, stdout, stderr):
        name = cwd.name
        self.order.append(name)
        if name == "sonic-swss":
            self.assertTrue((self.root / "src/sonic-swss-common/Cargo.Bazel.lock").is_file())
        self.assertEqual(stderr, subprocess.STDOUT)
        self.assertIn("--bazel-startup-arg=--batch", command)
        self.assertIn("--bazel-startup-arg=--noworkspace_rc", command)
        rc = next(value.split("=", 2)[2] for value in command
                  if value.startswith("--bazel-startup-arg=--bazelrc="))
        self.assertEqual(Path(rc).read_text(), (cwd / ".bazelrc").read_text().replace(
            "codex/common-rust-library", "main"))
        self.assertIn("codex/common-rust-library", (cwd / ".bazelrc").read_text())
        if name == "sonic-swss":
            self.assertEqual(command[command.index("--prepared-common") + 1],
                             str(self.root / "src/sonic-swss-common"))
        else:
            self.assertNotIn("--prepared-common", command)
        overrides = [value.split("=", 2)[2].split("=", 1)[0] for value in command
                     if value.startswith("--bazel-arg=--override_module=")]
        expected = ["sonic-build-infra"]
        if name == "sonic-swss":
            expected += ["sonic-dash-api", "sonic-sairedis"]
        self.assertEqual(overrides, expected)
        self.assertIn("--bazel-arg=--repository_cache=/cache", command)
        (cwd / "Cargo.Bazel.lock").write_text('{"crates": {"fixture 1.0.0": {}}}\n')
        Path(command[command.index("--receipt") + 1]).write_text('{"status": "passed"}\n')
        return subprocess.CompletedProcess(command, 0)

    def prepare(self):
        return rust.prepare(self.root, self.artifacts, startup=["--batch"],
                            options=["--repository_cache=/cache"])

    def test_common_precedes_swss_and_generated_evidence_is_retained(self):
        with mock.patch.object(rust.subprocess, "run", side_effect=self.generate):
            receipt = self.prepare()
        self.assertEqual(self.order, list(rust.COMPONENTS))
        self.assertEqual(receipt["status"], "passed")
        for name in rust.COMPONENTS:
            self.assertTrue(receipt["components"][name]["cargo_lock_unchanged"])
            for file in ("Cargo.lock", "Cargo.Bazel.lock", "preparation.json"):
                self.assertTrue((self.artifacts / name / file).is_file())
        self.assertEqual(json.loads((self.artifacts / "receipt.json").read_text()), receipt)

    def test_component_failure_blocks_consumer_and_writes_failed_receipt(self):
        with mock.patch.object(rust.subprocess, "run", return_value=subprocess.CompletedProcess([], 9)) as run:
            with self.assertRaisesRegex(RuntimeError, "sonic-swss-common Rust preparation failed"):
                self.prepare()
        self.assertEqual(run.call_count, 1)
        self.assertEqual(json.loads((self.artifacts / "receipt.json").read_text())["status"], "failed")

    def test_swss_cannot_change_commons_authoritative_lock(self):
        def mutate(command, **kwargs):
            result = self.generate(command, **kwargs)
            if kwargs["cwd"].name == "sonic-swss":
                (self.root / "src/sonic-swss-common/Cargo.lock").write_text("changed")
            return result
        with mock.patch.object(rust.subprocess, "run", side_effect=mutate):
            with self.assertRaisesRegex(ValueError, "changed committed Cargo.lock: sonic-swss-common"):
                self.prepare()

    def test_success_without_generated_metadata_is_rejected(self):
        def omit(command, **kwargs):
            result = self.generate(command, **kwargs)
            (kwargs["cwd"] / "Cargo.Bazel.lock").unlink()
            return result
        with mock.patch.object(rust.subprocess, "run", side_effect=omit):
            with self.assertRaisesRegex(ValueError, "missing generated Rust metadata"):
                self.prepare()
        self.assertEqual(self.order, ["sonic-swss-common"])

    def test_ambiguous_registry_configuration_is_rejected(self):
        component = self.root / "src/sonic-swss-common"
        config = component / ".bazelrc"
        original = config.read_text()
        config.write_text(original + original)
        with self.assertRaisesRegex(ValueError, "Expected one SONiC registry"):
            self.prepare()
        self.assertEqual(config.read_text(), original + original)

    def test_existing_artifacts_are_not_reused(self):
        self.artifacts.mkdir()
        previous = self.artifacts / "receipt.json"
        previous.write_text("retain previous evidence")
        with self.assertRaisesRegex(ValueError, "must be empty"):
            self.prepare()
        self.assertEqual(previous.read_text(), "retain previous evidence")


if __name__ == "__main__":
    unittest.main()
