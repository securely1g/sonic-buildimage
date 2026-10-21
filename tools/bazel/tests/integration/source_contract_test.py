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
                        {"libexample": "@example//:runtime"}, self.output, payloads=payloads)

    def test_owner_changes_update_contract_without_editing_image_policy(self):
        first = self.run_contract()["packages"][0]
        self.metadata["packages"]["libexample"]["control_fields"].update(Version="2.4.0-2", Depends="libc6 (>= 2.40)")
        self.owner.write_text(json.dumps(self.metadata))
        self.module.write_text('module(name = "example", version = "2.4.0.sonic.2")\n')
        second = self.run_contract()["packages"][0]
        self.assertEqual(first["version"], "2.4.0.sonic.1")
        self.assertEqual(second["version"], "2.4.0.sonic.2")
        self.assertNotIn("control_fields", second)
        self.assertEqual(second["kind"], "runtime-layer")
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
        self.assertNotIn("control_fields", package)
        self.assertEqual(package["source"]["commit"], revision)

    def test_owner_controls_do_not_replace_real_installed_package_state(self):
        """Generated source relationships remain separate from APT's installed Debian controls."""
        fields = self.metadata["packages"]["libexample"]["control_fields"]
        fields.pop("Depends")
        fields.update({"Recommends": "libc6 (>= 2.38)", "Breaks": "liboldexample (<< 2.4)",
                       "Replaces": "liboldexample", "Description": "Example library\n Detailed description."})
        self.owner.write_text(json.dumps(self.metadata))
        package = self.run_contract()["packages"][0]
        self.assertNotIn("control_fields", package)
        self.assertEqual(package["kind"], "runtime-layer")

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

    def test_base_provenance_preserves_paths_without_inventing_package_state(self):
        self.policy.pop("architecture")
        self.policy.pop("image")
        self.policy["packages"]["libexample"]["excluded_baseline_paths"] = ["usr/lib/obsolete.so"]
        self.expectations.write_text(json.dumps(self.policy))
        result = generate(self.expectations, {"@@example+//:runtime_packages.json": self.owner},
                          {"example": self.module}, {"libexample": "@@example+//:runtime"},
                          {"libexample": "@example//:runtime"}, self.output, "arm64", "provenance")
        package = result["packages"]["libexample"]
        self.assertEqual(package["source_version"], "2.4.0.sonic.1")
        self.assertEqual(package["target"], "@example//:runtime")
        self.assertEqual(package["excluded_baseline_paths"], ["usr/lib/obsolete.so"])
        self.assertNotIn("control_fields", package)
        self.assertNotIn("version", package)

    def test_image_cannot_override_owner_dependency_fields(self):
        self.policy["packages"]["libexample"]["control_fields"] = {"Depends": ""}
        self.expectations.write_text(json.dumps(self.policy))
        with self.assertRaisesRegex(ValueError, "duplicates owner"):
            self.run_contract()

    def module_provenance(self, root_module=None):
        return generate(self.expectations, {}, {"example": self.module},
                        {"libexample": "@@example+//:runtime"},
                        {"libexample": "@alias//:runtime"}, self.output,
                        mode="provenance", module_sources={"libexample": "example"},
                        root_module=root_module)["packages"]["libexample"]

    def test_module_only_provenance_tracks_selected_source_and_release(self):
        """Owners without package metadata use their resolved module, retaining public aliases."""
        for version, revision in (("0.0.4-" + "a" * 40, "a" * 40),
                                  ("0.0.4-" + "b" * 40, "b" * 40),
                                  ("3.21.12-sonic.1", "3.21.12-sonic.1")):
            self.module.write_text('module(name = "example", version = "' + version + '")\n')
            result = self.module_provenance()
            self.assertEqual(result["source_revision"], revision)
            self.assertEqual(result["source_version"], version)
            self.assertEqual(result["target"], "@alias//:runtime")
            self.assertEqual(result["required_paths"], self.policy["packages"]["libexample"]["required_paths"])
            self.assertNotIn("control_fields", result)

    def test_git_source_provenance_tracks_the_root_override(self):
        """SWSS's placeholder module version must follow the actual pinned git selection."""
        self.module.write_text('module(name = "example", version = "0.0.0")\n')
        with self.assertRaisesRegex(ValueError, "requires its root git override"):
            self.module_provenance()
        root_module = self.root / "root.MODULE.bazel"
        for revision in ("a" * 40, "b" * 40):
            root_module.write_text('git_override(module_name = "example", commit = "' + revision + '")\n')
            self.assertEqual(self.module_provenance(root_module)["source_revision"], revision)
        root_module.write_text('git_override(module_name = "example", commit = "master")\n')
        with self.assertRaisesRegex(ValueError, "unique pinned git overrides"):
            self.module_provenance(root_module)

    def test_module_only_provenance_rejects_frozen_identity_and_wrong_owner(self):
        """Path policy cannot silently retain an old revision or identify a different owner."""
        for field in ("source_revision", "target", "control_fields"):
            self.policy["packages"]["libexample"][field] = "stale"
            self.expectations.write_text(json.dumps(self.policy))
            with self.assertRaisesRegex(ValueError, "duplicates owner"):
                self.module_provenance()
            del self.policy["packages"]["libexample"][field]
        self.expectations.write_text(json.dumps(self.policy))
        self.module.write_text('module(name = "different-owner", version = "1.0")\n')
        with self.assertRaisesRegex(ValueError, "owner module declaration differs"):
            self.module_provenance()

    def test_module_fallback_cannot_bypass_generated_owner_metadata(self):
        """A known owner's metadata and exact runtime TAR hash remain mandatory."""
        with self.assertRaisesRegex(ValueError, "cannot replace owner package metadata"):
            generate(self.expectations, {"@@example+//:runtime_packages.json": self.owner},
                     {"example": self.module}, {"libexample": "@@example+//:runtime"},
                     {"libexample": "@example//:runtime"}, self.output,
                     mode="provenance", module_sources={"libexample": "example"})


if __name__ == "__main__":
    unittest.main()
