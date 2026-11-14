#!/usr/bin/env python3
"""Metadata export tests: mocked native verification, JSON only, no DEB fixtures."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.bazel.ci import kernel_artifacts

ORIGINAL_METADATA = ("kernel-packages.json", "kernel-provenance.json")


class KernelMetadataRetentionTest(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        packages, state, output = root / "packages", root / "state", root / "public"
        packages.mkdir()
        digest = "a" * 64
        source = "b" * 40
        modules = {"sonic-linux-kernel": "0.0.1-" + source,
                   "sonic-build-infra": "0.0.18-" + "c" * 40}
        config = {
            "CONFIGURED_ARCH": "amd64", "CONFIGURED_PLATFORM": "vs", "BLDENV": "trixie",
            "CROSS_BUILD_ENVIRON": "n", "MULTIARCH_QEMU_ENVIRON": "n", "KERNEL_VERSION": "6.12.41",
            "KERNEL_ABISUFFIX": "+deb13", "KERNEL_SUBVERSION": "1", "KERNEL_FEATURESET": "sonic",
            "KVERSION": "6.12.41+deb13-sonic-amd64", "ADDITIONAL_BUILD_PROFILES": "",
            "SECURE_UPGRADE_MODE": "no_sign", "SECURE_UPGRADE_KERNEL_CAFILE": "",
            "INCLUDE_EXTERNAL_PATCHES": "n", "EXTERNAL_KERNEL_PATCH_URL": "", "EXTERNAL_KERNEL_PATCH_LOC": "",
        }
        manifest = {
            "schema": 3, "architecture": "amd64", "platform": "vs", "kernel_config": config,
            "kernel_version": "6.12.41", "kernel_abi": config["KVERSION"], "package_version": "6.12.41-1",
            "signing": "unsigned", "source_date_epoch": 1754969284, "source_archives": [],
            "source_tree_sha256": digest, "source_files": [],
            "build_tools": {"schema_version": 1, "kind": "debian-build-tools", "architecture": "amd64",
                            "execution": "relocatable", "identity_sha256": digest,
                            "input_sha256": [digest], "packages": {"make": "4.4.1-2"}},
            # Empty packages deliberately isolates export behavior, not native acceptance.
            "packages": [], "created_packages": [],
            "debug": {"kernel_build_id": source, "module_count": 1, "module_build_ids_sha256": digest},
        }
        records = {
            "MODULE.bazel.lock": {"lockFileVersion": 25, "registryFileHashes": {}, "moduleExtensions": {}},
            "module-graph.json": {"key": "<root>", "name": "sonic-kernel-consumer", "version": "", "dependencies": []},
            "kernel-source.json": {"url": "https://example.invalid/kernel.tar.gz", "integrity": "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="},
            "infra-source.json": {"url": "https://example.invalid/infra.tar.gz", "integrity": "sha256-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="},
        }
        paths = {"MODULE.bazel.lock": state / "consumer/MODULE.bazel.lock"}
        paths.update({name: state / "build" / name for name in records if name != "MODULE.bazel.lock"})
        fields = dict(zip(records, ("lock_sha256", "graph_sha256", "kernel_source_json_sha256", "infra_source_json_sha256")))
        resolution = {"kernel_source_kind": "registry", "kernel_source_commit": source,
                      "modules": modules, "registry": "https://example.invalid/registry",
                      "infra_source_commit": "c" * 40,
                      "graph_inspection": {"returncode": 0, "module_count": 1,
                                           "module_graph_complete": True, "extension_inspection_complete": True}}
        for name, value in records.items():
            paths[name].parent.mkdir(parents=True, exist_ok=True)
            data = self.encode(value)
            paths[name].write_bytes(data)
            resolution[fields[name]] = self.sha(data)
        paths["kernel-packages.json"] = packages / "kernel-packages.json"
        paths["kernel-packages.json"].write_bytes(self.encode(manifest))
        provenance = {
            "schema": 2, "source_commit": "d" * 40, "consumer_inputs_sha256": digest,
            "kernel_gitlink": source, "modules": modules,
            "launcher": {"execution_strategy": "linux-sandbox", "architecture": "amd64",
                         "bazel_version": "8.5.1", "bazel_sha256": digest},
            "kernel_config": config, "manifest_sha256": self.sha(paths["kernel-packages.json"].read_bytes()),
            "target": kernel_artifacts.kernel.TARGET, "resolution": resolution,
        }
        paths["kernel-provenance.json"] = packages / "kernel-provenance.json"
        paths["kernel-provenance.json"].write_bytes(self.encode(provenance))
        return SimpleNamespace(root=root, packages=packages, state=state, output=output, paths=paths,
                               fields=fields, verified={"manifest": manifest, "provenance": provenance})

    @staticmethod
    def sha(data):
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def encode(value):
        # Preserve deliberately noncanonical whitespace and key order.
        return (" \n" + json.dumps(value, indent=3) + "\n\t").encode()

    def replace(self, fixture, name, data):
        """Rebind synthetic expected hashes so a rejection tests the export guard."""
        fixture.paths[name].write_bytes(data)
        if name == "kernel-provenance.json":
            fixture.verified["provenance"] = json.loads(data)
            return
        if name == "kernel-packages.json":
            fixture.verified["manifest"] = json.loads(data)
            fixture.verified["provenance"]["manifest_sha256"] = self.sha(data)
        else:
            fixture.verified["provenance"]["resolution"][fixture.fields[name]] = self.sha(data)
        fixture.paths["kernel-provenance.json"].write_bytes(self.encode(fixture.verified["provenance"]))

    def collect(self, fixture):
        with mock.patch.object(kernel_artifacts.kernel, "verify", return_value=fixture.verified) as verify:
            result = kernel_artifacts.collect(fixture.root, fixture.state, fixture.output,
                                              architecture="amd64", debs_directory=fixture.packages)
            verify.assert_called_once_with(fixture.packages, fixture.root, architecture="amd64")
            return result

    def rejected(self, fixture, error=ValueError, message=None):
        expectation = self.assertRaisesRegex(error, message) if message else self.assertRaises(error)
        with expectation:
            self.collect(fixture)
        self.assertFalse(fixture.output.exists())

    def test_preserves_original_bytes_and_whitespace(self):
        fixture = self.fixture()
        originals = {name: path.read_bytes() for name, path in fixture.paths.items()}
        (fixture.state / "launcher.log").write_text("unrelated private diagnostic")
        (fixture.packages / "unrelated.json").write_text("{}")
        self.collect(fixture)
        self.assertEqual({path.name for path in fixture.output.iterdir()}, {*ORIGINAL_METADATA, "summary.json"})
        for name, data in originals.items():
            self.assertEqual(fixture.paths[name].read_bytes(), data)
            if name in ORIGINAL_METADATA:
                self.assertEqual((fixture.output / name).read_bytes(), data)
            else:
                self.assertFalse((fixture.output / name).exists())
        summary = json.loads((fixture.output / "summary.json").read_bytes())
        self.assertEqual(summary["artifact_count"], 0)
        self.assertEqual(summary["artifacts"], [])
        self.assertEqual(summary["manifest_sha256"], self.sha(originals["kernel-packages.json"]))
        self.assertEqual(summary["provenance_sha256"], self.sha(originals["kernel-provenance.json"]))
        self.assertEqual(summary["resolution_sha256"], {name: self.sha(originals[name]) for name in fixture.fields})

    def test_rejects_credential_fields_in_every_original(self):
        for name in ORIGINAL_METADATA:
            with self.subTest(record=name):
                fixture = self.fixture()
                value = json.loads(fixture.paths[name].read_bytes())
                value["access_token"] = "synthetic forbidden value"
                self.replace(fixture, name, self.encode(value))
                self.rejected(fixture)

    def test_rejects_unknown_fields_in_original_metadata(self):
        for name in ("kernel-packages.json", "kernel-provenance.json"):
            with self.subTest(record=name):
                fixture = self.fixture()
                value = json.loads(fixture.paths[name].read_bytes())
                value["private_path"] = "/home/runner/private"
                self.replace(fixture, name, self.encode(value))
                self.rejected(fixture)

    def test_rejects_unbound_nested_private_fields(self):
        # These locations are not closed by the existing native verifier.
        # Do not mutate launcher/modules/source inventory: native verify owns those.
        cases = (("kernel-packages.json", ("build_tools", "packages")),
                 ("kernel-provenance.json", ("resolution", "graph_inspection")))
        for name, route in cases:
            with self.subTest(record=name, route=route):
                fixture = self.fixture()
                value = json.loads(fixture.paths[name].read_bytes())
                cursor = value
                for part in route:
                    cursor = cursor[part]
                cursor["private_path"] = "/home/runner/private"
                self.replace(fixture, name, self.encode(value))
                self.rejected(fixture)

    def test_keeps_unrelated_dependency_contents_private_and_hash_verified(self):
        fixture = self.fixture()
        dependency_bytes = {}
        for name in fixture.fields:
            value = {"private_path": "/home/runner/private", "access_token": "synthetic private value",
                     "envVariables": {"PRIVATE": "synthetic environment"}}
            data = self.encode(value)
            dependency_bytes[name] = data
            self.replace(fixture, name, data)
        self.collect(fixture)
        self.assertEqual({path.name for path in fixture.output.iterdir()}, {*ORIGINAL_METADATA, "summary.json"})
        summary = json.loads((fixture.output / "summary.json").read_bytes())
        self.assertEqual(summary["resolution_sha256"], {name: self.sha(data) for name, data in dependency_bytes.items()})
        for name, data in dependency_bytes.items():
            self.assertEqual(fixture.paths[name].read_bytes(), data)
            self.assertFalse((fixture.output / name).exists())
        for path in fixture.output.iterdir():
            self.assertNotIn(b"synthetic private value", path.read_bytes())
            self.assertNotIn(b"/home/runner/private", path.read_bytes())
            self.assertNotIn(b"synthetic environment", path.read_bytes())

    def test_rejects_duplicate_keys_in_every_original(self):
        for name in ORIGINAL_METADATA:
            with self.subTest(record=name):
                fixture = self.fixture()
                value = json.loads(fixture.paths[name].read_bytes())
                key = next(iter(value))
                data = (json.dumps({key: value[key]})[:-1] + "," + json.dumps(value)[1:]).encode()
                self.replace(fixture, name, data)
                self.rejected(fixture, kernel_artifacts.public_artifacts.PublicArtifactError, "duplicate-json-key")

    def test_rejects_nonfinite_constants_in_every_original(self):
        for name in ORIGINAL_METADATA:
            for token in ("NaN", "Infinity", "-Infinity"):
                with self.subTest(record=name, constant=token):
                    fixture = self.fixture()
                    value = json.loads(fixture.paths[name].read_bytes())
                    data = ('{"invalid_number":' + token + "," + json.dumps(value)[1:]).encode()
                    self.replace(fixture, name, data)
                    self.rejected(fixture, kernel_artifacts.public_artifacts.PublicArtifactError, "invalid-json-number")

    def test_rejects_changed_records_without_publishing(self):
        for name in self.fixture().paths:
            with self.subTest(record=name):
                fixture = self.fixture()
                fixture.paths[name].write_bytes(b"{}\n")
                self.rejected(fixture)

    def test_rejects_missing_records_without_publishing(self):
        for name in self.fixture().paths:
            with self.subTest(record=name):
                fixture = self.fixture()
                fixture.paths[name].unlink()
                self.rejected(fixture)

    def test_rejects_symlink_records_without_publishing(self):
        for name in self.fixture().paths:
            with self.subTest(record=name):
                fixture = self.fixture()
                path = fixture.paths[name]
                other = fixture.root / "original-record"
                other.write_bytes(path.read_bytes())
                path.unlink()
                path.symlink_to(other)
                self.rejected(fixture)


    def test_rejects_invalid_source_date_epoch(self):
        for value in ("/home/runner/private", True, -1):
            with self.subTest(epoch=value):
                fixture = self.fixture()
                manifest = fixture.verified["manifest"]
                manifest["source_date_epoch"] = value
                self.replace(fixture, "kernel-packages.json", self.encode(manifest))
                self.rejected(fixture, message="invalid public kernel source timestamp")

    def test_accepts_existing_package_version_syntax(self):
        fixture = self.fixture()
        value = fixture.verified["manifest"]
        value["build_tools"]["packages"] = {"g++": "4:14.2.0-1~deb13u1", "libstdc++6": "14.2.0-19+deb13u1"}
        original = self.encode(value)
        self.replace(fixture, "kernel-packages.json", original)
        self.collect(fixture)
        self.assertEqual((fixture.output / "kernel-packages.json").read_bytes(), original)

    def test_rejects_non_package_tool_values(self):
        for packages in ({"make": "/home/runner/private"}, {"libc6:amd64": "2.41-12"}, {"make": "1.0\n"}):
            with self.subTest(packages=packages):
                fixture = self.fixture()
                value = fixture.verified["manifest"]
                value["build_tools"]["packages"] = packages
                self.replace(fixture, "kernel-packages.json", self.encode(value))
                self.rejected(fixture, message="invalid public kernel tool package metadata")

    def test_accepts_known_inspection_labels_without_rewriting(self):
        known = ["abseil-cpp@20240722.0:cc_configure_extension", "rules_apple@3.5.1:apple"]
        for failures in ([known[0]], [known[1]], known):
            with self.subTest(failures=failures):
                fixture = self.fixture()
                provenance = fixture.verified["provenance"]
                provenance["resolution"]["graph_inspection"].update(
                    returncode=2, module_count=4, extension_inspection_complete=False,
                    unused_extension_failures=failures)
                original = self.encode(provenance)
                self.replace(fixture, "kernel-provenance.json", original)
                self.collect(fixture)
                self.assertEqual((fixture.output / "kernel-provenance.json").read_bytes(), original)

    def test_rejects_invalid_inspection_values(self):
        known = ["abseil-cpp@20240722.0:cc_configure_extension", "rules_apple@3.5.1:apple"]
        changes = (
            {"module_count": "/home/runner/private"}, {"module_count": True}, {"returncode": False},
            {"unused_extension_failures": [known[0]]},
            {"returncode": 2, "extension_inspection_complete": False, "unused_extension_failures": ["/home/runner/private"]},
            {"returncode": 2, "extension_inspection_complete": False, "unused_extension_failures": [known[0], known[0]]},
            {"returncode": 2, "extension_inspection_complete": False, "unused_extension_failures": list(reversed(known))},
        )
        for change in changes:
            with self.subTest(change=change):
                fixture = self.fixture()
                provenance = fixture.verified["provenance"]
                provenance["resolution"]["graph_inspection"].update(change)
                self.replace(fixture, "kernel-provenance.json", self.encode(provenance))
                self.rejected(fixture)


if __name__ == "__main__":
    unittest.main()
