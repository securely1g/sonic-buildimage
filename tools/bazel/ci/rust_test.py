#!/usr/bin/env python3
"""The source Cargo inputs and retained evidence must survive Bazel unchanged."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import rust


class RustInputsTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.artifacts = self.root / "artifacts"
        for name in rust.COMPONENTS:
            component = self.root / "src" / name
            component.mkdir(parents=True)
            self.git(component, "init", "--quiet")
            self.git(component, "config", "user.name", "Fixture")
            self.git(component, "config", "user.email", "fixture@example.test")
            for filename in rust.INPUTS:
                (component / filename).write_text("tracked " + filename + "\n")
            self.git(component, "add", ".")
            self.git(component, "commit", "--quiet", "-m", "Fixture inputs")

    @staticmethod
    def git(component, *args):
        return subprocess.check_output(["git", "-C", str(component), *args])

    def test_records_tracked_inputs_without_generated_lock_or_helper(self):
        receipt = rust.record(self.root, self.artifacts)
        self.assertEqual(receipt["status"], "passed")
        self.assertEqual(json.loads((self.artifacts / "receipt.json").read_text()), receipt)
        for name in rust.COMPONENTS:
            self.assertEqual(set(receipt["components"][name]["inputs"]), set(rust.INPUTS))
            self.assertFalse((self.root / "src" / name / "Cargo.Bazel.lock").exists())
        self.assertTrue(rust.verify(self.root, self.artifacts)["tracked_inputs_unchanged"])

    def test_rejects_modified_source_before_capture(self):
        (self.root / "src/sonic-swss-common/Cargo.lock").write_text("modified")
        with self.assertRaisesRegex(ValueError, "modified tracked Rust input"):
            rust.record(self.root, self.artifacts)
        self.assertEqual(json.loads((self.artifacts / "receipt.json").read_text())["status"], "failed")

    def test_build_cannot_modify_the_source_lock(self):
        rust.record(self.root, self.artifacts)
        (self.root / "src/sonic-swss/Cargo.lock").write_text("modified by build")
        with self.assertRaisesRegex(ValueError, "modified tracked Rust input"):
            rust.verify(self.root, self.artifacts)

    def test_build_cannot_replace_retained_evidence(self):
        rust.record(self.root, self.artifacts)
        (self.artifacts / "sonic-swss-common/Cargo.toml").write_text("changed evidence")
        with self.assertRaisesRegex(ValueError, "retained evidence changed"):
            rust.verify(self.root, self.artifacts)

    def test_build_cannot_change_selected_revision(self):
        rust.record(self.root, self.artifacts)
        component = self.root / "src/sonic-swss"
        self.git(component, "commit", "--quiet", "--allow-empty", "-m", "Moved source")
        with self.assertRaisesRegex(ValueError, "source revision or input set changed"):
            rust.verify(self.root, self.artifacts)

    def test_existing_artifacts_are_preserved(self):
        self.artifacts.mkdir()
        previous = self.artifacts / "receipt.json"
        previous.write_text("previous evidence")
        with self.assertRaisesRegex(ValueError, "must be empty"):
            rust.record(self.root, self.artifacts)
        self.assertEqual(previous.read_text(), "previous evidence")


if __name__ == "__main__":
    unittest.main()
