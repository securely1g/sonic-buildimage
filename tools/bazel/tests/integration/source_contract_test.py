"""Image contracts follow owner metadata without copied package versions."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from tools.bazel.oci.source_contract import generate


class SourceContractTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.expectations = self.root / "paths.json"
        self.owner = self.root / "runtime_packages.json"
        self.module = self.root / "MODULE.bazel"
        self.output = self.root / "contract.json"
        self.policy = {"schema": 1, "image": "example", "architecture": "amd64", "distribution": "trixie",
                       "packages": {"libexample": {"required_paths": {"usr/lib/libexample.so.2": "elf"},
                                                   "path_modes": {"usr/lib/libexample.so.2": 0o755}}}}
        self.expectations.write_text(json.dumps(self.policy))
        self.metadata = {"schema": 1, "module": "example", "packages": {"libexample": {
            "runtime_target": "//:runtime", "control_fields": {"Package": "libexample", "Version": "2.4.0-1",
            "Depends": "libc6 (>= 2.38)", "Description": "Example library"}}}}
        self.owner.write_text(json.dumps(self.metadata))
        self.module.write_text('module(name = "example", version = "2.4.0.sonic.1")\n')

    def run_contract(self, payloads=None):
        return generate(self.expectations, {"@@example+//:runtime_packages.json": self.owner},
                        {"example": self.module}, {"libexample": "@@example+//:runtime"},
                        {"libexample": "@example//:runtime"}, self.output, payloads)

    def test_owner_changes_update_contract_without_editing_image_policy(self):
        first = self.run_contract()["packages"][0]
        self.metadata["packages"]["libexample"]["control_fields"].update(Version="2.4.0-2", Depends="libc6 (>= 2.40)")
        self.owner.write_text(json.dumps(self.metadata))
        self.module.write_text('module(name = "example", version = "2.4.0.sonic.2")\n')
        second = self.run_contract()["packages"][0]
        self.assertEqual(first["version"], "2.4.0-1")
        self.assertEqual(second["version"], "2.4.0-2")
        self.assertEqual(second["control_fields"]["Depends"], "libc6 (>= 2.40)")
        self.assertEqual(second["source"], {"module": "example", "version": "2.4.0.sonic.2",
                                           "release": "2.4.0", "target": "@example//:runtime"})
        self.assertEqual(second["required_paths"], first["required_paths"])
        self.assertIn("DO NOT EDIT", json.loads(self.output.read_bytes())["_generated"])

    def test_target_architecture_and_commit_come_from_selected_inputs(self):
        self.policy["architecture"] = "arm64"
        self.expectations.write_text(json.dumps(self.policy))
        revision = "a" * 40
        self.module.write_text('module(name = "example", version = "0.0.1-' + revision + '")\n')
        package = self.run_contract()["packages"][0]
        self.assertEqual(package["architecture"], "arm64")
        self.assertEqual(package["control_fields"]["Architecture"], "arm64")
        self.assertEqual(package["source"]["commit"], revision)

    def test_complete_debian_controls_keep_their_original_fields(self):
        """Preserve optional relationships and multiline text without promoting Recommends to Depends."""
        fields = self.metadata["packages"]["libexample"]["control_fields"]
        fields.pop("Depends")
        fields.update({"Recommends": "libc6 (>= 2.38)", "Breaks": "liboldexample (<< 2.4)",
                       "Replaces": "liboldexample", "Description": "Example library\n Detailed description."})
        self.owner.write_text(json.dumps(self.metadata))
        controls = self.run_contract()["packages"][0]["control_fields"]
        for key, value in fields.items():
            self.assertEqual(controls[key], value)
        self.assertEqual(controls["Depends"], "")
        self.assertEqual(controls["Pre-Depends"], "")
        self.assertEqual(controls["Multi-Arch"], "no")

    def test_generated_controls_are_bound_to_the_actual_runtime_tar(self):
        """Reject stale dependency metadata even when its public TAR label is unchanged."""
        payload = self.root / "runtime.tar"
        payload.write_bytes(b"runtime payload")
        self.metadata["architecture"] = "amd64"
        self.metadata["packages"]["libexample"]["payload_sha256"] = hashlib.sha256(payload.read_bytes()).hexdigest()
        self.owner.write_text(json.dumps(self.metadata))
        with self.assertRaisesRegex(ValueError, "requires its runtime TAR"):
            self.run_contract()
        self.run_contract({"libexample": payload})
        payload.write_bytes(b"changed runtime payload")
        with self.assertRaisesRegex(ValueError, "differs from runtime TAR"):
            self.run_contract({"libexample": payload})

    def test_image_cannot_override_owner_dependency_fields(self):
        self.policy["packages"]["libexample"]["control_fields"] = {"Depends": ""}
        self.expectations.write_text(json.dumps(self.policy))
        with self.assertRaisesRegex(ValueError, "duplicates owner"):
            self.run_contract()


if __name__ == "__main__":
    unittest.main()
