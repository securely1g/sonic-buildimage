"""Image contracts follow owner metadata without copied package versions."""

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

    def run_contract(self):
        return generate(self.expectations, {"@@example+//:runtime_packages.json": self.owner},
                        {"example": self.module}, {"libexample": "@@example+//:runtime"},
                        {"libexample": "@example//:runtime"}, self.output)

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

    def test_image_cannot_override_owner_dependency_fields(self):
        self.policy["packages"]["libexample"]["control_fields"] = {"Depends": ""}
        self.expectations.write_text(json.dumps(self.policy))
        with self.assertRaisesRegex(ValueError, "duplicates owner"):
            self.run_contract()


if __name__ == "__main__":
    unittest.main()
